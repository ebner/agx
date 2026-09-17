"""POSIX-ACL helpers — let the host user reach files the rootless
container owns, and vice versa.

Background — why this exists:

Rootless Docker maps container uids to the host user's subuid range
from ``/etc/subuid``. Container uid 0 = host user uid; container uid N
(for N > 0) = ``subuid_base + N - 1``. The Hermes / OpenClaw images
both run their long-living processes as container uid 1000, which on
a typical install lands at host uid 100999. Every file the agent
writes into a bind-mounted host directory shows up under that subuid,
with whatever umask the agent used (often 0600 for credentials).

The host user owns the data dir conceptually but cannot read it from
the host without elevation. POSIX ACLs are the cleanest fix: layer
additional rwx grants on top of POSIX ownership without changing the
owner. The container keeps writing as 100999; the host user gets an
ACL entry granting access; everyone's happy.

This module is the shared plumbing for two callers:

- :mod:`agents_cli.access_cmd` — explicit ``agents access {grant,
  revoke,list}`` for the agent's own data dirs (which the rootless
  container created — needs sudo to ACL).
- :mod:`agents_cli.mounts_cmd` — auto-applies ACLs on the host folder
  added by ``agents mount add`` (which the user owns — no sudo
  needed). Bidirectional: agent gets read (or rw) access to user
  files; user gets default ACL for files the agent writes back.

Stylistically these helpers are argv-emitting (return the exact
``setfacl …`` argv they'd run). Callers decide whether to print +
hand off to the user, sudo-exec them, or run them as the current
user. Matches the "no surprise sudo" posture locked in issue #5 for
the bare-metal bootstrap.
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agents_cli import ui
from agents_cli.errors import RuntimeFailure, UserError

# The Hermes + OpenClaw images both run their long-living process as
# container uid 1000. Hardcoded here so callers don't have to thread
# it through; matches the rest of the toolkit (compose .env sets
# HERMES_UID=1000 unconditionally).
DEFAULT_CONTAINER_UID: int = 1000


# ---------- subuid math ----------


_subuid_cache: dict[int, int] = {}


def compute_container_subuid(host_uid: int, container_uid: int = DEFAULT_CONTAINER_UID) -> int:
    """Return the host-side uid that a rootless-Docker container uid
    resolves to under ``host_uid``'s subuid range.

    Mapping (rootless Docker default):

    - container uid 0 → ``host_uid`` itself (the rootless user is the
      mapping for container root)
    - container uid N>0 → ``subuid_base + (N - 1)``, where
      ``subuid_base`` comes from ``/etc/subuid`` for ``host_uid``'s
      login name.

    Cached on first call — ``/etc/subuid`` doesn't change during a
    process lifetime.

    Raises :class:`UserError` if ``host_uid`` has no ``/etc/subuid``
    entry (rare — usually means rootless Docker isn't set up).
    """
    if container_uid == 0:
        return host_uid
    if host_uid in _subuid_cache:
        return _subuid_cache[host_uid] + container_uid - 1
    try:
        user = pwd.getpwuid(host_uid).pw_name
    except KeyError as e:
        raise UserError(f"no passwd entry for uid {host_uid}: {e}") from e
    try:
        subuid_lines = Path("/etc/subuid").read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise UserError(f"cannot read /etc/subuid: {e}") from e
    for line in subuid_lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == user:
            base = int(parts[1])
            _subuid_cache[host_uid] = base
            return base + container_uid - 1
    raise UserError(
        f"no /etc/subuid entry for user {user!r} — rootless Docker isn't set up. "
        "Run `usermod --add-subuids 100000-165535 $USER` (then re-login)."
    )


# ---------- ACL argv emitters ----------


@dataclass(frozen=True)
class AclRecipient:
    """One ``u:<uid>:<perms>`` ACL entry."""

    uid: int
    perms: str  # "rX", "rwX", etc. Capital X = exec on dirs only.

    def to_spec(self, *, default: bool = False) -> str:
        prefix = "d:" if default else ""
        return f"{prefix}u:{self.uid}:{self.perms}"


def apply_acl_argv(
    path: Path,
    *,
    recipients: Sequence[AclRecipient],
    recurse: bool = True,
    defaults: bool = True,
) -> list[list[str]]:
    """Emit one or more ``setfacl …`` argvs.

    The recursive walk and the default-ACL set are split into two
    invocations because the default-ACL flag (``-d``) only makes
    sense on directories — applying it to existing files via ``-R``
    would error on every regular file in the tree. ``setfacl`` does
    have ``-R -d`` shorthand but it's brittle across versions; the
    explicit split is consistent.

    Returns ``[argv1, argv2?]`` so the caller can sudo or shell-out
    however it wants. Empty list means "nothing to do" (no recipients).
    """
    if not recipients:
        return []
    # ``m::rwx`` reset is critical. The setfacl manpage claims the
    # mask is auto-recomputed unless explicitly given, but in practice
    # an existing ``mask::---`` (typical after a ``chmod 700`` on a
    # dir that already had named ACL entries) is NOT recomputed by a
    # subsequent ``-m u:N:rX``. The new entry lands but is masked to
    # ``---`` so effective perms stay at zero. Including ``m::rwx``
    # in the spec forces the mask wide open; each individual entry's
    # own perms still cap effective access (u:1000:rX → r-x on dirs).
    # Reproduced live on coder's .data (subuid-owned, mode 700) before
    # this fix landed.
    specs = [r.to_spec() for r in recipients] + ["m::rwx"]
    cmds: list[list[str]] = []
    base = ["setfacl"]
    if recurse:
        base.append("-R")
    base += ["-m", ",".join(specs), str(path)]
    cmds.append(base)
    if defaults:
        # ``-d -m`` already implies the default-ACL namespace; the
        # spec passed in must NOT carry the ``d:`` prefix (setfacl
        # rejects the duplication with "Invalid argument"). Same
        # mask-reset reasoning applies.
        default_specs = [r.to_spec() for r in recipients] + ["m::rwx"]
        cmds.append(["setfacl", "-d", "-m", ",".join(default_specs), str(path)])
    return cmds


def remove_acl_argv(
    path: Path,
    *,
    recipients: Sequence[AclRecipient],
    recurse: bool = True,
    defaults: bool = True,
) -> list[list[str]]:
    """Emit ``setfacl -x …`` argvs to remove the entries by uid.

    ``-x`` takes a recipient spec without perms (``u:1000`` not
    ``u:1000:rwx``) and only deletes that uid's entries — other ACL
    entries (e.g. user-set or another caller's) are left in place.
    """
    if not recipients:
        return []
    specs = [f"u:{r.uid}" for r in recipients]
    cmds: list[list[str]] = []
    base = ["setfacl"]
    if recurse:
        base.append("-R")
    base += ["-x", ",".join(specs), str(path)]
    cmds.append(base)
    if defaults:
        # Same reason as apply_acl_argv: ``-d -x`` already implies
        # default namespace, don't double up the prefix.
        cmds.append(["setfacl", "-d", "-x", ",".join(specs), str(path)])
    return cmds


# ---------- filesystem probe ----------


def probe_acl_support(path: Path) -> tuple[bool, str | None]:
    """Try a benign ACL write on a temp file in the nearest user-
    writable parent of ``path``. Returns ``(supported, error_message)``.

    The "nearest user-writable parent" detail matters: when ``path``
    itself is a subuid-owned dir (the common case for ``agents
    access grant``), the calling user can't create files inside it
    — we'd get EACCES on the temp-file creation, which looks like
    "ACLs not supported" but is actually "you don't own this dir".
    Walking up to a user-writable ancestor side-steps the false
    negative while still probing the same filesystem (ACL support is
    a filesystem-level feature, not a per-directory one).

    The temp file is created by the calling user, gets a single
    no-op ACL write (``u:0:---`` — grants root no permissions), then
    is removed. Idempotent + side-effect-free.
    """
    if not path.exists():
        return False, f"{path} does not exist"
    if not shutil.which("setfacl"):
        return False, "setfacl(1) not installed (apt install acl)"

    # Find the nearest ancestor we can write to.
    probe_dir = path if path.is_dir() else path.parent
    while not os.access(probe_dir, os.W_OK):
        parent = probe_dir.parent
        if parent == probe_dir:
            # Reached the root without finding a writable ancestor —
            # the user's session genuinely can't probe here.
            return False, f"no user-writable parent above {path} to probe in"
        probe_dir = parent

    try:
        with tempfile.NamedTemporaryFile(
            dir=str(probe_dir),
            prefix=".acl-probe-",
            delete=False,
        ) as f:
            probe = Path(f.name)
        try:
            proc = subprocess.run(
                ["setfacl", "-m", "u:0:---", str(probe)],
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="replace").strip()
                return False, err or "setfacl refused (filesystem may lack ACL support)"
            return True, None
        finally:
            probe.unlink(missing_ok=True)
    except OSError as e:
        return False, f"could not create probe file in {probe_dir}: {e}"


# ---------- execution ----------


def run_owned(argvs: Sequence[Sequence[str]]) -> None:
    """Run each argv directly (no sudo). Use when the caller owns the
    target paths (e.g. user-owned host folder added via ``agents
    mount``).
    """
    for argv in argvs:
        ui.info(f"$ {' '.join(argv)}")
        proc = subprocess.run(list(argv), capture_output=True, check=False)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeFailure(f"setfacl failed: {err or 'unknown error'}")


def run_with_sudo(argvs: Sequence[Sequence[str]], *, yes: bool) -> None:
    """Run argvs prefixed with ``sudo``. Use when the target path is
    owned by a subuid (the agent's data dir).

    Prints the exact commands the user is about to authorize — always,
    via :func:`ui.progress` (not the ``--verbose``-gated ``ui.info``),
    because the CLI help and docs promise the argv display and a user
    can't audit a sudo grant they never saw. The argvs carry no secrets
    (paths + uids only). With ``yes=True`` skips the interactive
    confirmation — same semantics as the toolkit's other ``--yes``
    flags. With ``yes=False`` (the default), confirms once before
    invoking sudo (which then runs all argvs in sequence with the same
    cached credential).
    """
    if not argvs:
        return
    if not shutil.which("sudo"):
        raise UserError(
            "sudo not available; this step needs root. "
            "Run the commands manually after logging in as root:\n  "
            + "\n  ".join("sudo " + " ".join(a) for a in argvs)
        )
    ui.progress("The toolkit will run (sudo will prompt for your password if not cached):")
    for argv in argvs:
        ui.progress("  sudo " + shlex.join(argv))
    if not yes and not ui.confirm("apply now?", default=True):
        raise UserError("aborted by user")
    for idx, argv in enumerate(argvs):
        proc = subprocess.run(["sudo", *list(argv)], check=False)
        if proc.returncode != 0:
            # sudo's own auth-failure rc is 1 with the explanation
            # already on stderr (which we let through unredirected).
            # If we never got past the first command, nothing has been
            # applied; after that, partial state is possible. Surface
            # both cases distinctly.
            if idx == 0:
                raise UserError(
                    f"sudo failed before anything was applied (exit {proc.returncode}). "
                    "Common cause: no interactive TTY for the sudo password prompt. "
                    "Run this from your shell instead of a non-interactive context, "
                    "or pre-cache sudo with `sudo -v` first."
                )
            raise RuntimeFailure(
                f"`sudo {' '.join(argv)}` exited {proc.returncode} after {idx} of "
                f"{len(argvs)} commands had been applied; state may be partial."
            )


# ---------- introspection ----------


def getfacl(path: Path) -> str:
    """Return ``getfacl`` output for ``path``. Empty string on failure
    (caller decides whether that's worth surfacing — for ``agents
    access list`` it's "no access entries" which is fine to show).

    ``--numeric`` is critical: without it, getfacl resolves uids to
    login names (``user:alice:r-x``) and ``has_user_acl``'s ``user:
    <uid>:`` substring match silently fails to find anything.
    """
    if not shutil.which("getfacl"):
        return ""
    proc = subprocess.run(
        ["getfacl", "--numeric", "--omit-header", "--no-effective", str(path)],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


def has_user_acl(path: Path, uid: int) -> bool:
    """True if ``path`` has an ACL entry granting ``uid`` any access.

    Used by ``agents doctor`` and by ``agents access list`` to detect
    drift. Uses ``getfacl`` rather than libacl bindings — avoids the
    Python-pylibacl dep, and matches every other shell-out in the
    toolkit.
    """
    out = getfacl(path)
    if not out:
        return False
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(f"user:{uid}:") or line.startswith(f"default:user:{uid}:"):
            # An ACL with all permissions stripped still counts as "set"
            # (and we'd want doctor to surface that as drift too).
            return True
    return False


def get_user_acl_perms(path: Path, uid: int) -> str | None:
    """Return the access-ACL perm string (e.g. ``"r-x"``, ``"rwx"``)
    for ``uid`` on ``path``, or ``None`` if there's no entry.

    Used by ``agents access list`` to tell the user whether they
    granted read-only or read-write — the binary ok/missing signal
    from :func:`assess_host_access` isn't enough on its own. Only the
    access ACL is inspected (the default ACL governs what *future*
    files inherit, which is a different question).
    """
    out = getfacl(path)
    if not out:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(f"user:{uid}:"):
            # Format: ``user:<uid>:<perms>`` — split on `:` and take
            # the perms field. With ``--no-effective`` getfacl strips
            # the ``#effective:…`` comment for us.
            parts = line.split(":", 2)
            if len(parts) >= 3:
                return parts[2].strip()
    return None


def summarize_acl_mode(perms: str | None) -> str:
    """Compress a perm string into a human label for table output.

    - ``None`` → ``"—"`` (no entry)
    - perms containing ``w`` → ``"rw"`` (writable)
    - any other entry (``r-x``, ``r--``, ``---``) → ``"r"``

    The simplification is deliberate: users who ran ``agents access
    grant`` (without ``--rw``) see ``"r"``; those who ran ``--rw``
    see ``"rw"``; the all-zero case (``mask::---`` zeroing the entry
    pre-fix) surfaces as ``"r"`` because the granted perms still say
    ``r-x``. The effective-readability question is answered by the
    separate ``status`` column.
    """
    if perms is None:
        return "—"
    if "w" in perms:
        return "rw"
    return "r"


# ---------- convenience: typical recipient sets ----------


def host_user_recipient(host_uid: int, *, writable: bool) -> AclRecipient:
    """Standard recipient for "let the host user reach the agent's
    files". Read-only by default; ``writable=True`` grants rwX.
    """
    return AclRecipient(uid=host_uid, perms="rwX" if writable else "rX")


def container_recipient(
    host_uid: int, *, writable: bool, container_uid: int = DEFAULT_CONTAINER_UID
) -> AclRecipient:
    """Standard recipient for "let the agent's container reach a host
    folder" — the subuid form. Used by ``agents mount add``.
    """
    subuid = compute_container_subuid(host_uid, container_uid)
    return AclRecipient(uid=subuid, perms="rwX" if writable else "rX")


# ---------- doctor probe (used by agents_cli.ops) ----------


def assess_host_access(path: Path, host_uid: int) -> str:
    """Return ``"ok"`` | ``"masked"`` | ``"missing"`` | ``"path-missing"``
    describing whether the host user has effective access to ``path``.

    - **ok** — host user can actually read+traverse (dir) or read
      (file) ``path`` right now, either by ownership or via an
      effective ACL entry.
    - **masked** — the host user has an ACL entry on ``path`` but
      it's masked to ``---`` so reads still fail. Surfaces the
      mask-stuck-at-zero failure mode (see the comment in
      :func:`apply_acl_argv`). Hint: re-run ``agents access grant
      <name>`` to apply the ``m::rwx`` fix.
    - **missing** — no ACL entry at all; ``agents access grant``
      will set one.
    - **path-missing** — ``path`` doesn't exist (agent never started
      or data dir got removed).

    Uses ``os.access`` for the effective check — the kernel's own
    decision, which honors ACL masks correctly. Plain
    "entry present in getfacl output" is necessary but not sufficient.
    """
    if not path.exists():
        return "path-missing"
    try:
        owner = path.stat().st_uid
    except OSError:
        return "missing"
    if owner == host_uid:
        return "ok"
    needed = os.R_OK | os.X_OK if path.is_dir() else os.R_OK
    if os.access(path, needed):
        return "ok"
    if has_user_acl(path, host_uid):
        return "masked"
    return "missing"


# Re-exported for places that need it but want to avoid pulling
# ``pwd`` directly.
def host_user_name(host_uid: int) -> str:
    try:
        return pwd.getpwuid(host_uid).pw_name
    except KeyError:
        return str(host_uid)


_ = os  # silence the import linter; used by callers indirectly
