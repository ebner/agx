"""Unit tests for upstream-release notifications (``update_notify``).

Release-note fixtures are fabricated; they only mimic the STRUCTURE of
the two upstreams' notes (Hermes: H1 title, metadata lines, blockquote
lead, ``**Title** — …`` highlights with PR-link credits, an upgrade-notes
section; OpenClaw: a version heading, first-sentence highlights with
``(#n) Fixes #n Thanks @x`` tails).
"""

from __future__ import annotations

from typing import Any

import pytest

from agx_cli import update_notify
from agx_cli.config import AgentsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agx_cli.update_notify import Update

HERMES_NOTES = """# Example Agent v1.3.0

**Release Date:** November 5, 2026
**Since v1.2.0:** ~100 commits

> **The Lantern Release.** Sessions now resume across restarts. Plugins update themselves.

<!-- BUILDS_TABLE -->

---

## ✨ Highlights

- **Sessions resume across restarts** — Long description of the change \
([#101](https://github.com/example/agent/pull/101), \
[#102](https://github.com/example/agent/pull/102) — @alice, @bob)
- **Self-updating plugins** — Another long description. ([#103](https://example.com/103))

## ⚠️ Upgrade notes

- **The old `foo` tool is gone.** Use `bar` instead; nothing rewrites your \
prompts for you. ([#104](https://github.com/example/agent/pull/104) — @carol)
- **Config key `a.b` was renamed to `a.c`.**

## 🧠 Core

- **Internal refactor** — not interesting for an upgrade mail.

## 👥 Contributors

- @alice, @bob, @carol
"""

OPENCLAW_NOTES = """## 2026.11.1

### Highlights

- Safer settings: failed saves no longer overwrite secrets. Second sentence here. \
(#1, #2) Fixes #3, #4 Thanks @dave.
- More reliable replies keep reaching the agent. (#5) Thanks @erin and @frank.

### Changes

- Something minor.
"""


@pytest.fixture
def cfg(tmp_path: Any) -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return AgentsConfig(
        paths=PathsConfig(host_data_dir=str(data_dir)),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock", ubuntu_uid=1000, ubuntu_gid=1000
        ),
    )


# ---------- condense ----------


def test_condense_hermes_style_notes() -> None:
    out = update_notify.condense(HERMES_NOTES)
    assert out[0] == (
        "The Lantern Release. Sessions now resume across restarts. Plugins update themselves."
    )
    text = "\n".join(out)
    # Highlights: bold titles only, no descriptions, no credits.
    assert "Highlights:\n- Sessions resume across restarts\n- Self-updating plugins" in text
    assert "Long description" not in text
    # Upgrade notes: full bullet text, markup and PR/contributor credits stripped.
    assert (
        "- The old `foo` tool is gone. Use `bar` instead; nothing rewrites your prompts for you."
    ) in text
    assert "- Config key `a.b` was renamed to `a.c`." in text
    # Metadata lines, the HTML comment and unrelated sections are dropped.
    for gone in ("Release Date", "Since v1.2.0", "BUILDS_TABLE", "Internal refactor", "@alice"):
        assert gone not in text
    assert "#10" not in text and "](" not in text and "**" not in text


def test_condense_openclaw_style_notes() -> None:
    out = update_notify.condense(OPENCLAW_NOTES)
    assert out == [
        "",
        "Highlights:",
        "- Safer settings: failed saves no longer overwrite secrets.",
        "- More reliable replies keep reaching the agent.",
    ]


def test_condense_caps_bullets_and_lead() -> None:
    lead = " ".join(f"Sentence number {i} is here." for i in range(100))
    bullets = "\n".join(f"- **Item {i}** — text" for i in range(15))
    out = update_notify.condense(f"# T v1.0\n\n{lead}\n\n## Highlights\n\n{bullets}\n")
    assert len(out[0]) <= update_notify.LEAD_MAX_CHARS
    assert out[0].endswith(".")  # cut at a sentence boundary
    assert out[-1] == f"- (+{15 - update_notify.SECTION_MAX_BULLETS} more)"


# ---------- release range ----------


def _rel(tag: str, **extra: Any) -> dict[str, Any]:
    return {"tag_name": tag, "body": "", "html_url": f"https://example.com/{tag}", **extra}


def test_attach_releases_between_pinned_and_latest() -> None:
    releases = [
        _rel("v2.0.0-beta.1", prerelease=True),
        _rel("v1.3.0"),
        _rel("linux-stable"),  # non-version tag
        _rel("v1.2.9", draft=True),
        _rel("v1.2.1"),
        _rel("v1.2.0"),
        _rel("v1.1.0"),
    ]
    u = Update(agent_type="hermes", current="v1.2.0", latest="v1.3.0", agents=["coder"])
    update_notify._attach_releases(u, releases)
    assert [r["tag_name"] for r in u.releases] == ["v1.3.0", "v1.2.1"]
    assert u.complete is True
    assert u.compare_url == ("https://github.com/NousResearch/hermes-agent/compare/v1.2.0...v1.3.0")


