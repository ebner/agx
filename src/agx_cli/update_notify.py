"""Upstream-release notifications for ``agx doctor-cron``.

``agx outdated`` answers "is anything behind?" when asked; this module
is the proactive half. :func:`check` runs from ``doctor-cron run`` at
most every ``[doctor_cron].update_check_hours`` (0 = off) and returns
one :class:`Update` per agent whose latest upstream release

- differs from the agent's pinned tag (same equality test as
  ``outdated`` — versions are never ordered, see
  ``outdated_cmd.fetch_latest_tag``),
- was not notified for that agent before (``notified`` maps agent →
  the last tag mailed about; one mail per release, not per poll), and
- has a pullable image: a GitHub release does not imply an image
  (Hermes v0.21.7), and ``agx upgrade`` refuses until it exists, so
  such a release is retried on the next check instead of mailed.

The mail carries every release between the pinned tag and the latest
one (newest first, capped at :data:`MAX_RELEASES`) with condensed
notes. Upstream notes range from ~2 KB (Hermes patch releases) to
>100 KB (Hermes minors, which re-document every patch since the last
minor), so :func:`condense` keeps the lead paragraph, the full
upgrade-notes / breaking-changes bullets (those are actionable before
upgrading), and only the titles of the highlights — the link to the
full notes is always included.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

from agx_cli import config as cfg_mod
from agx_cli import outdated_cmd

MAIL_WIDTH = 76
MAX_RELEASES = 8
LEAD_MAX_CHARS = 700
SECTION_MAX_BULLETS = 10
NOTE_MAX_CHARS = 400
TITLE_MAX_CHARS = 200

# Sections whose bullets go into the mail in full / as titles.
_FULL_SECTIONS = re.compile(r"upgrad|breaking|migrat|deprecat|removed|action required", re.I)
_TITLE_SECTIONS = re.compile(r"highlight", re.I)


@dataclass
class Update:
    """One pending upgrade: ``agents`` (same type, same pinned tag) can
    go from ``current`` to ``latest`` (image tags)."""

    agent_type: str
    current: str
    latest: str
    agents: list[str] = field(default_factory=list)
    releases: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True  # False: older releases in the range were cut
    compare_url: str | None = None


def check(cfg: cfg_mod.AgentsConfig, notified: dict[str, str]) -> tuple[list[Update], bool]:
    """Pending, not-yet-notified updates, and whether the check reached
    upstream for every agent type in compose (``False`` = retry soon)."""
    rows = outdated_cmd.collect(cfg)["agents"]
    reached = all(r["latest_tag"] is not None for r in rows if r["current_tag"])

    pending: dict[tuple[str, str, str], Update] = {}
    published: dict[tuple[str, str], bool | None] = {}
    for row in rows:
        if row["status"] != "outdated":
            continue
        agent, agent_type, latest = row["agent"], row["type"], row["latest_tag"]
        if notified.get(agent) == latest:
            continue
        key = (agent_type, latest)
        if key not in published:
            published[key] = outdated_cmd.image_tag_published(agent_type, latest)
        if published[key] is False:
            continue
        update = pending.setdefault(
            (agent_type, row["current_tag"], latest),
            Update(agent_type=agent_type, current=row["current_tag"], latest=latest),
        )
        update.agents.append(agent)

    releases_cache: dict[str, list[dict[str, Any]] | None] = {}
    for update in pending.values():
        if update.agent_type not in releases_cache:
            releases_cache[update.agent_type] = outdated_cmd.fetch_releases(update.agent_type)
        _attach_releases(update, releases_cache[update.agent_type] or [])
    return list(pending.values()), reached


def _attach_releases(update: Update, releases: list[dict[str, Any]]) -> None:
    """Fill ``update.releases`` with the stable releases after ``current``
    up to ``latest``, in GitHub's newest-created order.

    Positional, not version-ordered (see the module docstring). Upstreams
    that cut maintenance releases of older lines after newer ones
    (OpenClaw) may contribute such a release to the range — it is shown
    under its own tag, which is harmless.
    """
    tagged = [
        (image_tag, r)
        for r in releases
        if not r.get("draft")
        and not r.get("prerelease")
        and isinstance(r.get("tag_name"), str)
        and (image_tag := outdated_cmd.image_tag_from_release(update.agent_type, r["tag_name"]))
    ]
    tags = [t for t, _ in tagged]
    if update.latest not in tags:
        update.complete = False
        return
    start = tags.index(update.latest)
    end = tags.index(update.current, start) if update.current in tags[start:] else None
    chain = [r for _, r in tagged[start:end]]
    update.complete = end is not None and len(chain) <= MAX_RELEASES
    update.releases = chain[:MAX_RELEASES]
    repo = outdated_cmd.html_repo_url(update.agent_type)
    if end is not None and repo:
        update.compare_url = (
            f"{repo}/compare/{tagged[end][1]['tag_name']}...{tagged[start][1]['tag_name']}"
        )


# ---------- mail ----------


def mail_subject(updates: list[Update]) -> str:
    """ASCII-only, names the agents and versions."""
    if len(updates) == 1:
        u = updates[0]
        return f"[agents] update available: {', '.join(u.agents)} {u.current} -> {u.latest}"
    agents = sorted(a for u in updates for a in u.agents)
    return f"[agents] updates available: {', '.join(agents)}"


def mail_body(cfg: cfg_mod.AgentsConfig, updates: list[Update], when: str) -> str:
    lines = [
        f"agx on VM {cfg.vm.name}: upstream update(s) available",
        f"checked:  {when}",
    ]
    for u in updates:
        title = f"{', '.join(u.agents)} ({u.agent_type}): {u.current} -> {u.latest}"
        lines += ["", title, "=" * len(title), ""]
        lines += [f"    upgrade:  agx upgrade {agent}" for agent in u.agents]
        if u.compare_url:
            lines += [f"    changes:  {u.compare_url}"]
        for rel in u.releases:
            published = str(rel.get("published_at") or "")[:10]
            heading = f"{rel['tag_name']} ({published})" if published else str(rel["tag_name"])
            lines += ["", f"  {heading}", f"  {'-' * len(heading)}"]
            for para in condense(str(rel.get("body") or "")):
                lines += wrap_text(para, "    ")
            if rel.get("html_url"):
                lines += ["", f"    full notes: {rel['html_url']}"]
        if not u.complete:
            lines += [
                "",
                "    (older releases in this range are not listed — see the changes link)"
                if u.compare_url
                else "    (the release list could not be matched to the pinned tag)",
            ]
    lines += [
        "",
        "-- ",
        "One mail per new release. `agx outdated` shows the current state;",
        "[doctor_cron].update_check_hours sets the check interval (0 = off).",
    ]
    return "\n".join(lines) + "\n"


def wrap_text(para: str, indent: str, width: int = MAIL_WIDTH) -> list[str]:
    """Wrap one paragraph for a plain-text mail; ``- item`` lines get a
    hanging indent. An empty paragraph is a blank line."""
    if not para.strip():
        return [""]
    return textwrap.wrap(
        para,
        width=width,
        initial_indent=indent,
        subsequent_indent=indent + ("  " if para.startswith("- ") else ""),
        break_on_hyphens=False,
        break_long_words=False,  # never split a URL or a long identifier
    )


# ---------- release-notes condensing ----------


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_METADATA_RE = re.compile(r"^\*\*[^*]+:\*\*")  # "**Release Date:** …", "**Since v0.22.0:** …"
# Rules (---), table rows, code fences: never part of the lead prose.
_NON_PROSE_RE = re.compile(r"^\s*(?:([-*_])(?:\s*\1){2,}\s*$|\||```|~~~)")
_VERSIONISH_RE = re.compile(r"(?:^|\s)v?\d+(\.\d+)+\b")  # a release-title heading


def condense(body: str) -> list[str]:
    """Markdown release notes → short plain paragraphs (``""`` = blank
    line, ``- …`` = bullet): the lead, then the full bullets of
    upgrade/breaking sections and the titles of the highlights."""
    sections = _sections(re.sub(r"<!--.*?-->", "", body, flags=re.S))

    out: list[str] = []
    lead = " ".join(
        _plain(line.lstrip("> "))
        for heading, level, lines in sections
        if heading is None or level == 1 or _VERSIONISH_RE.search(heading)
        for line in lines
        if line.strip()
        and not _BULLET_RE.match(line)
        and not _METADATA_RE.match(line.strip())
        and not _NON_PROSE_RE.match(line)
    ).strip()
    if lead:
        out.append(_truncate(lead, LEAD_MAX_CHARS))

    for heading, _level, lines in sections:
        if heading is None:
            continue
        name = re.sub(r"^\W+", "", heading)
        full = bool(_FULL_SECTIONS.search(name))
        if not full and not _TITLE_SECTIONS.search(name):
            continue
        bullets = [m.group(1) for line in lines if (m := _BULLET_RE.match(line))]
        if not bullets:
            continue
        out += ["", f"{name}:"]
        for raw in bullets[:SECTION_MAX_BULLETS]:
            text = _truncate(_plain(raw), NOTE_MAX_CHARS) if full else _bullet_title(raw)
            out.append(f"- {text}")
        if len(bullets) > SECTION_MAX_BULLETS:
            out.append(f"- (+{len(bullets) - SECTION_MAX_BULLETS} more)")
    return out


def _sections(text: str) -> list[tuple[str | None, int, list[str]]]:
    """Split Markdown into (heading text, level, body lines); text
    before the first heading has heading ``None``."""
    sections: list[tuple[str | None, int, list[str]]] = [(None, 0, [])]
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            sections.append((_plain(m.group(2)), len(m.group(1)), []))
        else:
            sections[-1][2].append(line)
    return sections


def _bullet_title(raw: str) -> str:
    """A highlight's bold lead-in (``**Title** — …``), else its first sentence."""
    m = re.match(r"\*\*(.+?)\*\*", raw.strip())
    if m:
        return _truncate(_plain(m.group(1)).rstrip(" .:—-"), TITLE_MAX_CHARS)
    text = _plain(raw)
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return _truncate(first, TITLE_MAX_CHARS)


def _plain(text: str) -> str:
    """Strip Markdown markup and contributor/PR credits from one line."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # links → their text
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)  # bold
    text = re.sub(r"\s*\(#\d+[^()]*\)", "", text)  # "(#123, #456 — @a, @b)"
    text = re.sub(r"\s*\bFixes #\d+(?:,\s*#\d+)*\.?", "", text)  # OpenClaw credit tails
    text = re.sub(r"\s*\bThanks @.*$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, limit: int) -> str:
    """Cut at a sentence (else word) boundary within ``limit`` chars."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sentence = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence > limit // 2:
        return cut[: sentence + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + " …"
