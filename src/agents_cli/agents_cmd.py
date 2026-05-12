"""Agent CRUD: create, list, remove, rename.

Every multi-phase op writes a journal and re-running the command resumes
from the last completed phase. Each phase is independently idempotent
(checks whether its work is already done before redoing it), so the
journal records progress without making the implementation rely on
exactly-once execution.

Service-name conventions:
- Hermes agents become compose service ``hermes-<name>``.
- OpenClaw agents become compose service ``openclaw-<name>``.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable, Iterator
from pathlib import Path

from agents_cli import compose, compose_runner, mp, ui
from agents_cli import config as cfg_mod
from agents_cli.compose import HERMES_IMAGE_BASE, OPENCLAW_IMAGE_BASE
from agents_cli.config import AgentsConfig
from agents_cli.errors import UserError
from agents_cli.journal import Journal

AGENT_TYPES = ("hermes", "openclaw")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")


# ---------- helpers ----------


def validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise UserError(
            f"invalid agent name {name!r}; use lowercase letters, digits, _ or - "
            "(start with a letter or digit, 1-50 chars)"
        )


def service_name(name: str, agent_type: str) -> str:
    if agent_type == "hermes":
        return f"hermes-{name}"
    if agent_type == "openclaw":
        return f"openclaw-{name}"
    raise UserError(f"unknown agent type {agent_type!r}; expected one of {AGENT_TYPES}")


def _agent_type_from_service(service_name_value: str) -> str | None:
    if service_name_value.startswith("hermes-"):
        return "hermes"
    if service_name_value.startswith("openclaw-"):
        return "openclaw"
    return None


def _name_from_service(service_name_value: str, agent_type: str) -> str:
    return service_name_value[len(agent_type) + 1 :]


def _agent_data_subpaths(agent_type: str) -> list[str]:
    """Per-agent subdirectory names relative to the agent's root dir."""
    if agent_type == "hermes":
        return [".data"]
    if agent_type == "openclaw":
        return [".openclaw", "workspace"]
    return []


def _agent_data_paths(cfg: AgentsConfig, name: str, agent_type: str) -> list[Path]:
    """Per-agent directories that should exist after `data_dir_created`
    (host-side; valid only in host-mount mode)."""
    root = cfg.paths.host_data_dir / name
    return [root, *(root / sub for sub in _agent_data_subpaths(agent_type))]


def _vm_agent_data_paths(cfg: AgentsConfig, name: str, agent_type: str) -> list[str]:
    """Per-agent directories that should exist after `data_dir_created`
    (VM-side; the canonical layout in ``--data-dir-in-vm`` mode)."""
    root = f"{cfg.paths.vm_agents_root}/{name}"
    return [root, *(f"{root}/{sub}" for sub in _agent_data_subpaths(agent_type))]


def _data_dir_exists(cfg: AgentsConfig, name: str, agent_type: str) -> bool:
    if cfg.paths.data_dir_in_vm:
        result = mp.exec_in_vm(
            cfg.vm.name,
            "test",
            "-d",
            f"{cfg.paths.vm_agents_root}/{name}",
            check=False,
        )
        return result.returncode == 0
    return any(p.exists() for p in _agent_data_paths(cfg, name, agent_type))


def _service_in_compose(cfg: AgentsConfig, service_name_value: str) -> bool:
    doc = compose.read_compose_doc(cfg)
    return service_name_value in (doc.get("services") or {})


def _has_live_container(cfg: AgentsConfig, service_name_value: str) -> bool:
    """Return True if a running container with ``container_name = <service>``
    is currently up. Conservatively returns False on any docker error so
    that ``--reuse-data`` doesn't refuse based on a docker hiccup."""
    result = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "ps",
        "--filter",
        f"name=^{service_name_value}$",
        "--format",
        "{{.Names}}",
        check=False,
    )
    if result.returncode != 0:
        return False
    names = {ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()}
    return service_name_value in names


# ---------- create ----------


CREATE_PHASES = ["data_dir_created", "compose_updated", "service_up", "setup_done"]


