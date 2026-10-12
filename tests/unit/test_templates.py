"""Templates: bootstrap script structure, sandbox Dockerfile rendering."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from agx_cli import templates
from agx_cli.mp import parse_bootstrap_ok
from agx_cli.templates import (
    FALLBACK_HERMES_IMAGE_TAG,
    FALLBACK_OPENCLAW_IMAGE_TAG,
    HERMES_DEFAULT_SANDBOX_IMAGE,
    render_codex_sandbox_dockerfile,
)


def test_bootstrap_script_is_bash_with_safety_flags() -> None:
    s = templates.VM_BOOTSTRAP_SCRIPT
    assert s.startswith("#!/usr/bin/env bash")
    assert "set -uo pipefail" in s


def test_bootstrap_script_emits_handshake_for_every_required_field() -> None:
    """The trailing echo must mention every field the parser requires."""
    s = templates.VM_BOOTSTRAP_SCRIPT
    handshake_lines = [line for line in s.splitlines() if 'echo "BOOTSTRAP_OK' in line]
    assert len(handshake_lines) == 1
    handshake = handshake_lines[0]
    for field in [
        "uidmap=",
        "dbus=",
        "lingering=",
        "docker_host=",
        "daemon=",
        "user_socket_active=",
        "net=",
        "ubuntu_uid=",
        "ubuntu_gid=",
        "socket_path=",
    ]:
        assert field in handshake, f"handshake missing {field}: {handshake}"


def test_bootstrap_handshake_parses_to_failed_status_when_simulated() -> None:
    """If every shell variable were left at its `fail` default, the parser
    would correctly classify the run as failed. Exercises the contract
    between the script template and the parser."""
    simulated_output = (
        "BOOTSTRAP_OK uidmap=fail dbus=fail lingering=fail docker_host=fail "
        "daemon=fail user_socket_active=fail net=fail ubuntu_uid=1000 "
        "ubuntu_gid=1000 socket_path=/run/user/1000/docker.sock"
    )
    status = parse_bootstrap_ok(simulated_output)
    assert not status.is_ok()
    assert len(status.fail_reasons()) == 7  # every required ok-field reports fail


def test_bootstrap_step_3c_destructive_cleanup_guarded_by_docker_info() -> None:
    """`agx vm init` re-runs the bootstrap on an existing VM. The
    uninstall + image-cache rm -rf in step 3c must therefore only run when
    `docker info` fails as ubuntu — otherwise every re-init would wipe all
    containers and the whole image cache."""
    s = templates.VM_BOOTSTRAP_SCRIPT
    guard = s.index("if as_ubuntu 'docker info >/dev/null 2>&1'; then")
    uninstall = s.index("dockerd-rootless-setuptool.sh uninstall -f")
    rm_rf = s.index("rm -rf /home/ubuntu/.local/share/docker")
    assert guard < uninstall, "uninstall must come after the docker-info guard"
    assert guard < rm_rf, "image-cache rm -rf must come after the docker-info guard"
    # The guarded block closes before the (idempotent, unconditional)
    # systemctl enable/start that keep the daemon healthy on both paths.
    fi = s.index("\nfi\n", rm_rf)
    enable = s.index("systemctl --user enable docker.service")
    assert fi < enable, "the fi must close the destructive block before enable/start"


def test_bootstrap_saves_real_stdout_before_tee_redirect() -> None:
    """fd 3 must capture the real stdout BEFORE the process-substitution
    tee takes over fd 1, or the handshake has nowhere flush-safe to go."""
    s = templates.VM_BOOTSTRAP_SCRIPT
    fd3 = s.index("exec 3>&1")
    tee = s.index('exec > >(tee -a "$LOG") 2>&1')
    assert fd3 < tee


def test_bootstrap_handshake_bypasses_async_tee() -> None:
    """bash doesn't wait for process-substitution children at exit, so the
    BOOTSTRAP_OK line must be written synchronously to fd 3 (the saved real
    stdout) — while still landing in the log for failure diagnostics."""
    s = templates.VM_BOOTSTRAP_SCRIPT
    handshake_lines = [line for line in s.splitlines() if 'echo "BOOTSTRAP_OK' in line]
    assert len(handshake_lines) == 1
    handshake = handshake_lines[0]
    assert handshake.rstrip().endswith(">&3")
    assert 'tee -a "$LOG"' in handshake  # copy still lands in the log


def test_render_codex_sandbox_dockerfile_uses_base() -> None:
    dockerfile = render_codex_sandbox_dockerfile(HERMES_DEFAULT_SANDBOX_IMAGE)
    assert dockerfile.startswith(f"FROM {HERMES_DEFAULT_SANDBOX_IMAGE}\n")
    assert "npm install -g @openai/codex" in dockerfile


def test_render_codex_sandbox_dockerfile_preserves_custom_base() -> None:
    dockerfile = render_codex_sandbox_dockerfile("custom/sandbox:abc123")
    assert "FROM custom/sandbox:abc123" in dockerfile


def test_fallback_tags_are_non_empty() -> None:
    """Sanity — these get written to config.toml if GitHub Releases is unreachable."""
    assert FALLBACK_HERMES_IMAGE_TAG
    assert FALLBACK_OPENCLAW_IMAGE_TAG


def test_initial_env_content_renders_uid_gid() -> None:
    out = templates.initial_env_content(1001, 1002)
    assert "HERMES_UID=1001" in out
    assert "HERMES_GID=1002" in out


def test_initial_compose_content_is_empty_services_dict() -> None:
    assert templates.initial_compose_content() == "services: {}\n"


def test_scripts_vm_bootstrap_sh_matches_template_constant() -> None:
    """The on-disk scripts/vm-bootstrap.sh exists (for ops review) and must
    stay in sync with templates.VM_BOOTSTRAP_SCRIPT (the source the toolkit
    actually pipes into the VM)."""
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "vm-bootstrap.sh"
    assert script_path.exists(), (
        "scripts/vm-bootstrap.sh is missing; regenerate with: "
        "python -c 'from agx_cli.templates import VM_BOOTSTRAP_SCRIPT; "
        "import sys; sys.stdout.write(VM_BOOTSTRAP_SCRIPT)' > scripts/vm-bootstrap.sh"
    )
    on_disk = script_path.read_text(encoding="utf-8")
    assert on_disk == templates.VM_BOOTSTRAP_SCRIPT, (
        "scripts/vm-bootstrap.sh has drifted from templates.VM_BOOTSTRAP_SCRIPT — "
        "regenerate from the template."
    )


# ---------------------------------------------------------------------------
# HERMES_CONT_INIT_FIXPERMS — the managed s6 cont-init.d hook, bind-mounted
# read-only at /etc/cont-init.d/99-toolkit-fixperms in every Hermes container
# (materialized by compose.ensure_cont_init_script; body-content assertions on
# the perms half also live in test_compose.py's ensure_cont_init tests).
# ---------------------------------------------------------------------------


def test_cont_init_hook_shebang_and_managed_header() -> None:
    s = templates.HERMES_CONT_INIT_FIXPERMS
    assert s.startswith("#!/command/with-contenv sh")
    assert "MANAGED BY THE `agx` HOST CLI. DO NOT EDIT." in s


def test_cont_init_hook_keeps_perms_behaviour() -> None:
    """The pre-existing split-privilege behaviors must survive the bridge-heal
    addition: numeric-uid chown of the overlay install paths as root, the
    gateway logdir pre-created as hermes, and the unconditional trailing
    exit 0 (a non-zero cont-init exit can abort container init)."""
    s = templates.HERMES_CONT_INIT_FIXPERMS
    assert "id -u hermes" in s and "id -g hermes" in s
    assert 'chown "$HUID:$HGID"' in s
    assert "/opt/hermes/scripts" in s
    assert "/command/s6-setuidgid hermes mkdir -p /opt/data/logs/gateways/default" in s
    assert s.rstrip().endswith("exit 0")


def test_cont_init_bridge_heal_guard_conditions() -> None:
    """`npm ci` must be gated: bridge actually installed (package.json) AND
    node_modules — or the baileys git-dep inside it — missing/empty. Never an
    unconditional reinstall on every boot."""
    s = templates.HERMES_CONT_INIT_FIXPERMS
    assert '[ -f "$BRIDGE/package.json" ] || return 1' in s
    assert "node_modules/@whiskeysockets/baileys" in s
    # the "git dep reinstalls empty" failure mode: present-but-empty dir
    assert '[ -z "$(ls -A "$BAILEYS" 2>/dev/null)" ]' in s
    assert "bridge_needs_heal; then" in s


def test_cont_init_npm_ci_runs_as_hermes_numeric_uid() -> None:
    """npm writes to the overlay paths the preceding chown just set to
    $HUID:$HGID, so it runs via s6-setuidgid with the same NUMERIC ids (name
    lookup could drift, same rationale as the chown), by absolute /command
    path (cont-init PATH lacks /command). The heal log lives on 9p-backed
    /opt/data where container-root cannot write: both the logs-dir mkdir and
    the log-file open must happen AS hermes — the append redirection sits
    INSIDE the setuidgid'd shell, never on the root side."""
    s = templates.HERMES_CONT_INIT_FIXPERMS
    assert '/command/s6-setuidgid "$HUID:$HGID" sh -c' in s
    assert '/command/s6-setuidgid "$HUID:$HGID" mkdir -p /opt/data/logs' in s
    assert 'exec >>"$NPM_LOG" 2>&1' in s
    assert "NPM_LOG=/opt/data/logs/toolkit-npm-ci.log" in s


