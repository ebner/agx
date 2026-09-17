"""``agents edit <agent> <path>`` — edit one of a Hermes agent's text
files in ``$EDITOR`` without any host-side access to its data dir.

The agent's state subtree is owned by a rootless-Docker subuid under
mode 700, so the host user can't open ``SOUL.md`` & co. directly. This
command never touches the host path: it reads and writes through
``docker compose exec`` as the container's runtime user (the same route
:mod:`agents_cli.hermes_config` uses), so it needs no sudo, no ACL, and
works identically in ``--data-dir-in-vm`` mode.

Contract:

- Hermes only, container running. Regular UTF-8 text files up to
  :data:`MAX_BYTES`, addressed relative to ``/opt/data`` (or to the
  ``--profile`` home). Symlinks are resolved once inside the container
  and must stay inside that base.
- A path that resolves to a ``config.yaml`` (default or any profile) is
  written via :func:`hermes_config.write_atomically`, so the
  ``0.0.0.0`` dashboard refusal and ``hermes config validate`` apply —
  there is deliberately no way around that here.
- Conflict check is optimistic: the file is re-read under
  ``<data-dir>/.agents.lock`` right before the write and compared by
  hash. That serializes against other toolkit commands; it is NOT a
  lock against Hermes itself rewriting the file in that instant.
- The edit buffer lives in a private dir under ``$XDG_RUNTIME_DIR``
  (tmpfs — not the VM-shared, backed-up staging dir). It is removed on
  success and KEPT on any failure after the editor ran, so a rejected
  write never costs the user their edits.

Note ``/opt/data/.env`` is Hermes' own env file; the toolkit-managed
per-agent secrets live in ``<data-dir>/<agent>/.env`` and are edited
with ``agents secret``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from ruamel.yaml.error import YAMLError

from agents_cli import agents_cmd, compose, hermes_config, mp, ui
from agents_cli import config as cfg_mod
from agents_cli.config import AgentsConfig
from agents_cli.errors import RuntimeFailure, UserError

MAX_BYTES = 1024 * 1024
DATA_ROOT = "/opt/data"
_CONFIG_RE = re.compile(r"/opt/data/(?:profiles/([^/]+)/)?config\.yaml")
_NOT_RUNNING_MARKERS = ("not running", "no such service", "no such container")


# ---------- container I/O ----------


def _exec(cfg: AgentsConfig, svc: str, *cmd: str) -> mp.MpResult:
    from agents_cli.ops import _compose_exec_in_vm_argv

    return mp.exec_in_vm(
        cfg.vm.name, *_compose_exec_in_vm_argv(cfg, svc, *cmd, no_tty=True), check=False
    )


def _raise_if_not_running(agent: str, svc: str, result: mp.MpResult) -> None:
    err = (result.stderr or "").lower()
    if any(m in err for m in _NOT_RUNNING_MARKERS):
        raise UserError(
            f"container {svc!r} is not running — start it with `agents start {agent}` first."
        )


def _realpath(cfg: AgentsConfig, agent: str, svc: str, path: str) -> str | None:
    result = _exec(cfg, svc, "realpath", "-e", "--", path)
    if result.returncode != 0:
        _raise_if_not_running(agent, svc, result)
        return None
    return (result.stdout or "").strip() or None


def _is_under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def _resolve_target(cfg: AgentsConfig, agent: str, svc: str, rel: str, profile: str | None) -> str:
    """Resolve ``rel`` to a real in-container path, confined to the
    (profile) home, itself confined to ``/opt/data``. Everything after
    this operates on the resolved path only."""
    pure = PurePosixPath(rel)
    if not rel or pure.is_absolute() or ".." in pure.parts or re.search(r"[\x00-\x1f]", rel):
        raise UserError(
            f"invalid path {rel!r} — give a path relative to the agent's "
            f"{hermes_config.profile_home(profile)} (no leading '/', no '..')."
        )
    data_root = _realpath(cfg, agent, svc, DATA_ROOT)
    if data_root is None:
        raise RuntimeFailure(f"could not resolve {DATA_ROOT} inside {svc!r}")
    home = hermes_config.profile_home(profile)
    home_real = _realpath(cfg, agent, svc, home)
    if home_real is None:
        raise UserError(
            f"profile {profile!r} does not exist in agent {agent!r} — see "
            f"`agents profile list {agent}`."
        )
    if not _is_under(home_real, data_root):
        raise UserError(f"refusing: {home} resolves outside {DATA_ROOT} ({home_real})")
    resolved = _realpath(cfg, agent, svc, f"{home}/{rel}")
    if resolved is None:
        raise UserError(
            f"{home}/{rel} does not exist in {agent!r}. `agents edit` only edits existing files."
        )
    if not _is_under(resolved, home_real) or resolved == home_real:
        raise UserError(f"refusing: {rel!r} resolves outside {home} ({resolved})")
    return resolved


def _require_regular_small_file(cfg: AgentsConfig, svc: str, resolved: str) -> None:
    result = _exec(cfg, svc, "stat", "-c", "%F|%s", "--", resolved)
    kind, _, size = (result.stdout or "").strip().partition("|")
    if result.returncode != 0 or not kind.startswith("regular"):
        raise UserError(f"{resolved} is not a regular file ({kind or 'stat failed'})")
    if not size.isdigit() or int(size) > MAX_BYTES:
        raise UserError(
            f"{resolved} is {size} bytes — `agents edit` handles text files up to 1 MiB"
        )


def _fetch(cfg: AgentsConfig, agent: str, svc: str, resolved: str) -> bytes:
    # base64 because backend exec output is decoded with
    # ``errors="replace"`` — a raw ``cat`` would silently mangle any
    # non-UTF-8 byte and we'd write the mangled version back.
    result = _exec(cfg, svc, "base64", "-w0", "--", resolved)
    if result.returncode != 0:
        _raise_if_not_running(agent, svc, result)
        raise RuntimeFailure(
            f"could not read {resolved} in {svc!r}: {(result.stderr or '').strip()}"
        )
    try:
        return base64.b64decode((result.stdout or "").strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeFailure(f"could not decode {resolved} from {svc!r}: {exc}") from exc


def _decode(data: bytes, what: str) -> str:
    if b"\x00" in data:
        raise UserError(f"{what} looks binary — `agents edit` is for UTF-8 text files")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UserError(f"{what} is not valid UTF-8 ({exc})") from exc


def _write_plain(cfg: AgentsConfig, svc: str, resolved: str, content: bytes) -> None:
    from agents_cli.ops import _compose_exec_in_vm_argv

    stage = f"{resolved}.{os.getpid()}.{uuid.uuid4().hex[:8]}.new"
    # The path rides in as a positional parameter, never as shell text.
    # umask first: the original may be mode 600 and the stage must not
    # be born wider than that.
    write_argv = _compose_exec_in_vm_argv(
        cfg, svc, "sh", "-c", 'umask 077; cat > "$1"', "sh", stage, no_tty=True
    )
    try:
        write = mp.exec_in_vm_with_stdin(cfg.vm.name, *write_argv, input_bytes=content, check=False)
        if write.returncode != 0:
            raise RuntimeFailure(
                f"could not stage {stage}: {(write.stderr or '').strip() or write.returncode}"
            )
        for step in (
            ("chmod", f"--reference={resolved}", "--", stage),
            ("mv", "--", stage, resolved),
        ):
            done = _exec(cfg, svc, *step)
            if done.returncode != 0:
                raise RuntimeFailure(
                    f"`{' '.join(step)}` failed in {svc!r}: {(done.stderr or '').strip()}"
                )
    except BaseException:
        _exec(cfg, svc, "rm", "-f", "--", stage)
        raise


# ---------- editor ----------


def _editor_argv() -> list[str]:
    for var in ("VISUAL", "EDITOR"):
        value = os.environ.get(var, "").strip()
        if value:
            return shlex.split(value)
    for candidate in ("sensible-editor", "editor", "nano", "vi"):
        found = shutil.which(candidate)
        if found:
            return [found]
    raise UserError("no editor found — set $VISUAL or $EDITOR")


def _run_editor(path: Path) -> None:
    """Run the editor on the controlling terminal, so ``--json`` stdout
    stays a single document and redirected stdio doesn't confuse it."""
    argv = [*_editor_argv(), str(path)]
    try:
        tty = open("/dev/tty", "r+b", buffering=0)  # noqa: SIM115 - closed below
    except OSError as exc:
        raise UserError("`agents edit` needs an interactive terminal") from exc
    try:
        proc = subprocess.run(argv, stdin=tty, stdout=tty, stderr=tty, check=False)
    except FileNotFoundError as exc:
        raise UserError(f"editor not found: {argv[0]}") from exc
    finally:
        tty.close()
    if proc.returncode != 0:
        raise UserError(f"editor exited {proc.returncode}; nothing was written")