def create(
    *,
    name: str,
    agent_type: str,
    port: int | None = None,
    image_tag: str | None = None,
    run_setup: bool = False,
    reuse_data: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    validate_name(name)
    if agent_type not in AGENT_TYPES:
        raise UserError(f"unknown agent type {agent_type!r}; expected one of {AGENT_TYPES}")

    cfg = cfg_mod.load(config_path)
    svc = service_name(name, agent_type)

    # Decide tag (cheap, no race).
    tag = image_tag or (
        cfg.defaults.hermes_image_tag if agent_type == "hermes" else cfg.defaults.openclaw_image_tag
    )
    if not tag:
        raise UserError(
            f"no pinned {agent_type} image tag in config; re-run `agents vm init` "
            "or pass --image-tag"
        )

    required = ["data_dir_created", "compose_updated", "service_up"]
    if run_setup:
        required.append("setup_done")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        # Everything that observes or mutates compose / data-dir state must
        # be inside the lock, including port allocation (which scans the
        # live compose) and the --reuse-data live-container check.
        if port is None:
            base = (
                cfg.defaults.hermes_base_port
                if agent_type == "hermes"
                else cfg.defaults.openclaw_base_port
            )
            port = compose.next_free_port(cfg, base=base)

        if _data_dir_exists(cfg, name, agent_type):
            if not reuse_data:
                if _service_in_compose(cfg, svc):
                    raise UserError(
                        f"agent {name!r} already exists. "
                        f"To re-create, run `agents remove {name}` first."
                    )
                raise UserError(
                    f"data directory for {name!r} already exists at "
                    f"{cfg.paths.host_data_dir / name}. Pass --reuse-data to attach to it, "
                    f"or pick a different name."
                )
            # --reuse-data path: refuse if a live container is currently mapping
            # the dir, regardless of compose state. Plan rule #1.
            if _has_live_container(cfg, svc):
                raise UserError(
                    f"refusing --reuse-data: container {svc!r} is currently running and maps "
                    f"the data dir. Stop it first with `agents stop {name}`."
                )

        journal = Journal.find_resumable(cfg.paths.host_data_dir, "create", name) or Journal.begin(
            cfg.paths.host_data_dir, "create", name, CREATE_PHASES
        )

        for phase in required:
            if journal.is_phase_done(phase):
                continue
            if phase == "data_dir_created":
                _create_data_dirs(cfg, name, agent_type)
            elif phase == "compose_updated":
                _add_service_to_compose(cfg, name, agent_type, svc, port, tag)
            elif phase == "service_up":
                compose_runner.up(cfg, svc)
            elif phase == "setup_done":
                _run_agent_setup(cfg, svc, agent_type)
            journal.advance(phase)

        journal.complete()

    ui.emit(
        {
            "name": name,
            "type": agent_type,
            "service": svc,
            "port": port,
            "image_tag": tag,
            "data_dir": str(cfg.paths.host_data_dir / name),
        },
        human=(
            f"created {agent_type} agent {name!r}\n"
            f"  service: {svc}\n"
            f"  port: 127.0.0.1:{port}\n"
            f"  image: {_image_base(agent_type)}:{tag}\n"
            f"  data: {cfg.paths.host_data_dir / name}\n"
            + ("  setup wizard ran" if run_setup else f"  run `agents setup {name}` when ready")
        ),
    )


def _create_data_dirs(cfg: AgentsConfig, name: str, agent_type: str) -> None:
    if cfg.paths.data_dir_in_vm:
        mp.exec_in_vm(
            cfg.vm.name,
            "mkdir",
            "-p",
            *_vm_agent_data_paths(cfg, name, agent_type),
        )
        return
    for p in _agent_data_paths(cfg, name, agent_type):
        p.mkdir(parents=True, exist_ok=True)


def _delete_data_dir(cfg: AgentsConfig, name: str) -> None:
    if cfg.paths.data_dir_in_vm:
        mp.exec_in_vm(
            cfg.vm.name,
            "rm",
            "-rf",
            f"{cfg.paths.vm_agents_root}/{name}",
            check=False,
        )
        return
    target = cfg.paths.host_data_dir / name
    if target.exists():
        shutil.rmtree(target)


def _add_service_to_compose(
    cfg: AgentsConfig,
    name: str,
    agent_type: str,
    svc: str,
    port: int,
    tag: str,
) -> None:
    doc = compose.read_compose_doc(cfg)
    if svc in (doc.get("services") or {}):
        return  # already there from a prior partial run
    if agent_type == "hermes":
        fragment = compose.hermes_service_fragment(name=name, cfg=cfg, port=port, image_tag=tag)
    else:
        fragment = compose.openclaw_service_fragment(name=name, port=port, image_tag=tag)
    compose.add_service(doc, svc, fragment)
    compose.write_compose_atomically(cfg, doc)


def _run_agent_setup(cfg: AgentsConfig, svc: str, agent_type: str) -> None:
    """Drop the user into the agent's setup wizard interactively.

    Hermes:   `hermes setup`
    OpenClaw: `openclaw onboard`
    """
    binary = "hermes" if agent_type == "hermes" else "openclaw"
    sub = "setup" if agent_type == "hermes" else "onboard"
    from agents_cli import mp

    mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "compose",
        "--project-directory",
        str(cfg.paths.vm_agents_root),
        "-f",
        f"{cfg.paths.vm_agents_root}/docker-compose.yml",
        "--env-file",
        f"{cfg.paths.vm_agents_root}/.env",
        "exec",
        svc,
        binary,
        sub,
    )


