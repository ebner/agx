"""``agents outdated``: report which agents are behind upstream.

Compose pinning policy (no ``:latest``) means each agent stays frozen
at its captured tag until the user explicitly runs ``agents upgrade``.
Without a drift-check surface, users have no way to know which agents
are behind upstream short of visiting GitHub manually. This module
provides that surface.

Read-only: fetches the agent's pinned tag from compose, fetches the
latest upstream tag via the GitHub Releases API (same mechanism as
``vm._fetch_latest_tag``), and classifies each agent as ``current`` /
``outdated`` / ``unknown`` (network fetch failed).

Pre-release filtering: the GitHub ``/releases/latest`` endpoint already
hides pre-releases by upstream policy. Users who pin to a beta on
purpose can pass ``--include-prerelease`` to compare against the
absolute newest tag from ``/releases?per_page=10`` instead.

Hermes and OpenClaw both report here. Service prefixes (``hermes-`` /
``openclaw-``) drive the repo lookup; firecrawl-* and other project-
level services are skipped (their pinning is digest-based, not tag-
based, and re-pin via ``agents firecrawl up --upgrade``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agents_cli import compose, ui
from agents_cli import config as cfg_mod

# Same source of truth as vm._fetch_latest_tag — duplicated here as a
# top-level constant so this module can also fetch via /releases (the
# "all releases" form, used when --include-prerelease is on).
GITHUB_API: dict[str, str] = {
    "hermes": "https://api.github.com/repos/NousResearch/hermes-agent",
    "openclaw": "https://api.github.com/repos/openclaw/openclaw",
}

# Tags carrying these tokens are filtered out unless --include-prerelease.
PRERELEASE_MARKERS: tuple[str, ...] = (
    "beta",
    "rc",
    "pre",
    "alpha",
    "dev",
    "snapshot",
)

FETCH_TIMEOUT_S: float = 5.0


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
        current_tag = _tag_from_image(image)

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
        from agents_cli.errors import UserError

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
    """Best-effort upstream lookup.

    Default path uses ``/releases/latest`` which upstream auto-filters
    pre-releases. With ``--include-prerelease`` we hit ``/releases``
    and take the first entry (most recent by GitHub's ordering).
    """
    base = GITHUB_API.get(agent_type)
    if base is None:
        return None
    if include_prerelease:
        url = f"{base}/releases?per_page=10"
        return _fetch_first_tag(url)
    return _fetch_single_tag(f"{base}/releases/latest")


def _fetch_single_tag(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:
            data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            tag = data.get("tag_name")
            return str(tag) if tag else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _fetch_first_tag(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list) and data:
                tag = data[0].get("tag_name")
                return str(tag) if tag else None
            return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def is_prerelease(tag: str) -> bool:
    """Public helper — used by callers that want to filter local tag
    lists (e.g. the upgrade command's tag-bump fallback)."""
    lower = tag.lower()
    return any(m in lower for m in PRERELEASE_MARKERS)


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
            f"`agents upgrade <name> --latest` to bump."
        )
    if payload["unknown_count"]:
        suffix.append(f"\n{payload['unknown_count']} agent(s) unknown (couldn't reach GitHub).")
    ui.emit(payload, human="\n".join(out_lines) + "".join(suffix))