def test_attach_releases_maps_openclaw_image_tags_to_release_tags() -> None:
    releases = [_rel("v2026.11.1"), _rel("v2026.10.10"), _rel("v2026.10.9")]
    u = Update(agent_type="openclaw", current="2026.10.9", latest="2026.11.1")
    update_notify._attach_releases(u, releases)
    assert [r["tag_name"] for r in u.releases] == ["v2026.11.1", "v2026.10.10"]
    assert u.compare_url is not None
    assert u.compare_url.endswith("/compare/v2026.10.9...v2026.11.1")


def test_attach_releases_pinned_tag_outside_window_is_incomplete() -> None:
    releases = [_rel(f"v1.{n}.0") for n in range(20, 0, -1)]
    u = Update(agent_type="hermes", current="v0.9.0", latest="v1.20.0")
    update_notify._attach_releases(u, releases)
    assert len(u.releases) == update_notify.MAX_RELEASES
    assert u.releases[0]["tag_name"] == "v1.20.0"
    assert u.complete is False and u.compare_url is None


def test_attach_releases_latest_missing_lists_nothing() -> None:
    u = Update(agent_type="hermes", current="v1.0.0", latest="v1.1.0")
    update_notify._attach_releases(u, [_rel("v1.0.0")])
    assert u.releases == [] and u.complete is False


# ---------- check ----------


def _rows(*rows: tuple[str, str, str, str | None]) -> dict[str, Any]:
    return {
        "agents": [
            {
                "agent": agent,
                "type": agent_type,
                "current_tag": current,
                "latest_tag": latest,
                "status": "unknown"
                if latest is None
                else ("current" if current == latest else "outdated"),
            }
            for agent, agent_type, current, latest in rows
        ]
    }


def test_check_groups_skips_notified_and_unpublished(cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch(
        "agx_cli.outdated_cmd.collect",
        return_value=_rows(
            ("coder", "hermes", "v1.2.0", "v1.3.0"),
            ("coder2", "hermes", "v1.2.0", "v1.3.0"),
            ("personal", "openclaw", "2026.10.9", "2026.11.1"),
            ("old", "hermes", "v1.1.0", "v1.3.0"),
            ("fresh", "hermes", "v1.3.0", "v1.3.0"),
        ),
    )
    published = mocker.patch(
        "agx_cli.outdated_cmd.image_tag_published",
        side_effect=lambda agent_type, tag: agent_type == "hermes",
    )
    fetch = mocker.patch(
        "agx_cli.outdated_cmd.fetch_releases",
        return_value=[_rel("v1.3.0"), _rel("v1.2.0"), _rel("v1.1.0")],
    )

    updates, reached = update_notify.check(cfg, {"old": "v1.3.0"})

    assert reached is True
    # coder + coder2 share one entry; `old` was already mailed about
    # v1.3.0; the openclaw image isn't published yet; `fresh` is current.
    [u] = updates
    assert (u.agents, u.current, u.latest) == (["coder", "coder2"], "v1.2.0", "v1.3.0")
    assert [r["tag_name"] for r in u.releases] == ["v1.3.0"]
    assert published.call_count == 2  # once per (type, tag)
    fetch.assert_called_once_with("hermes")


def test_check_reports_unreached_upstream(cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch(
        "agx_cli.outdated_cmd.collect",
        return_value=_rows(("coder", "hermes", "v1.2.0", None)),
    )
    updates, reached = update_notify.check(cfg, {})
    assert updates == [] and reached is False


def test_check_inconclusive_image_lookup_still_notifies(cfg: AgentsConfig, mocker: Any) -> None:
    mocker.patch(
        "agx_cli.outdated_cmd.collect",
        return_value=_rows(("coder", "hermes", "v1.2.0", "v1.3.0")),
    )
    mocker.patch("agx_cli.outdated_cmd.image_tag_published", return_value=None)
    mocker.patch("agx_cli.outdated_cmd.fetch_releases", return_value=None)
    [u], _ = update_notify.check(cfg, {})
    assert u.releases == [] and u.complete is False


# ---------- mail ----------


def test_mail_subject_and_body(cfg: AgentsConfig) -> None:
    u = Update(
        agent_type="hermes",
        current="v1.2.0",
        latest="v1.3.0",
        agents=["coder"],
        releases=[_rel("v1.3.0", body=HERMES_NOTES, published_at="2026-11-05T21:32:33Z")],
        compare_url="https://github.com/example/agent/compare/v1.2.0...v1.3.0",
    )
    assert update_notify.mail_subject([u]) == "[agents] update available: coder v1.2.0 -> v1.3.0"
    other = Update(agent_type="openclaw", current="a", latest="b", agents=["personal"])
    assert update_notify.mail_subject([u, other]) == ("[agents] updates available: coder, personal")

    body = update_notify.mail_body(cfg, [u], "2026-11-06T08:00:00+00:00")
    assert "coder (hermes): v1.2.0 -> v1.3.0\n" in body
    assert "    upgrade:  agx upgrade coder\n" in body
    assert "    changes:  https://github.com/example/agent/compare/v1.2.0...v1.3.0\n" in body
    assert "  v1.3.0 (2026-11-05)\n" in body
    assert "    Upgrade notes:\n    - The old `foo` tool is gone." in body
    assert "    full notes: https://example.com/v1.3.0\n" in body
    assert all(len(line) <= update_notify.MAIL_WIDTH for line in body.splitlines())