def _image_base(agent_type: str) -> str:
    return HERMES_IMAGE_BASE if agent_type == "hermes" else OPENCLAW_IMAGE_BASE


# ---------- list ----------


def list_agents(*, config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> list[dict[str, object]]:
    cfg = cfg_mod.load(config_path)
    doc = compose.read_compose_doc(cfg)
    rows: list[dict[str, object]] = []
    for svc_name, svc in (doc.get("services") or {}).items():
        agent_type = _agent_type_from_service(svc_name)
        if agent_type is None:
            continue
        rows.append(
            {
                "name": _name_from_service(svc_name, agent_type),
                "type": agent_type,
                "service": svc_name,
                "image": svc.get("image", ""),
                "ports": ", ".join(svc.get("ports", []) or []),
            }
        )
    ui.table(rows, ["name", "type", "service", "image", "ports"])
    return rows


# ---------- remove ----------


REMOVE_PHASES = ["container_stopped", "compose_updated", "data_dir_deleted"]


def remove(
    name: str,
    *,
    agent_type: str | None = None,
    yes: bool = False,
    delete_data: bool = True,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    validate_name(name)
    cfg = cfg_mod.load(config_path)

    agent_type = agent_type or _detect_type_from_compose(cfg, name)
    if agent_type is None:
        raise UserError(f"agent {name!r} not found in compose")
    svc = service_name(name, agent_type)

    confirm_msg = f"remove agent {name!r}"
    if delete_data:
        confirm_msg += f" and its data dir at {cfg.paths.host_data_dir / name}"
    confirm_msg += "?"
    if not yes and not ui.confirm(confirm_msg, default=False):
        raise UserError("aborted by user")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        journal = Journal.find_resumable(cfg.paths.host_data_dir, "remove", name) or Journal.begin(
            cfg.paths.host_data_dir, "remove", name, REMOVE_PHASES
        )

        for phase in _phases_to_run(journal):
            if phase == "container_stopped":
                if _service_in_compose(cfg, svc):
                    # `compose stop` + `compose rm -fs <svc>` is service-scoped;
                    # `compose down <svc>` is version-dependent and historically
                    # has brought the whole project down regardless of args.
                    compose_runner.stop(cfg, svc)
                    compose_runner.rm(cfg, svc)
                journal.advance(phase)
            elif phase == "compose_updated":
                doc = compose.read_compose_doc(cfg)
                if svc in (doc.get("services") or {}):
                    compose.remove_service(doc, svc)
                    compose.write_compose_atomically(cfg, doc)
                journal.advance(phase)
            elif phase == "data_dir_deleted":
                if delete_data:
                    _delete_data_dir(cfg, name)
                journal.advance(phase)

        journal.complete()

    ui.emit(
        {"name": name, "type": agent_type, "status": "removed"},
        human=f"removed agent {name!r}",
    )


def _detect_type_from_compose(cfg: AgentsConfig, name: str) -> str | None:
    doc = compose.read_compose_doc(cfg)
    services = doc.get("services") or {}
    for at in AGENT_TYPES:
        if service_name(name, at) in services:
            return at
    return None


# ---------- rename ----------


RENAME_PHASES = ["compose_updated", "dir_renamed", "container_recreated"]


def rename(
    old_name: str,
    new_name: str,
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    validate_name(old_name)
    validate_name(new_name)
    cfg = cfg_mod.load(config_path)

    agent_type = _detect_type_from_compose(cfg, old_name)
    if agent_type is None:
        raise UserError(f"agent {old_name!r} not in compose")
    if _detect_type_from_compose(cfg, new_name) is not None:
        raise UserError(f"agent {new_name!r} already exists")

    old_svc = service_name(old_name, agent_type)
    new_svc = service_name(new_name, agent_type)
    old_dir = cfg.paths.host_data_dir / old_name
    new_dir = cfg.paths.host_data_dir / new_name

    with compose.acquire_lock(cfg.paths.host_data_dir):
        journal = Journal.find_resumable(
            cfg.paths.host_data_dir, "rename", old_name
        ) or Journal.begin(cfg.paths.host_data_dir, "rename", old_name, RENAME_PHASES)

        for phase in _phases_to_run(journal):
            if phase == "compose_updated":
                doc = compose.read_compose_doc(cfg)
                if old_svc in (doc.get("services") or {}):
                    compose.rename_service(doc, old_svc, new_svc)
                    # Also update container_name and any path references that mention old_name.
                    _rewrite_service_paths(doc["services"][new_svc], old_name, new_name)
                    compose.write_compose_atomically(cfg, doc)
                journal.advance(phase)
            elif phase == "dir_renamed":
                if cfg.paths.data_dir_in_vm:
                    mp.exec_bash(
                        cfg.vm.name,
                        f"set -eu\n"
                        f"if [ -d {cfg.paths.vm_agents_root}/{old_name} ] && "
                        f"[ ! -d {cfg.paths.vm_agents_root}/{new_name} ]; then "
                        f"mv {cfg.paths.vm_agents_root}/{old_name} "
                        f"{cfg.paths.vm_agents_root}/{new_name}; fi\n",
                        check=False,
                    )
                elif old_dir.exists() and not new_dir.exists():
                    old_dir.rename(new_dir)
                journal.advance(phase)
            elif phase == "container_recreated":
                # Old container's container_name no longer matches anything in
                # compose; remove it explicitly so it doesn't linger. Then
                # bring up the renamed service.
                old_container = f"{agent_type}-{old_name}"
                mp.exec_in_vm(cfg.vm.name, "docker", "rm", "-f", old_container, check=False)
                compose_runner.up(cfg, new_svc)
                journal.advance(phase)

        journal.complete()

    ui.emit(
        {"old": old_name, "new": new_name, "type": agent_type, "status": "renamed"},
        human=f"renamed {old_name!r} -> {new_name!r}",
    )


def _rewrite_service_paths(service: dict[str, object], old_name: str, new_name: str) -> None:
    """Update container_name and any `./<old>/...` volume entries in place."""
    container_name = service.get("container_name")
    if isinstance(container_name, str):
        service["container_name"] = container_name.replace(old_name, new_name)
    volumes = service.get("volumes")
    if isinstance(volumes, Iterable):
        new_volumes: list[object] = []
        for v in volumes:
            if isinstance(v, str) and v.startswith(f"./{old_name}/"):
                new_volumes.append(f"./{new_name}/" + v[len(f"./{old_name}/") :])
            else:
                new_volumes.append(v)
        service["volumes"] = new_volumes


# ---------- shared journal walker ----------


def _phases_to_run(journal: Journal) -> Iterator[str]:
    """Yield each phase that has not yet been completed, in declared order."""
    for phase in journal.phases:
        if phase not in journal.completed_phases:
            yield phase
