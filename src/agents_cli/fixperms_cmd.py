"""One-shot remediation for the ``/opt/data`` permission drift class of
bug.

The class of bug
----------------

The Hermes image's entrypoint *should* ``usermod -u $HERMES_UID hermes``
on container start, so the runtime ``hermes`` user matches the toolkit's
``RUNTIME_UID`` (1000). When that doesn't happen — entrypoint skipped on
an old running container, image-level changes, env-var interpolation
failure — ``/opt/data`` ends up owned by uid 10000 with mode 700, and
``agents exec`` (which runs as 1000) hits ``EACCES`` on every stat under
``/opt/data``.

The fix is mechanical:

1. Stop the agent's container.
2. ``docker rm -f`` it so ``compose up`` cannot decide "no config drift,
   keep the running one" and skip the entrypoint.
3. Bring it back up — entrypoint runs from scratch with the current
   ``HERMES_UID`` / ``HERMES_GID`` env.
4. Query the container's *actual* ``hermes`` uid (whatever it ended up
   as after the entrypoint did or didn't usermod).
5. ``chown -R <huid>:<huid> /opt/data`` so existing files match the
   live runtime user. Catches the case where files were created during
   a previous container generation at a different uid.
6. ``chmod 755 /opt/data`` so the toolkit's ``agents exec`` at uid
   1000 can at least traverse the dir (necessary when the entrypoint
   still didn't honour HERMES_UID — band-aid for the worst case).

When the entrypoint *did* usermod to 1000, step 5 is a no-op and step
6 is harmless (1000 already owns the dir).

When it *didn't* (hermes still at 10000), step 5 lines up files with
the runtime, and step 6 lets the toolkit's exec pier in to traverse —
mode-600 files like ``auth.json`` stay private to uid 10000 (so
secrets aren't exposed), but the toolkit can run ``hermes`` commands
without hitting ``EACCES`` on the dir traversal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents_cli import compose, compose_runner, mp, ui
from agents_cli import config as cfg_mod
from agents_cli.agents_cmd import _detect_type_from_compose
from agents_cli.agents_cmd import service_name as _service_name_for
from agents_cli.config import AgentsConfig
from agents_cli.errors import RuntimeFailure, UserError
from agents_cli.ops import RUNTIME_UID, _compose_exec_in_vm_argv


def fix_perms(
    name: str,
    *,
    yes: bool = False,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    cfg = cfg_mod.load(config_path)
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type is None:
        raise UserError(f"agent {name!r} not in compose")
    if agent_type != "hermes":
        raise UserError(f"fix-perms is Hermes-only — {name!r} is type {agent_type!r}")
    svc = _service_name_for(name, agent_type)

    if not yes and not ui.confirm(
        f"fix-perms will stop, force-remove, and re-create the {name!r} "
        f"container, then chown /opt/data. Proceed?",
        default=False,
    ):
        raise UserError("aborted by user")

    with compose.acquire_lock(cfg.paths.host_data_dir):
        _force_recreate(cfg, svc)
        huid = _container_hermes_uid(cfg, svc)
        _chown_data_dir(cfg, svc, huid)
        _chmod_data_dir_traversable(cfg, svc)

    aligned = huid == RUNTIME_UID
    payload = {
        "agent": name,
        "hermes_uid": huid,
        "runtime_uid": RUNTIME_UID,
        "aligned": aligned,
    }
    if aligned:
        human = (
            f"fix-perms[{name}]: ok — hermes uid {huid} matches the toolkit's "
            f"RUNTIME_UID ({RUNTIME_UID})"
        )
    else:
        human = (
            f"fix-perms[{name}]: PARTIAL — hermes uid is {huid} but the "
            f"toolkit's RUNTIME_UID is {RUNTIME_UID}. Dir traversal opened "
            f"via chmod 755 (band-aid). The image's entrypoint isn't honouring "
            f"HERMES_UID; investigate that to get a clean fix."
        )
    ui.emit(payload, human=human)
    return payload


# ---------- internals ----------


def _force_recreate(cfg: AgentsConfig, svc: str) -> None:
    """Stop, ``docker rm -f``, then ``compose up`` so the entrypoint runs
    from scratch.

    ``docker compose up -d <svc>`` alone often decides "no config drift,
    keep the running container" even when env-var interpolation has
    changed, so a force-remove is the only way to guarantee a fresh
    entrypoint pass. Both the stop and the rm tolerate "not running" /
    "no such container" errors — the desired post-state is "service is
    up from scratch" regardless of starting state.
    """
    compose_runner.stop(cfg, svc)
    mp.exec_in_vm(cfg.vm.name, "docker", "rm", "-f", svc, check=False)
    result = compose_runner.up(cfg, svc)
    if result.returncode != 0:
        raise RuntimeFailure(f"compose up failed for {svc!r}: {result.stderr.strip()}")


def _container_hermes_uid(cfg: AgentsConfig, svc: str) -> int:
    """Return the numeric uid of the ``hermes`` user inside the running
    container. Runs as root so it works regardless of dir perms.
    """
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(cfg, svc, "id", "-u", "hermes", no_tty=True, as_root=True),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeFailure(f"could not read hermes uid in {svc!r}: {result.stderr.strip()}")
    try:
        return int(result.stdout.strip())
    except ValueError as e:
        raise RuntimeFailure(f"unparseable hermes uid from {svc!r}: {result.stdout!r}") from e


def _chown_data_dir(cfg: AgentsConfig, svc: str, uid: int) -> None:
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(
            cfg,
            svc,
            "chown",
            "-R",
            f"{uid}:{uid}",
            "/opt/data",
            no_tty=True,
            as_root=True,
        ),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeFailure(f"chown -R {uid}:{uid} /opt/data failed: {result.stderr.strip()}")


def _chmod_data_dir_traversable(cfg: AgentsConfig, svc: str) -> None:
    """Make ``/opt/data`` mode 755. The directory becomes world-traversable
    so the toolkit's ``RUNTIME_UID`` exec can ``cd`` in even when the
    image's entrypoint failed to align hermes with RUNTIME_UID.

    Per-file modes are untouched: secrets at mode 600 (``auth.json``,
    ``auth.lock``, etc.) stay private to their owner.
    """
    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(cfg, svc, "chmod", "755", "/opt/data", no_tty=True, as_root=True),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeFailure(f"chmod 755 /opt/data failed: {result.stderr.strip()}")