def test_cont_init_npm_ci_is_backgrounded_and_ordered() -> None:
    """npm ci can take minutes; it must run inside a backgrounded subshell so
    boot never blocks, and only after the chown block has fixed the overlay
    ownership npm is about to write through."""
    s = templates.HERMES_CONT_INIT_FIXPERMS
    m = re.search(r"^\s*npm ci$", s, re.MULTILINE)
    assert m is not None, "expected a bare `npm ci` invocation"
    bg_close = s.index(") >/dev/null 2>&1 &")
    assert s.index('chown "$HUID:$HGID"') < m.start(), "heal must follow the chown block"
    assert m.start() < bg_close, "npm ci must live inside the backgrounded subshell"
    # backgrounded, not the last word: the script still falls through to exit 0
    assert bg_close < s.rindex("exit 0")


def test_cont_init_npm_lock_under_tmp_with_liveness_probe() -> None:
    """The double-run guard is a pid file on /tmp: root-writable overlay fs
    (no 9p DAC in the way), wiped on recreate (which also wipes node_modules,
    re-arming the heal), preserved across restarts where the previous boot's
    install may still be running — hence the kill -0 liveness probe instead
    of bare file existence."""
    s = templates.HERMES_CONT_INIT_FIXPERMS
    assert "NPM_LOCK=/tmp/toolkit-npm-ci.pid" in s
    assert 'kill -0 "$(cat "$NPM_LOCK" 2>/dev/null)"' in s
    assert 'rm -f "$NPM_LOCK"' in s  # released once the heal finishes


