"""outdated_cmd: report which agents are behind upstream."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agx_cli import compose, outdated_cmd
from agx_cli import config as cfg_mod
from agx_cli.config import (
    AgentsConfig,
    DefaultsConfig,
    HostConfig,
    PathsConfig,
    VmRuntimeConfig,
)
from agx_cli.errors import UserError


@pytest.fixture
def cfg(tmp_path: Path) -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return AgentsConfig(
        paths=PathsConfig(host_data_dir=str(data_dir)),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock",
            ubuntu_uid=1000,
            ubuntu_gid=1000,
        ),
        defaults=DefaultsConfig(hermes_image_tag="v2026.5.7", openclaw_image_tag="0.1.0"),
    )


@pytest.fixture
def saved_config(cfg: AgentsConfig, tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg, p)
    return p


def _write_compose(cfg: AgentsConfig, services: dict[str, dict[str, Any]]) -> None:
    doc = compose.load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    doc["services"] = services
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        compose.dump_compose(doc), encoding="utf-8"
    )


# ---------- tag extraction ----------


def test_tag_from_image_handles_simple_repo_tag() -> None:
    assert outdated_cmd._tag_from_image("nousresearch/hermes-agent:v2026.5.7") == "v2026.5.7"


def test_tag_from_image_strips_digest_suffix() -> None:
    """Digest pins like ``repo:tag@sha256:...`` should yield the tag."""
    assert outdated_cmd._tag_from_image("repo:v1.2.3@sha256:" + "a" * 64) == "v1.2.3"


def test_tag_from_image_returns_empty_for_digest_only() -> None:
    """Digest-only pins (no tag at all) yield empty — `outdated` then
    classifies the agent as `unknown` rather than guessing.
    """
    assert outdated_cmd._tag_from_image("repo@sha256:" + "a" * 64) == ""


def test_tag_from_image_empty_string() -> None:
    assert outdated_cmd._tag_from_image("") == ""


# ---------- classification ----------


def test_classify_current_when_tags_match() -> None:
    assert outdated_cmd._classify("v2026.5.7", "v2026.5.7") == "current"


def test_classify_outdated_when_tags_differ() -> None:
    assert outdated_cmd._classify("v2026.4.16", "v2026.5.7") == "outdated"


def test_classify_unknown_when_latest_missing() -> None:
    """Network fetch failed — surface as unknown, NOT outdated.
    Otherwise users could be told to upgrade based on a stale local view.
    """
    assert outdated_cmd._classify("v2026.5.7", None) == "unknown"


def test_classify_unknown_when_current_blank() -> None:
    """Digest-only pin has no current tag to compare — unknown."""
    assert outdated_cmd._classify("", "v2026.5.7") == "unknown"


# ---------- check() end-to-end ----------


def test_check_reports_current_when_pinned_tag_matches_latest(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    _write_compose(
        cfg,
        {"hermes-coder": {"image": "nousresearch/hermes-agent:v2026.5.7"}},
    )
    mocker.patch("agx_cli.outdated_cmd.fetch_latest_tag", return_value="v2026.5.7")

    payload = outdated_cmd.check(config_path=saved_config)
    assert payload["outdated_count"] == 0
    assert payload["agents"][0]["status"] == "current"


def test_check_reports_outdated_when_tag_lags(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    _write_compose(
        cfg,
        {"hermes-coder": {"image": "nousresearch/hermes-agent:v2026.4.16"}},
    )
    mocker.patch("agx_cli.outdated_cmd.fetch_latest_tag", return_value="v2026.5.7")

    payload = outdated_cmd.check(config_path=saved_config)
    assert payload["outdated_count"] == 1
    [row] = payload["agents"]
    assert row["status"] == "outdated"
    assert row["current_tag"] == "v2026.4.16"
    assert row["latest_tag"] == "v2026.5.7"


def test_check_reports_unknown_when_github_unreachable(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    _write_compose(
        cfg,
        {"hermes-coder": {"image": "nousresearch/hermes-agent:v2026.5.7"}},
    )
    mocker.patch("agx_cli.outdated_cmd.fetch_latest_tag", return_value=None)

    payload = outdated_cmd.check(config_path=saved_config)
    assert payload["unknown_count"] == 1
    assert payload["agents"][0]["status"] == "unknown"


def test_check_caches_latest_per_agent_type(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    """Two Hermes agents → exactly one GitHub call for the hermes repo.
    No accidental N+1 against the API.
    """
    _write_compose(
        cfg,
        {
            "hermes-coder": {"image": "nousresearch/hermes-agent:v2026.5.7"},
            "hermes-backup": {"image": "nousresearch/hermes-agent:v2026.4.16"},
        },
    )
    fetch = mocker.patch("agx_cli.outdated_cmd.fetch_latest_tag", return_value="v2026.5.7")

    outdated_cmd.check(config_path=saved_config)
    assert fetch.call_count == 1


def test_check_skips_non_agent_services(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> None:
    """firecrawl-* and other project-level services aren't agents —
    must not appear in the table (they use digest pinning, not tags).
    """
    _write_compose(
        cfg,
        {
            "hermes-coder": {"image": "nousresearch/hermes-agent:v2026.5.7"},
            "firecrawl-api": {"image": "ghcr.io/firecrawl/firecrawl@sha256:" + "a" * 64},
            "firecrawl-redis": {"image": "redis@sha256:" + "b" * 64},
        },
    )
    mocker.patch("agx_cli.outdated_cmd.fetch_latest_tag", return_value="v2026.5.7")

    payload = outdated_cmd.check(config_path=saved_config)
    agents = [r["agent"] for r in payload["agents"]]
    assert agents == ["coder"]


def test_check_sorts_outdated_first(cfg: AgentsConfig, saved_config: Path, mocker: Any) -> None:
    """Outdated agents are the actionable ones — they must be at the top
    of the rendered table so the user sees them without scrolling.
    """
    _write_compose(
        cfg,
        {
            "hermes-current": {"image": "nousresearch/hermes-agent:v2026.5.7"},
            "hermes-outdated": {"image": "nousresearch/hermes-agent:v2026.4.16"},
        },
    )
    mocker.patch("agx_cli.outdated_cmd.fetch_latest_tag", return_value="v2026.5.7")

    payload = outdated_cmd.check(config_path=saved_config)
    assert [r["agent"] for r in payload["agents"]] == ["outdated", "current"]


def test_check_rejects_unknown_agent_name(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    _write_compose(
        cfg,
        {"hermes-coder": {"image": "nousresearch/hermes-agent:v2026.5.7"}},
    )
    mocker.patch("agx_cli.outdated_cmd.fetch_latest_tag", return_value="v2026.5.7")

    with pytest.raises(UserError, match="not in compose"):
        outdated_cmd.check("ghost", config_path=saved_config)


def test_check_filters_to_single_agent_when_name_given(
    cfg: AgentsConfig, saved_config: Path, mocker: Any
) -> None:
    _write_compose(
        cfg,
        {
            "hermes-coder": {"image": "nousresearch/hermes-agent:v2026.5.7"},
            "hermes-backup": {"image": "nousresearch/hermes-agent:v2026.4.16"},
        },
    )
    mocker.patch("agx_cli.outdated_cmd.fetch_latest_tag", return_value="v2026.5.7")

    payload = outdated_cmd.check("backup", config_path=saved_config)
    agents = [r["agent"] for r in payload["agents"]]
    assert agents == ["backup"]


# ---------- release tag -> image tag ----------


@pytest.mark.parametrize(
    ("agent_type", "release", "image"),
    [
        ("hermes", "v0.21.6", "v0.21.6"),
        ("hermes", "v2026.9.24", "v2026.9.24"),
        ("openclaw", "v2026.10.1", "2026.10.1"),
        ("openclaw", "v2026.10.5-beta.1", "2026.10.5-beta.1"),
        ("openclaw", "v2026.7.1-2", "2026.7.1-2"),
        ("openclaw", "linux-stable", None),
        ("hermes", "nightly", None),
    ],
)
def test_image_tag_from_release(agent_type: str, release: str, image: str | None) -> None:
    """OpenClaw images drop the release tag's `v` (`openclaw/openclaw:
    v2026.10.1` does not exist); non-version release tags have no image."""
    assert outdated_cmd.image_tag_from_release(agent_type, release) == image


def test_normalize_image_tag_repairs_legacy_openclaw_pins() -> None:
    assert outdated_cmd.normalize_image_tag("openclaw", "v2026.5.12") == "2026.5.12"
    assert outdated_cmd.normalize_image_tag("openclaw", "2026.10.1") == "2026.10.1"
    assert outdated_cmd.normalize_image_tag("hermes", "v0.21.6") == "v0.21.6"


def _github(
    monkeypatch: pytest.MonkeyPatch, *, latest: str, releases: list[dict[str, Any]]
) -> None:
    def fake(url: str) -> Any:
        return {"tag_name": latest} if url.endswith("/releases/latest") else releases

    monkeypatch.setattr(outdated_cmd, "_fetch_json", fake)


def test_fetch_latest_tag_returns_image_form(monkeypatch: pytest.MonkeyPatch) -> None:
    _github(monkeypatch, latest="v2026.10.1", releases=[])
    assert outdated_cmd.fetch_latest_tag("openclaw") == "2026.10.1"


def test_fetch_latest_prerelease_skips_maintenance_and_channel_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newest-CREATED first, as GitHub lists them: a channel tag and a
    maintenance release of an older line precede the real newest beta."""
    _github(
        monkeypatch,
        latest="v2026.10.1",
        releases=[
            {"tag_name": "linux-stable", "prerelease": True},
            {"tag_name": "v2026.8.35", "prerelease": False},
            {"tag_name": "v2026.10.5-beta.1", "prerelease": True},
            {"tag_name": "v2026.10.1", "prerelease": False},
        ],
    )
    assert outdated_cmd.fetch_latest_tag("openclaw", include_prerelease=True) == "2026.10.5-beta.1"


def test_fetch_latest_prerelease_falls_back_to_newer_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _github(
        monkeypatch,
        latest="v2026.10.1",
        releases=[
            {"tag_name": "v2026.8.35", "prerelease": False},
            {"tag_name": "v2026.10.1", "prerelease": False},
            {"tag_name": "v2026.10.1-beta.2", "prerelease": True},
        ],
    )
    assert outdated_cmd.fetch_latest_tag("openclaw", include_prerelease=True) == "2026.10.1"


def test_fetch_latest_tag_unreachable_is_none() -> None:
    # The autouse no-network fixture makes every lookup "unreachable".
    assert outdated_cmd.fetch_latest_tag("hermes") is None
    assert outdated_cmd.fetch_latest_tag("hermes", include_prerelease=True) is None


def test_image_tag_published_maps_hub_status(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def status(url: str) -> int | None:
        seen.append(url)
        return {"v0.21.6": 200, "v0.21.7": 404}.get(url.rsplit("/", 1)[1])

    monkeypatch.setattr(outdated_cmd, "_http_status", status)
    assert outdated_cmd.image_tag_published("hermes", "v0.21.6") is True
    assert outdated_cmd.image_tag_published("hermes", "v0.21.7") is False
    assert outdated_cmd.image_tag_published("hermes", "v0.0.1") is None  # unknown/5xx/network
    assert seen[0] == (
        "https://hub.docker.com/v2/namespaces/nousresearch/repositories/hermes-agent/tags/v0.21.6"
    )
    outdated_cmd.image_tag_published("openclaw", "2026.10.1")
    assert "/namespaces/openclaw/repositories/openclaw/tags/2026.10.1" in seen[-1]