def _make_buffer(name: str, content: bytes) -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    parent = runtime if runtime and os.path.isdir(runtime) else None
    workdir = Path(tempfile.mkdtemp(prefix="agents-edit-", dir=parent))  # mode 0700
    buffer = workdir / name
    fd = os.open(buffer, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)
    return buffer


# ---------- command ----------


def _parse_config(text: str) -> Any:
    doc = hermes_config._yaml().load(text)
    if not isinstance(doc, dict):
        raise UserError("config.yaml must be a YAML mapping")
    return doc


def edit(
    *,
    agent: str,
    path: str,
    profile: str | None = None,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> None:
    agents_cmd.validate_name(agent)
    cfg = cfg_mod.load(config_path)
    agent_type = agents_cmd._detect_type_from_compose(cfg, agent)
    if agent_type is None:
        raise UserError(f"agent {agent!r} not found in compose")
    if agent_type != "hermes":
        raise UserError(
            f"`agents edit` is Hermes-only ({agent!r} is {agent_type}). For OpenClaw use "
            f"`agents exec {agent} -- <editor> <path>` or `openclaw configure`."
        )
    svc = agents_cmd.service_name(agent, agent_type)

    resolved = _resolve_target(cfg, agent, svc, path, profile)
    _require_regular_small_file(cfg, svc, resolved)
    original = _fetch(cfg, agent, svc, resolved)
    _decode(original, resolved)
    config_match = _CONFIG_RE.fullmatch(resolved)

    buffer = _make_buffer(PurePosixPath(resolved).name, original)
    keep_buffer = False
    try:
        doc: Any = None
        while True:
            _run_editor(buffer)
            keep_buffer = True
            edited = buffer.read_bytes()
            if edited == original:
                keep_buffer = False
                ui.emit(
                    {"agent": agent, "path": resolved, "changed": False},
                    human=f"{resolved}: no changes",
                )
                return
            _decode(edited, "the edited buffer")
            if len(edited) > MAX_BYTES:
                raise UserError("the edited buffer exceeds 1 MiB")
            if config_match is None:
                break
            try:
                doc = _parse_config(edited.decode("utf-8"))
                hermes_config.validate_dashboard_binding(doc)
                break
            except (YAMLError, UserError) as exc:
                ui.warn(f"config.yaml rejected: {exc}")
                if not ui.confirm("re-open the editor?", default=True):
                    raise UserError("aborted; config.yaml was not changed") from exc

        with compose.acquire_lock(cfg.paths.host_data_dir):
            current = _fetch(cfg, agent, svc, resolved)
            if hashlib.sha256(current).digest() != hashlib.sha256(original).digest():
                raise UserError(
                    f"{resolved} changed inside {agent!r} while you were editing; "
                    "nothing was written."
                )
            if config_match is not None:
                hermes_config.write_atomically(cfg, agent, doc, profile=config_match.group(1))
            else:
                _write_plain(cfg, svc, resolved, edited)
        keep_buffer = False
    except BaseException:
        if keep_buffer:
            ui.warn(f"your edits are kept at {buffer}")
        raise
    finally:
        if not keep_buffer:
            shutil.rmtree(buffer.parent, ignore_errors=True)

    ui.emit(
        {"agent": agent, "path": resolved, "changed": True},
        human=f"updated {resolved} in {agent!r}",
    )
