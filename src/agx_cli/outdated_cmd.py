"""``agx outdated``: report which agents are behind upstream.

Compose pinning policy (no ``:latest``) means each agent stays frozen
at its captured tag until the user explicitly runs ``agx upgrade``.
Without a drift-check surface, users have no way to know which agents
are behind upstream short of visiting GitHub manually. This module
provides that surface.

Read-only: fetches the agent's pinned tag from compose, fetches the
latest upstream tag via the GitHub Releases API (the one fetch path
``vm init``, ``create`` and ``upgrade`` use too), and classifies each agent as ``current`` /
``outdated`` / ``unknown`` (network fetch failed).

Pre-release filtering: the GitHub ``/releases/latest`` endpoint already
hides pre-releases by upstream policy. Users who pin to a beta on
purpose can pass ``--include-prerelease`` to compare against the
newest pre-release instead (see :func:`fetch_latest_tag`).

Release tag != image tag: GitHub release tags and Docker image tags
follow different conventions per upstream — Hermes publishes
``nousresearch/hermes-agent:v0.21.6`` (same as the release tag),
OpenClaw publishes ``openclaw/openclaw:2026.10.1`` for release
``v2026.10.1``. Every tag this module returns is already in IMAGE form
(:func:`image_tag_from_release`), so callers can pin it directly.

Hermes and OpenClaw both report here. Service prefixes (``hermes-`` /
``openclaw-``) drive the repo lookup; firecrawl-* and other project-
level services are skipped (their pinning is digest-based, not tag-
based, and re-pin via ``agx firecrawl up --upgrade``).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agx_cli import compose, ui
from agx_cli import config as cfg_mod

# GitHub Releases is the version source of truth for every agent type.
GITHUB_API: dict[str, str] = {
    "hermes": "https://api.github.com/repos/NousResearch/hermes-agent",
    "openclaw": "https://api.github.com/repos/openclaw/openclaw",
}

FETCH_TIMEOUT_S: float = 5.0

# A release tag that names a version: ``v0.21.6``, ``v2026.9.24``,
# ``v2026.10.1``, ``v2026.10.5-beta.1``, ``v2026.7.1-2``. Non-version
# release tags upstream also publishes (OpenClaw's ``linux-stable``
# desktop channel) have no matching image and must never be pinned.
_VERSION_TAG_RE = re.compile(r"v?\d+(\.\d+)+([-.][0-9A-Za-z.-]+)?")

# Upstreams whose Docker tags drop the release tag's leading ``v``.
_IMAGE_TAG_STRIPS_V = frozenset({"openclaw"})


def image_tag_from_release(agent_type: str, release_tag: str) -> str | None:
    """Docker image tag for an upstream GitHub release tag, or ``None``
    when the release tag doesn't name a version (no image exists)."""
    if not _VERSION_TAG_RE.fullmatch(release_tag):
        return None
    return normalize_image_tag(agent_type, release_tag)


def normalize_image_tag(agent_type: str, tag: str) -> str:
    """Map a user- or config-supplied tag to the upstream's image-tag
    form: OpenClaw images are ``2026.10.1``, never ``v2026.10.1`` (the
    release-tag form older toolkit versions pinned, which 404s on pull).
    Other tags pass through unchanged."""
    if agent_type in _IMAGE_TAG_STRIPS_V and re.fullmatch(r"v\d.*", tag):
        return tag[1:]
    return tag


# Public, unauthenticated Docker Hub tag lookup (both agent images live
# on Docker Hub). 200 = tag exists, 404 = it doesn't.
DOCKER_HUB_TAG_URL = "https://hub.docker.com/v2/namespaces/{ns}/repositories/{repo}/tags/{tag}"


def image_tag_published(agent_type: str, tag: str) -> bool | None:
    """Whether ``<image base>:<tag>`` exists on Docker Hub.

    ``None`` when it couldn't be determined (network, rate limit) —
    callers then proceed and let the pull report. A GitHub release does
    NOT imply an image: Hermes v0.21.7 (2026-10-12) shipped its release
    with only ``:stable`` / ``:latest`` / ``rc.15-v0.21.7`` images and no
    ``v0.21.7`` tag, which made every ``create`` resolving "latest" fail
    at pull.
    """
    base = compose.HERMES_IMAGE_BASE if agent_type == "hermes" else compose.OPENCLAW_IMAGE_BASE
    ns, _, repo = base.partition("/")
    status = _http_status(DOCKER_HUB_TAG_URL.format(ns=ns, repo=repo, tag=tag))
    if status == 200:
        return True
    if status == 404:
        return False
    return None