def test_cont_init_npm_resolved_defensively() -> None:
    """The cont-init PATH may not include the image's node bin dir; the hook
    probes `command -v` plus the common install prefixes and logs a skip
    (rather than failing boot) when npm truly is not there."""
    s = templates.HERMES_CONT_INIT_FIXPERMS
    assert "command -v npm" in s
    assert "/usr/local/bin" in s and "/usr/bin" in s
    assert "npm not found" in s


def test_cont_init_creates_runtime_home_before_chown() -> None:
    """The hook must create /home/hermes (the image doesn't; it used to
    exist only as the parent of the retired .codex bridge mount) before
    the existence-gated chown, or npm's cache/log dir is missing and the
    bridge's `npm ci` fails half-way on every boot."""
    s = templates.HERMES_CONT_INIT_FIXPERMS
    assert "mkdir -p /home/hermes" in s
    assert s.index("mkdir -p /home/hermes") < s.index("find /home/hermes")


def test_cont_init_hook_is_valid_posix_sh(tmp_path: Path) -> None:
    """The hook is `#!/command/with-contenv sh` — POSIX sh, not bash. sh -n
    catches bashisms and quoting errors before the template ships into a
    container as a boot-critical cont-init script."""
    hook = tmp_path / "99-toolkit-fixperms"
    hook.write_text(templates.HERMES_CONT_INIT_FIXPERMS, encoding="utf-8")
    res = subprocess.run(["sh", "-n", str(hook)], capture_output=True, text=True)
    assert res.returncode == 0, f"sh -n rejected the hook:\n{res.stderr}"
