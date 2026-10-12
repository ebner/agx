"""Retention for upgrade artifacts: tarballs, VM snapshots, image tags.

Every ``agx upgrade`` leaves three artifacts behind, and none of
them expired before this module existed:

- a host tarball at ``<data-dir>/.upgrades/<name>-<utc>.tar.zst``
- a ``pre-upgrade-<name>-<utc>`` VM snapshot (bloats the qcow2 /
  multipass image on disk)
- the superseded image tag in the VM's Docker cache (~4-9 GB per
  Hermes release — eight of these filled a 58 GB VM disk to 100% in
  ~3 months of routine upgrades; the 2026-08 outage class doctor's
  ``vm_disk`` check now watches for)

Retention is counted in upgrade *generations*, not age: ``agx
rollback`` only ever uses the newest generation, and age-based expiry
could delete the only rollback point of an agent that upgrades
rarely. Keeping the newest N guarantees rollback for the last N
upgrades regardless of cadence.

Two entry points:

- :func:`prune` — the ``agx prune`` command. Takes the flock,
  confirms (``--yes`` skips), supports ``--dry-run``.
- :func:`auto_prune_after_upgrade` — called by ``upgrades.upgrade``
  right after ``journal.complete()``, inside the lock the upgrade
  already holds (the flock is not re-entrant — a second acquire in
  the same process deadlocks). Best-effort by contract: the caller
  wraps it so a prune hiccup can never fail an upgrade that landed.

Image pruning is repo-scoped, not per-agent — two Hermes agents share
``nousresearch/hermes-agent`` — so a tag is deletable only when NO
compose service references it AND it isn't the ``defaults.*_image_tag``
baseline for future ``agx create``. Only the agent-image repos
(hermes/openclaw) are ever touched: firecrawl/ollama/openviking images
are digest-pinned sidecars owned by their own up/down commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agx_cli import compose, mp, ui
from agx_cli import config as cfg_mod
from agx_cli.compose import HERMES_IMAGE_BASE, OPENCLAW_IMAGE_BASE
from agx_cli.config import AgentsConfig
from agx_cli.errors import UserError

# Manual `agx prune` fallback when the config knob is 0: a zero
# there means "no automatic prune", not "a manual prune should delete
# every rollback point".
DEFAULT_KEEP = 2


@dataclass
class PrunePlan:
    keep: int
    # Host tarballs under .upgrades/ (absolute paths).
    archives: list[Path] = field(default_factory=list)
    # VM snapshot names.
    snapshots: list[str] = field(default_factory=list)
    # Full image refs (repo:tag) in the VM's Docker cache.
    images: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.archives or self.snapshots or self.images)


def _agents_in_compose(cfg: AgentsConfig) -> list[tuple[str, str]]:
    """(agent_name, agent_type) for every toolkit-owned compose service."""
    from agx_cli.ops import _services_to_check

    return [
        (agent_name, agent_type) for _svc, agent_type, agent_name in _services_to_check(cfg, None)
    ]


def _plan_archives(cfg: AgentsConfig, name: str, keep: int) -> list[Path]:
    from agx_cli.upgrades import _host_archive_re

    upgrades_dir = cfg.paths.host_data_dir / ".upgrades"
    if not upgrades_dir.is_dir():
        return []
    pattern = _host_archive_re(name)
    candidates = sorted(p for p in upgrades_dir.iterdir() if pattern.fullmatch(p.name))
    return candidates[:-keep] if keep else candidates


def _list_vm_snapshots(cfg: AgentsConfig) -> list[str]:
    import json

    result = mp.info_vm(cfg.vm.name, fmt="json")
    if result.returncode != 0:
        return []
    try:
        info = json.loads(result.stdout)
    except ValueError:
        return []
    snapshots = info.get("info", {}).get(cfg.vm.name, {}).get("snapshots", {})
    return sorted(snapshots)


def _plan_snapshots(cfg: AgentsConfig, name: str, keep: int) -> list[str]:
    from agx_cli.upgrades import _vm_snapshot_re

    pattern = _vm_snapshot_re(name)
    candidates = [s for s in _list_vm_snapshots(cfg) if pattern.fullmatch(s)]
    return candidates[:-keep] if keep else candidates


def _referenced_tags(cfg: AgentsConfig, repo: str) -> set[str]:
    """Tags of ``repo`` that anything in compose still points at, plus
    the ``agx create`` baseline tag from ``[defaults]``."""
    doc = compose.read_compose_doc(cfg)
    tags: set[str] = set()
    prefix = repo + ":"
    for svc in (doc.get("services") or {}).values():
        image = str(svc.get("image", "") or "")
        if image.startswith(prefix):
            tags.add(image[len(prefix) :])
    if repo == HERMES_IMAGE_BASE:
        tags.add(cfg.defaults.hermes_image_tag)
    elif repo == OPENCLAW_IMAGE_BASE:
        tags.add(cfg.defaults.openclaw_image_tag)
    tags.discard("")
    return tags


def _plan_images(cfg: AgentsConfig, repos: set[str], keep: int) -> list[str]:
    """Superseded agent-image tags: everything in the VM's cache for the
    given repos except the ``keep`` newest tags (by CreatedAt, whatever
    they are), compose-referenced tags, and the defaults baseline.

    "Newest ``keep``" rather than "referenced + ``keep - 1`` spares":
    the auto-prune hook runs before ``_persist_default_tag`` bumps the
    ``agx create`` baseline, so during a ``--latest`` upgrade the
    outgoing tag is still the defaults tag. Counting it as a protected
    extra on top of the spares retained one image more than configured
    on every upgrade (codex review 2026-08-24); counting newest-N over
    all tags keeps exactly ``keep`` generations in the steady state
    while referenced/baseline tags still always survive.
    """
    doomed: list[str] = []
    for repo in sorted(repos):
        result = mp.exec_in_vm(
            cfg.vm.name,
            "docker",
            "images",
            repo,
            "--format",
            "{{.Tag}}\t{{.CreatedAt}}",
            check=False,
        )
        if result.returncode != 0:
            ui.warn(f"could not list {repo} images (VM down?) — skipping image prune")
            continue
        protected = _referenced_tags(cfg, repo)
        tagged: list[tuple[str, str]] = []  # (created_at, tag)
        for line in result.stdout.splitlines():
            parts = line.strip().split("\t")
            if len(parts) != 2:
                continue
            tag, created = parts
            if tag == "<none>":
                continue
            tagged.append((created, tag))
        # CreatedAt is docker's fixed "YYYY-MM-DD HH:MM:SS +ZZZZ TZ"
        # form — lexicographic sort orders it chronologically.
        tagged.sort()
        newest = {tag for _created, tag in tagged[-keep:]} if keep else set()
        doomed.extend(
            f"{repo}:{tag}"
            for _created, tag in tagged
            if tag not in protected and tag not in newest
        )
    return doomed


def build_plan(cfg: AgentsConfig, name: str | None, keep: int) -> PrunePlan:
    """Read-only: compute what a prune with this retention would delete."""
    agents = _agents_in_compose(cfg)
    if name is not None:
        agents = [(n, t) for n, t in agents if n == name]
        if not agents:
            raise UserError(f"agent {name!r} not in compose")
    plan = PrunePlan(keep=keep)
    for agent_name, _agent_type in agents:
        plan.archives.extend(_plan_archives(cfg, agent_name, keep))
        plan.snapshots.extend(_plan_snapshots(cfg, agent_name, keep))
    plan.images = _plan_images(cfg, {_repo_for(t) for _n, t in agents}, keep)
    return plan


def _repo_for(agent_type: str) -> str:
    return HERMES_IMAGE_BASE if agent_type == "hermes" else OPENCLAW_IMAGE_BASE


@dataclass
class PruneResult:
    keep: int
    archives: list[str] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    archive_bytes_freed: int = 0

    def payload(self) -> dict[str, Any]:
        return {
            "keep": self.keep,
            "deleted": {
                "archives": self.archives,
                "snapshots": self.snapshots,
                "images": self.images,
            },
            "failed": self.failed,
            "archive_bytes_freed": self.archive_bytes_freed,
        }


def execute_plan(cfg: AgentsConfig, plan: PrunePlan) -> PruneResult:
    """Delete everything in the plan. Caller holds ``.agents.lock``.

    Per-item failures are collected, never raised — a snapshot the
    backend refuses to drop must not strand the archive/image half of
    the cleanup.
    """
    result = PruneResult(keep=plan.keep)

    for archive in plan.archives:
        try:
            size = archive.stat().st_size
            archive.unlink()
        except OSError as e:
            result.failed.append({"kind": "archive", "ref": str(archive), "error": str(e)})
        else:
            result.archive_bytes_freed += size
            result.archives.append(archive.name)

    for snap in plan.snapshots:
        res = mp.delete_snapshot(cfg.vm.name, snap)
        if res.returncode != 0:
            result.failed.append({"kind": "snapshot", "ref": snap, "error": res.stderr.strip()})
        else:
            result.snapshots.append(snap)

    for image in plan.images:
        res = mp.exec_in_vm(cfg.vm.name, "docker", "rmi", image, check=False)
        if res.returncode != 0:
            result.failed.append({"kind": "image", "ref": image, "error": res.stderr.strip()})
        else:
            result.images.append(image)

    return result


def auto_prune_after_upgrade(cfg: AgentsConfig, name: str) -> PruneResult | None:
    """Post-upgrade retention hook. Runs inside the upgrade's flock —
    do NOT acquire the lock here (flock isn't re-entrant in-process).
    Returns the execute result, or None when disabled in config."""
    keep = cfg.upgrades.keep_generations
    if keep <= 0:
        return None
    plan = build_plan(cfg, name, keep)
    if plan.empty:
        return PruneResult(keep=keep)
    return execute_plan(cfg, plan)


def _plan_payload(plan: PrunePlan) -> dict[str, object]:
    return {
        "keep": plan.keep,
        "archives": [p.name for p in plan.archives],
        "snapshots": plan.snapshots,
        "images": plan.images,
    }


def _describe(plan: PrunePlan) -> str:
    lines = [f"prune plan (keeping newest {plan.keep} generation(s)):"]
    for label, items in (
        ("host tarballs", [p.name for p in plan.archives]),
        ("VM snapshots", plan.snapshots),
        ("image tags", plan.images),
    ):
        lines.append(f"  {label}: {len(items)}")
        lines.extend(f"    - {item}" for item in items)
    return "\n".join(lines)


def prune(
    name: str | None = None,
    *,
    keep: int | None = None,
    dry_run: bool = False,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    """``agx prune [name] [--keep N] [--dry-run]``."""
    cfg = cfg_mod.load(config_path)
    effective_keep = keep if keep is not None else (cfg.upgrades.keep_generations or DEFAULT_KEEP)

    if dry_run:
        plan = build_plan(cfg, name, effective_keep)
        ui.emit(
            {"dry_run": True, **_plan_payload(plan)},
            human=_describe(plan) + "\n(dry run — nothing deleted)",
        )
        return

    with compose.acquire_lock(cfg.paths.host_data_dir):
        # Built under the lock so a parallel upgrade can't add or
        # consume artifacts between planning and deletion.
        plan = build_plan(cfg, name, effective_keep)
        if plan.empty:
            ui.emit(
                {"dry_run": False, **PruneResult(keep=effective_keep).payload()},
                human=f"nothing to prune (keeping newest {effective_keep} generation(s))",
            )
            return
        if not yes:
            ui.info(_describe(plan))
            total = len(plan.archives) + len(plan.snapshots) + len(plan.images)
            if not ui.confirm(f"delete these {total} artifact(s)?"):
                raise UserError("prune aborted — pass --yes to skip the prompt")
        result = execute_plan(cfg, plan)

    human = (
        f"pruned: {len(result.archives)} tarball(s) "
        f"({_fmt_size(result.archive_bytes_freed)}), "
        f"{len(result.snapshots)} VM snapshot(s), "
        f"{len(result.images)} image tag(s)"
    )
    if result.failed:
        human += "\nfailed:\n" + "\n".join(
            f"  - {f['kind']} {f['ref']}: {f['error']}" for f in result.failed
        )
    ui.emit({"dry_run": False, **result.payload()}, human=human)


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"
