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
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

from agents_cli import compose, compose_runner, mp, permissions, ui
from agents_cli import config as cfg_mod
from agents_cli.compose import HERMES_IMAGE_BASE, OPENCLAW_IMAGE_BASE
from agents_cli.config import AgentsConfig
from agents_cli.errors import RuntimeFailure, UserError
from agents_cli.journal import Journal

AGENT_TYPES = ("hermes", "openclaw")
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,49}")

# Top-level dirs under the data dir that belong to project-scoped
# features, not to an agent. An agent with one of these names would
# share its directory with that feature — and `agents remove` deletes
# the agent dir as root inside the VM, so `create --reuse-data firecrawl`
# followed by `remove` would take the postgres data with it.
RESERVED_NAMES = frozenset({"backups", "firecrawl", "models"})


# ---------- helpers ----------


def validate_name(name: str) -> None:
    # fullmatch, not match + ``$``: ``$`` also matches before a trailing
    # newline, and names are interpolated into VM-side paths.
    if not _NAME_RE.fullmatch(name):
        raise UserError(
            f"invalid agent name {name!r}; use lowercase letters, digits, _ or - "
            "(start with a letter or digit, 1-50 chars)"
        )


def refuse_reserved_name(name: str) -> None:
    if name in RESERVED_NAMES:
        raise UserError(
            f"agent name {name!r} is reserved — <data-dir>/{name}/ belongs to a "
            "project-scoped toolkit feature. Pick a different name."
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
    refuse_reserved_name(name)
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

        # A resumable journal is an explicit "this is a continuation, not a
        # fresh attempt" signal, so it bypasses the data-dir-exists refusal
        # below — that guard exists to protect orphaned dirs without a
        # journal, not to block the resume path the journal was built for.
        # The live-container guard still applies in both cases.
        resumable = Journal.find_resumable(cfg.paths.host_data_dir, "create", name)

        # An interrupted `agents remove` still owns this name: its journal
        # resumes at ``data_dir_deleted`` without re-running the stop
        # phase, so attaching new or reused data now would hand a live
        # agent's dir to a pending root-powered delete.
        if Journal.find_resumable(cfg.paths.host_data_dir, "remove", name) is not None:
            raise UserError(
                f"a previous `agents remove {name}` did not finish. Re-run it to "
                "completion (add --keep-data if the data should survive) before "
                "creating an agent with this name."
            )

        if _data_dir_exists(cfg, name, agent_type):
            if resumable is None and not reuse_data:
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
            # Both --reuse-data and resume paths must refuse if a live
            # container is currently mapping the dir. Plan rule #1.
            if _has_live_container(cfg, svc):
                raise UserError(
                    f"refusing to {'resume' if resumable else 'attach'}: "
                    f"container {svc!r} is currently running and maps the data dir. "
                    f"Stop it first with `agents stop {name}`."
                )

        journal = resumable or Journal.begin(cfg.paths.host_data_dir, "create", name, CREATE_PHASES)

        for phase in required:
            if journal.is_phase_done(phase):
                continue
            if phase == "data_dir_created":
                _create_data_dirs(cfg, name, agent_type)
            elif phase == "compose_updated":
                if agent_type == "hermes":
                    # Materialize the cont-init.d perms hook BEFORE writing the
                    # fragment that bind-mounts it, so the source always exists
                    # before any `compose up` — otherwise an interrupted/raced
                    # start lets Docker auto-create a directory at the mount
                    # path. The hook then re-applies the install-path chown on
                    # EVERY boot, not just toolkit-driven recreates.
                    compose.ensure_cont_init_script(cfg)
                _add_service_to_compose(cfg, name, agent_type, svc, port, tag)
            elif phase == "service_up":
                ui.progress(f"starting container {svc}...")
                compose_runner.up(cfg, svc)
                if agent_type == "hermes":
                    # Auto-chown the image-baked paths Hermes' setup
                    # wizard writes into (e.g. /opt/hermes/scripts/
                    # for bridge npm-installs, /home/hermes/ for npm
                    # cache) so first-run `hermes setup` doesn't hit
                    # EACCES on its install branches. Lost on every
                    # container recreate — re-applied by fix-perms
                    # and reseat. See fixperms_cmd.HERMES_WRITABLE_PATHS.
                    from agents_cli import fixperms_cmd

                    try:
                        huid = fixperms_cmd.container_hermes_uid(cfg, svc)
                    except RuntimeFailure:
                        huid = 1000  # best-effort; entrypoint usually maps to 1000
                    fixperms_cmd.chown_hermes_install_paths(cfg, svc, huid)
                    # If firecrawl is already up project-wide, auto-wire
                    # the new Hermes agent's terminal.env_passthrough so
                    # the LLM's sandboxed shell tool sees the existing
                    # FIRECRAWL_API_KEY / FIRECRAWL_API_URL env vars.
                    # No-op when firecrawl isn't installed.
                    from agents_cli import firecrawl_cmd

                    firecrawl_cmd.ensure_agent_wired(cfg, name)
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
        # Touch per-agent .env so the compose fragment's env_file: clause
        # validates even before the user has set any secrets.
        mp.exec_in_vm(
            cfg.vm.name,
            "touch",
            f"{cfg.paths.vm_agents_root}/{name}/.env",
        )
        return
    for p in _agent_data_paths(cfg, name, agent_type):
        p.mkdir(parents=True, exist_ok=True)
    env_path = cfg.paths.host_data_dir / name / ".env"
    if not env_path.exists():
        env_path.write_text("", encoding="utf-8")


def _refuse_if_host_view(cfg: AgentsConfig, name: str, action: str) -> None:
    # Lazy import: access_cmd imports this module.
    from agents_cli import access_cmd

    access_cmd.refuse_if_view_registered(cfg, name, action)


def _delete_data_dir(cfg: AgentsConfig, name: str, *, yes: bool = False) -> None:
    """Delete ``<data-dir>/<name>`` without traversing it as the host user.

    The runtime-owned subtree (``.data`` / ``.openclaw``) belongs to a
    rootless-Docker subuid under mode 700, so a host-side ``rmtree``
    fails — and host ACLs can't be relied on to change that (a
    ``mkdir(0700)`` under a default ACL is born with ``mask::---``).
    VM backends delete as root inside the VM instead: guest root acts as
    root on the share, same as the ``sudo tar`` export/snapshot paths.
    """
    validate_name(name)
    refuse_reserved_name(name)
    if cfg.vm.backend in ("multipass", "libvirt"):
        _delete_data_dir_in_vm(cfg, name)
        return
    _delete_data_dir_on_host(cfg, name, yes=yes)


def _delete_data_dir_in_vm(cfg: AgentsConfig, name: str) -> None:
    vm_root = str(cfg.paths.vm_agents_root)
    vm_target = f"{vm_root}/{name}"
    manual = f"sudo rm -rf {cfg.paths.host_data_dir / name}"
    if cfg.paths.data_dir_in_vm:
        manual = f"`agents vm shell`, then: sudo rm -rf {vm_target}"

    def fail(why: str) -> RuntimeFailure:
        return RuntimeFailure(
            f"could not delete {name!r}'s data dir: {why}. The removal journal stays "
            f"resumable: fix the cause (or delete manually: {manual}) and re-run "
            f"`agents remove {name}` to finish."
        )

    if not cfg.paths.data_dir_in_vm:
        # A detached share would make the VM-side path an empty stub:
        # ``rm -rf`` succeeds, the real host data survives, and the
        # journal would record a delete that never happened.
        mounted = mp.exec_in_vm(cfg.vm.name, "mountpoint", "-q", vm_root, check=False)
        if mounted.returncode != 0:
            raise fail(f"{vm_root} is not mounted in the VM (run `agents vm start`)")

    # ``--one-file-system`` does not stop at same-filesystem bind mounts,
    # so refuse outright if anything is mounted beneath the target.
    mounts = mp.exec_in_vm(cfg.vm.name, "findmnt", "-rno", "TARGET", check=False)
    nested = [
        line
        for line in (mounts.stdout or "").splitlines()
        if line == vm_target or line.startswith(vm_target + "/")
    ]
    if nested:
        raise fail(f"mounts are still active beneath it in the VM ({', '.join(nested)})")

    rm = mp.exec_in_vm(
        cfg.vm.name,
        "sudo",
        "rm",
        "-rf",
        "--one-file-system",
        "--preserve-root=all",
        "--",
        vm_target,
        check=False,
    )
    if rm.returncode != 0:
        raise fail((rm.stderr or "").strip() or f"rm exited {rm.returncode}")
    if not cfg.paths.data_dir_in_vm and (cfg.paths.host_data_dir / name).exists():
        raise fail("the VM-side delete reported success but the directory is still there")


def _delete_data_dir_on_host(cfg: AgentsConfig, name: str, *, yes: bool) -> None:
    """bare_metal: no VM to borrow root from, and the agent's files are
    subuid-owned here too (rootless Docker on the host). ``rootlesskit``
    enters a user namespace whose root owns the subuid range, so it can
    delete them without sudo; confirmed sudo is the fallback.
    """
    target = cfg.paths.host_data_dir / name
    if not target.exists():
        return
    try:
        shutil.rmtree(target)
        return
    except PermissionError:
        pass
    rootlesskit = shutil.which("rootlesskit")
    if rootlesskit:
        subprocess.run(
            [rootlesskit, "rm", "-rf", "--one-file-system", "--", str(target)],
            capture_output=True,
            check=False,
        )
    if target.exists():
        try:
            permissions.run_with_sudo(
                [["rm", "-rf", "--one-file-system", "--", str(target)]], yes=yes
            )
        except (UserError, RuntimeFailure) as exc:
            raise RuntimeFailure(
                f"could not delete {target} ({exc}). The removal journal stays "
                f"resumable: delete the directory manually with "
                f"`sudo rm -rf {target}` and re-run `agents remove {name}` to "
                "finish."
            ) from exc
    if target.exists():
        raise RuntimeFailure(
            f"could not delete {target}. The removal journal stays resumable: "
            f"delete it manually with `sudo rm -rf {target}` and re-run "
            f"`agents remove {name}` to finish."
        )


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
        _ensure_hermes_image_pulled(cfg, tag)
        fragment = compose.hermes_service_fragment(name=name, cfg=cfg, port=port, image_tag=tag)
    else:
        fragment = compose.openclaw_service_fragment(name=name, port=port, image_tag=tag)
    compose.add_service(doc, svc, fragment)
    compose.write_compose_atomically(cfg, doc)


def _ensure_hermes_image_pulled(cfg: AgentsConfig, tag: str) -> None:
    """Pull the Hermes image up front so `docker compose up` doesn't hit a
    network failure mid-mutation. We used to also verify the image's
    entrypoint USER/HOME, but the compose fragment now sets both
    `user:` and `HOME` explicitly — the runtime user is fully ours to
    own, so image drift in those fields doesn't break the Codex import
    mount. We just need the image local before compose up.
    """
    image_ref = f"{HERMES_IMAGE_BASE}:{tag}"
    inspect = mp.exec_in_vm(cfg.vm.name, "docker", "image", "inspect", image_ref, check=False)
    if inspect.returncode == 0:
        return
    ui.progress(f"pulling {image_ref} (multi-GB on first create — can take minutes)...")
    pull = mp.exec_in_vm(cfg.vm.name, "docker", "pull", image_ref, check=False)
    if pull.returncode != 0:
        raise UserError(
            f"could not pull {image_ref} (rc={pull.returncode}): "
            f"{pull.stderr.strip() or pull.stdout.strip()}"
        )
    ui.progress(f"pulled {image_ref}")


def _run_agent_setup(cfg: AgentsConfig, svc: str, agent_type: str) -> None:
    """Drop the user into the agent's setup wizard interactively.

    Hermes:   `hermes setup`
    OpenClaw: `openclaw onboard`

    Uses ``exec_in_vm_interactive`` so stdin/stdout/stderr inherit
    from the parent process. The non-interactive ``exec_in_vm`` was
    swallowing the wizard's prompts and reporting success — the user
    saw "setup wizard ran" but never got to answer any questions.
    """
    binary = "hermes" if agent_type == "hermes" else "openclaw"
    sub = "setup" if agent_type == "hermes" else "onboard"
    from agents_cli import mp
    from agents_cli.ops import _compose_exec_in_vm_argv

    rc = mp.exec_in_vm_interactive(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(cfg, svc, binary, sub),
    )
    if rc != 0:
        raise UserError(
            f"{binary} {sub} exited with rc={rc}. Re-run with `agents setup {_name_from_service(svc, agent_type)}`."
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


REMOVE_PHASES = ["mounts_torn_down", "container_stopped", "compose_updated", "data_dir_deleted"]


def removal_confirm_message(cfg: AgentsConfig, name: str, *, delete_data: bool) -> str:
    """Confirmation prompt for removing an agent.

    Shared by :func:`remove` and the CLI layer — ``agents remove
    --export-first`` hoists the prompt above the export so a declined
    removal never leaves a fresh tarball behind.
    """
    msg = f"remove agent {name!r}"
    if delete_data:
        msg += f" and its data dir at {cfg.paths.host_data_dir / name}"
    return msg + "?"


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
        # A prior removal may have died AFTER ``compose_updated`` (the
        # compose entry is gone) but BEFORE ``data_dir_deleted``.
        # Refusing with "not found" here would strand the open journal
        # forever; consult it before giving up. The pre-meta fallback
        # type is safe: a missing compose entry proves the
        # container-facing phases already completed, so from here the
        # type only labels the final emit.
        stranded = Journal.find_resumable(cfg.paths.host_data_dir, "remove", name)
        if stranded is None:
            raise UserError(f"agent {name!r} not found in compose")
        agent_type = str(stranded.meta.get("agent_type") or AGENT_TYPES[0])
    svc = service_name(name, agent_type)
    if delete_data:
        _refuse_if_host_view(cfg, name, f"remove {name!r}'s data")

    if not yes and not ui.confirm(
        removal_confirm_message(cfg, name, delete_data=delete_data), default=False
    ):
        raise UserError("aborted by user")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        # Again under the lock: `access mount` registers views under this
        # same lock, and one may have appeared while we sat at the prompt.
        if delete_data:
            _refuse_if_host_view(cfg, name, f"remove {name!r}'s data")
        journal = Journal.find_resumable(cfg.paths.host_data_dir, "remove", name) or Journal.begin(
            cfg.paths.host_data_dir, "remove", name, REMOVE_PHASES, meta={"agent_type": agent_type}
        )

        for phase in _phases_to_run(journal):
            if phase == "mounts_torn_down":
                # Wipe any host-folder mounts the user added via
                # `agents mount add` BEFORE stopping the container — the
                # container holds the bind mounts open, but the VM-side
                # mount needs the agent's compose-override entry gone
                # first to avoid `compose down`'s implicit re-up.
                from agents_cli import mounts_cmd

                mounts_cmd.remove_all_for_agent(cfg, name)
                journal.advance(phase)
            elif phase == "container_stopped":
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
                    _delete_data_dir(cfg, name, yes=yes)
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


# ``dir_renamed`` deliberately precedes ``compose_updated``: the rewritten
# service block points its env_file (and volumes) at ``./<new>/…``, and
# ``write_compose_atomically`` validates via ``docker compose config -q``,
# which errors on a missing env_file. The new dir must therefore exist
# before the compose write — the reverse order can never validate.
RENAME_PHASES = ["dir_renamed", "compose_updated", "container_recreated"]


def rename(
    old_name: str,
    new_name: str,
    *,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    validate_name(old_name)
    validate_name(new_name)
    cfg = cfg_mod.load(config_path)

    agent_type = _detect_type_from_compose(cfg, old_name)
    if agent_type is None:
        raise UserError(f"agent {old_name!r} not in compose")
    _refuse_if_host_view(cfg, old_name, f"rename {old_name!r}")
    if _detect_type_from_compose(cfg, new_name) is not None:
        raise UserError(f"agent {new_name!r} already exists")
    _refuse_rename_with_openviking_sidecar(cfg, old_name)

    if not yes and not ui.confirm(
        f"rename will force-recreate {old_name!r}'s container as {new_name!r} "
        f"(brief downtime while the container is replaced). Proceed?",
        default=False,
    ):
        raise UserError("aborted by user")

    old_svc = service_name(old_name, agent_type)
    new_svc = service_name(new_name, agent_type)
    old_dir = cfg.paths.host_data_dir / old_name
    new_dir = cfg.paths.host_data_dir / new_name

    with compose.acquire_lock(cfg.paths.host_data_dir):
        _refuse_if_host_view(cfg, old_name, f"rename {old_name!r}")  # re-check under the lock
        journal = Journal.find_resumable(
            cfg.paths.host_data_dir, "rename", old_name
        ) or Journal.begin(cfg.paths.host_data_dir, "rename", old_name, RENAME_PHASES)

        for phase in _phases_to_run(journal):
            if phase == "dir_renamed":
                # This phase must FAIL LOUDLY rather than advance on a
                # botched move: the next phase rewrites env_file to
                # ``./<new>/.env`` and validates via `docker compose
                # config -q`, which errors on a missing env file — and a
                # journaled-but-not-actually-moved dir means every retry
                # skips this phase, leaving the rename unrecoverable
                # without hand-editing the journal. The idempotency
                # contract: already-moved (new exists, old gone) is a
                # resume and counts as success; a missing source with no
                # target — or a stray target alongside the source — is
                # an error.
                if cfg.paths.data_dir_in_vm:
                    vm_old = f"{cfg.paths.vm_agents_root}/{old_name}"
                    vm_new = f"{cfg.paths.vm_agents_root}/{new_name}"
                    result = mp.exec_bash(
                        cfg.vm.name,
                        f"set -eu\n"
                        f"if [ -d {vm_new} ] && [ ! -d {vm_old} ]; then exit 0; fi\n"
                        f"if [ ! -d {vm_old} ]; then "
                        f"echo 'source dir {vm_old} is missing' >&2; exit 1; fi\n"
                        f"if [ -d {vm_new} ]; then "
                        f"echo 'target dir {vm_new} already exists' >&2; exit 1; fi\n"
                        f"mv {vm_old} {vm_new}\n",
                        check=False,
                    )
                    if result.returncode != 0:
                        raise RuntimeFailure(
                            f"could not move VM data dir {vm_old} -> {vm_new} "
                            f"(exit {result.returncode}): "
                            f"{(result.stderr or '').strip() or 'unknown error'}. "
                            f"Nothing was journaled — re-run "
                            f"`agents rename {old_name} {new_name}` to retry."
                        )
                elif new_dir.is_dir() and not old_dir.exists():
                    pass  # already moved by a previous partially-completed run
                elif not old_dir.exists():
                    raise RuntimeFailure(
                        f"data dir {old_dir} is missing and {new_dir} does not "
                        f"exist — cannot rename the agent's data dir"
                    )
                elif new_dir.exists():
                    raise RuntimeFailure(
                        f"target data dir {new_dir} already exists while {old_dir} "
                        f"is still present — move it aside and re-run the rename"
                    )
                else:
                    old_dir.rename(new_dir)
                journal.advance(phase)
            elif phase == "compose_updated":
                doc = compose.read_compose_doc(cfg)
                if old_svc in (doc.get("services") or {}):
                    compose.rename_service(doc, old_svc, new_svc)
                    # Also update container_name and any path references that mention old_name.
                    _rewrite_service_paths(doc["services"][new_svc], old_name, new_name)
                    compose.write_compose_atomically(cfg, doc)
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


def _refuse_rename_with_openviking_sidecar(cfg: AgentsConfig, name: str) -> None:
    """Refuse to rename an agent that has an OpenViking memory sidecar.

    The sidecar is a *sibling* compose service (``openviking-<name>``,
    referenced from the agent's ``depends_on``) with its own
    container_name, volume path, and DNS-based endpoint baked into the
    agent's config.yaml. Renaming only the agent would strand all of
    that under the old name — a partially-renamed sidecar is worse
    than a refusal.
    """
    doc = compose.read_compose_doc(cfg)
    has_sidecar_service = f"openviking-{name}" in (doc.get("services") or {})
    if not has_sidecar_service:
        # Lazy import: memory/__init__ imports from this module.
        from agents_cli import memory as memory_mod

        state = memory_mod.read_state(cfg, name)
        if not (state and state.get("plugin") == "openviking"):
            return
    raise UserError(
        f"agent {name!r} has an OpenViking memory sidecar (openviking-{name}) "
        f"that rename cannot migrate. Disable memory first "
        f"(`agents memory disable {name}`), rename, then re-enable."
    )


def _rewrite_service_paths(service: dict[str, object], old_name: str, new_name: str) -> None:
    """Update container_name and every ``./<old>/...``-prefixed string value
    in the service block, in place.

    ``env_file`` matters as much as ``volumes``: both agent fragments carry
    ``env_file: [./<name>/.env]``, and a stale entry breaks EVERY compose
    op for EVERY agent once the old dir is gone ("env file not found").
    Rather than enumerating keys, rewrite any matching string anywhere in
    the block (handles string and list forms of env_file, nested maps,
    and future fragment evolution).
    """
    container_name = service.get("container_name")
    if isinstance(container_name, str):
        service["container_name"] = container_name.replace(old_name, new_name)

    old_prefix = f"./{old_name}/"
    new_prefix = f"./{new_name}/"

    def rewrite(value: str) -> str:
        if value.startswith(old_prefix):
            return new_prefix + value[len(old_prefix) :]
        return value

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    node[key] = rewrite(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                if isinstance(value, str):
                    node[i] = rewrite(value)
                else:
                    walk(value)

    walk(service)


# ---------- reseat ----------


def reseat(
    name: str,
    *,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, object]:
    """Regenerate the agent's compose service block from the current fragment.

    Use case: the toolkit's service-fragment template evolves (e.g. a new
    env var is added, a volume entry changes), but agents created before
    the change keep their stale service block forever. ``reseat`` rewrites
    just that block to match what ``agents create`` would produce today —
    preserving the published host port(s), the pinned image tag, env_file
    additions (per-agent secrets file), depends_on additions (e.g. an
    OpenViking sidecar), and any volumes the stock fragment doesn't claim,
    so the user doesn't lose those. Works for both agent types.

    For Hermes agents, also materializes the cont-init perms hook and runs
    the ``fix-perms`` chown/chmod dance after the recreate, since a
    fragment that didn't include ``HERMES_UID`` will have left
    ``/opt/data`` owned by uid 10000. OpenClaw skips those Hermes-only
    steps (no cont-init hook, no install-path chown, no HERMES_UID dance).

    Force-recreates the container so the new fragment takes effect on the
    next entrypoint pass.
    """
    from agents_cli import fixperms_cmd

    validate_name(name)
    cfg = cfg_mod.load(config_path)
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type is None:
        raise UserError(f"agent {name!r} not in compose")
    svc = service_name(name, agent_type)

    if not yes and not ui.confirm(
        f"reseat will regenerate {name!r}'s compose service block from the "
        f"current toolkit template and force-recreate the container. Proceed?",
        default=False,
    ):
        raise UserError("aborted by user")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        doc = compose.read_compose_doc(cfg)
        services = doc.get("services") or {}
        existing = services.get(svc)
        if not isinstance(existing, dict):
            raise UserError(f"service {svc!r} missing from compose")

        # Pull out the values worth preserving from the existing entry.
        port = _extract_host_port(existing)
        tag = _extract_image_tag(existing)

        # Built with the preserved name/port/tag, the fresh fragment
        # doubles as the diff baseline: its entries are exactly what a
        # stock block would carry today, so anything in the existing
        # block beyond it is a user/toolkit addition worth keeping.
        if agent_type == "hermes":
            new_fragment: dict[str, object] = compose.hermes_service_fragment(
                name=name, cfg=cfg, port=port, image_tag=tag
            )
        else:
            new_fragment = compose.openclaw_service_fragment(name=name, port=port, image_tag=tag)

        _carry_user_additions(new_fragment, existing)

        ui.progress(f"rewriting {svc}'s compose service block from the current template...")
        services[svc] = new_fragment
        compose.write_compose_atomically(cfg, doc)

        # Recreate so the new fragment is actually applied. Hermes additionally
        # gets the chown/chmod dance — adding HERMES_UID to env will fix newly
        # created files, but stale files from the previous container are still
        # at the old uid.
        if agent_type == "hermes":
            # The regenerated fragment now bind-mounts the cont-init.d perms
            # hook; materialize its source before recreating so the mount
            # resolves to a file (not an auto-created dir).
            compose.ensure_cont_init_script(cfg)
        fixperms_cmd.force_recreate(cfg, svc)
        if agent_type == "hermes":
            huid = fixperms_cmd.container_hermes_uid(cfg, svc)
            fixperms_cmd.chown_data_dir(cfg, svc, huid)
            fixperms_cmd.chmod_data_dir_traversable(cfg, svc)
            fixperms_cmd.chown_hermes_install_paths(cfg, svc, huid)

    payload: dict[str, object] = {
        "name": name,
        "type": agent_type,
        "service": svc,
        "image_tag": tag,
        "port": port,
        "status": "reseated",
    }
    ui.emit(
        payload,
        human=(
            f"reseated {agent_type} agent {name!r}\n"
            f"  service block regenerated from current fragment\n"
            f"  container force-recreated\n"
            + (
                "  /opt/data ownership re-aligned"
                if agent_type == "hermes"
                else "  (no chown step for non-Hermes agents)"
            )
        ),
    )
    return payload


def _extract_host_port(service: dict[str, object]) -> int:
    """Pull the host-side port out of an existing service's ``ports:`` list.

    Compose port entries look like ``"127.0.0.1:8642:8642"`` — the middle
    field is the host port. Falls back to the agent type's base port if
    we can't parse anything, so reseat doesn't silently drift the port.
    """
    ports = service.get("ports") or []
    if isinstance(ports, list):
        for entry in ports:
            if isinstance(entry, str):
                parts = entry.split(":")
                if len(parts) >= 2:
                    try:
                        return int(parts[-2])
                    except ValueError:
                        continue
    raise UserError(
        "could not extract host port from existing service entry; compose file may be malformed"
    )


def _extract_image_tag(service: dict[str, object]) -> str:
    image = service.get("image")
    if not isinstance(image, str) or ":" not in image:
        raise UserError(f"existing service has no parseable image tag (got {image!r})")
    return image.rsplit(":", 1)[1]


def _carry_user_additions(new_fragment: dict[str, object], existing: dict[str, object]) -> None:
    """Re-attach additions the user/toolkit layered onto the stock block:
    extra env_file entries (secrets multi-file setups), depends_on
    additions (sidecars), extra published ports, and hand-added volumes.

    ``new_fragment`` — freshly built from the current template — is the
    diff baseline; existing entries the stock list doesn't claim are
    classified as additions and appended (when in doubt, preserve).
    Ports diff by host port and volumes by container target, so a
    template evolution that reshapes a stock entry (new source path, new
    mount options) supersedes the stale variant instead of producing a
    duplicate binding compose would refuse.
    """
    _merge_list_additions(new_fragment, existing, "env_file")
    _merge_list_additions(new_fragment, existing, "depends_on")
    _merge_list_additions(new_fragment, existing, "ports", entry_identity=_port_identity)
    _merge_list_additions(new_fragment, existing, "volumes", entry_identity=_volume_identity)


def _merge_list_additions(
    new_fragment: dict[str, object],
    existing: dict[str, object],
    key: str,
    entry_identity: Callable[[object], object] = lambda entry: entry,
) -> None:
    """Merge ``existing[key]``'s additions into ``new_fragment[key]``.

    Stock entries come first and win for any shared identity; existing
    entries whose identity the stock list doesn't claim are appended in
    their original order. Shapes the classifier can't diff (string-form
    env_file, dict-form depends_on) are preserved wholesale rather than
    dropped.
    """
    old = existing.get(key)
    if old is None:
        return
    stock = new_fragment.get(key)
    if not isinstance(old, list) or not (stock is None or isinstance(stock, list)):
        new_fragment[key] = old
        return
    stock_list: list[object] = list(stock) if isinstance(stock, list) else []
    # A list (not a set) so unhashable long-form entries still compare.
    stock_ids = [entry_identity(entry) for entry in stock_list]
    additions = [entry for entry in old if entry_identity(entry) not in stock_ids]
    if stock_list or additions:
        new_fragment[key] = stock_list + additions


def _port_identity(entry: object) -> object:
    """Identity of a ``ports:`` entry — ``(host port, protocol)``. Two
    published entries collide iff they claim the same host port on the
    same protocol, regardless of bind address (``127.0.0.1:8642:8642``
    vs ``8642:8642``). TCP and UDP can publish the same numeric host
    port simultaneously, so a user's UDP twin of the stock TCP port
    (``127.0.0.1:8642:9000/udp``) is an addition, not a duplicate.
    """
    if isinstance(entry, str):
        spec, _, proto = entry.partition("/")
        parts = spec.split(":")
        if len(parts) >= 2:
            return (parts[-2], proto or "tcp")
    return entry


def _volume_identity(entry: object) -> object:
    """Identity of a ``volumes:`` entry — its container-side target.
    Docker refuses duplicate mount targets, so when a template evolution
    reshapes a stock entry, the fresh form must supersede the stale one
    rather than sit next to it.
    """
    if isinstance(entry, str):
        parts = entry.split(":")
        # "src:target" / "src:target:opts" — or a bare anonymous-volume
        # target with no colon at all.
        return parts[1] if len(parts) >= 2 else parts[0]
    if isinstance(entry, dict):
        target = entry.get("target")
        if target is not None:
            return target
    return entry


# ---------- shared journal walker ----------


def _phases_to_run(journal: Journal) -> Iterator[str]:
    """Yield each phase that has not yet been completed, in declared order."""
    for phase in journal.phases:
        if phase not in journal.completed_phases:
            yield phase