def _http_status(url: str) -> int | None:
    """HTTP status of a GET, or ``None`` on a transport failure."""
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def check(
    name: str | None = None,
    *,
    include_prerelease: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Enumerate agent services in compose and classify each against
    upstream. Returns a payload with one row per agent.

    Network failure on any single repo's latest-tag fetch is surfaced
    as ``status: unknown`` for that row, not raised — partial visibility
    beats no visibility.
    """
    cfg = cfg_mod.load(config_path)
    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}

    # Cache per-repo latest-tag lookups so two agents of the same type
    # don't double up the network call.
    latest_cache: dict[str, str | None] = {}

    rows: list[dict[str, Any]] = []
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        if svc_name.startswith("hermes-"):
            agent_type = "hermes"
            agent_name = svc_name[len("hermes-") :]
        elif svc_name.startswith("openclaw-"):
            agent_type = "openclaw"
            agent_name = svc_name[len("openclaw-") :]
        else:
            continue
        if name is not None and agent_name != name:
            continue

        image = svc.get("image") or ""
        current_tag = normalize_image_tag(agent_type, _tag_from_image(image))

        if agent_type not in latest_cache:
            latest_cache[agent_type] = fetch_latest_tag(
                agent_type, include_prerelease=include_prerelease
            )
        latest_tag = latest_cache[agent_type]

        status = _classify(current_tag, latest_tag)
        rows.append(
            {
                "agent": agent_name,
                "type": agent_type,
                "current_tag": current_tag,
                "latest_tag": latest_tag,
                "status": status,
            }
        )

    if name is not None and not rows:
        from agx_cli.errors import UserError

        raise UserError(f"agent {name!r} not in compose")

    rows.sort(key=lambda r: (_status_rank(r["status"]), r["agent"]))
    payload = {
        "include_prerelease": include_prerelease,
        "agents": rows,
        "outdated_count": sum(1 for r in rows if r["status"] == "outdated"),
        "unknown_count": sum(1 for r in rows if r["status"] == "unknown"),
    }
    _emit(payload)
    return payload


# ---------- internals ----------


def _tag_from_image(image: str) -> str:
    """Extract the tag from a compose ``image:`` value.

    Handles ``repo:tag`` and ``repo@sha256:...`` and ``repo:tag@sha256:...``.
    Digest-only references return empty string (no tag to compare).
    """
    if not image:
        return ""
    # Strip digest if present.
    base = image.split("@", 1)[0]
    if ":" in base:
        _, _, tag = base.rpartition(":")
        return tag
    return ""


def _classify(current: str, latest: str | None) -> str:
    if latest is None:
        return "unknown"
    if not current:
        return "unknown"
    if current == latest:
        return "current"
    return "outdated"


def _status_rank(status: str) -> int:
    """Outdated first, then unknown, then current."""
    return {"outdated": 0, "unknown": 1, "current": 2}.get(status, 9)


def fetch_latest_tag(agent_type: str, *, include_prerelease: bool = False) -> str | None:
    """Best-effort upstream lookup; returns an IMAGE tag (see
    :func:`image_tag_from_release`) or ``None`` when unreachable.

    Default: ``/releases/latest`` — the release upstream marks latest,
    which excludes pre-releases. With ``include_prerelease`` the newest
    pre-release wins when it is newer than that stable: walk
    ``/releases`` (newest-created first) and take the first version tag
    that is a pre-release or IS the latest stable. Taking ``data[0]``
    blindly is wrong — OpenClaw cuts maintenance releases of older lines
    (``v2026.6.35`` published after ``v2026.9.x``) and non-version tags
    (``linux-stable``), either of which can be the newest-created entry.
    Versions are never compared numerically: Hermes switched from calver
    (``v2026.9.24``) to semver (``v0.21.6``), so ordering by version
    would rank the newest release last.
    """
    base = GITHUB_API.get(agent_type)
    if base is None:
        return None
    latest = _fetch_json(f"{base}/releases/latest")
    stable = latest.get("tag_name") if isinstance(latest, dict) else None
    if not include_prerelease:
        return image_tag_from_release(agent_type, stable) if isinstance(stable, str) else None
    releases = _fetch_json(f"{base}/releases?per_page=30")
    if not isinstance(releases, list):
        return None
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        tag = rel.get("tag_name")
        if not isinstance(tag, str) or image_tag_from_release(agent_type, tag) is None:
            continue
        if rel.get("prerelease") or tag == stable:
            return image_tag_from_release(agent_type, tag)
    return None


def _fetch_json(url: str) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _emit(payload: dict[str, Any]) -> None:
    rows = payload["agents"]
    if not rows:
        ui.emit(payload, human="no agents in compose")
        return

    headers = ("agent", "type", "current", "latest", "status")
    body: list[tuple[str, str, str, str, str]] = []
    for r in rows:
        body.append(
            (
                r["agent"],
                r["type"],
                r["current_tag"] or "(no tag)",
                r["latest_tag"] or "(fetch failed)",
                r["status"],
            )
        )
    widths = [
        max(len(h), max((len(row[i]) for row in body), default=0)) for i, h in enumerate(headers)
    ]
    line = "  ".join(f"{h:<{widths[i]}}" for i, h in enumerate(headers))
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    out_lines = [line, sep]
    for row in body:
        out_lines.append("  ".join(f"{row[i]:<{widths[i]}}" for i in range(len(headers))))

    suffix: list[str] = []
    if payload["outdated_count"]:
        suffix.append(
            f"\n{payload['outdated_count']} agent(s) outdated — "
            f"`agx upgrade <name> --latest` to bump."
        )
    if payload["unknown_count"]:
        suffix.append(f"\n{payload['unknown_count']} agent(s) unknown (couldn't reach GitHub).")
    ui.emit(payload, human="\n".join(out_lines) + "".join(suffix))
