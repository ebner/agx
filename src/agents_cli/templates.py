"""Embedded templates.

These are kept in a Python module rather than separate files so they ride
with the wheel and tests can verify them without filesystem fixtures.

- ``VM_BOOTSTRAP_SCRIPT`` is piped via ``multipass exec ... bash -s`` at
  ``vm init``. The closing ``BOOTSTRAP_OK`` line is the handshake that
  ``mp.parse_bootstrap_ok`` consumes.
- ``FALLBACK_HERMES_IMAGE_TAG`` / ``FALLBACK_OPENCLAW_IMAGE_TAG`` are the
  conservative defaults used when ``vm init``'s GitHub Releases query
  times out or hits a network error. The pinned tag actually used by
  ``agents create`` lives in ``config.toml`` and can override these at
  any time.
- ``render_codex_sandbox_dockerfile`` builds the FROM-base Dockerfile
  for ``agents codex skill-enable``: derive whatever sandbox image the
  agent currently uses, install Node + the Codex CLI on top.
"""

from __future__ import annotations

from typing import Final

# Hermes + OpenClaw image tags — fallbacks when GitHub Releases query
# fails at `vm init`. Bump deliberately; let the runtime query handle
# the moving target.
FALLBACK_HERMES_IMAGE_TAG: Final = "v1.0.0"
FALLBACK_OPENCLAW_IMAGE_TAG: Final = "0.1.0"

# Hermes' default sandbox image (`terminal.docker_image`). Used as the
# FROM base for `agents codex skill-enable` unless the agent has set
# its own override.
HERMES_DEFAULT_SANDBOX_IMAGE: Final = "nikolaik/python-nodejs:python3.11-nodejs20"


VM_BOOTSTRAP_SCRIPT: Final = r"""#!/usr/bin/env bash
# Idempotent rootless-Docker bootstrap for the `agents` Multipass VM.
# Emits a final `BOOTSTRAP_OK ...` line that mp.parse_bootstrap_ok reads.
#
# Re-runnable: every step checks before doing. Failures land as
# `<field>=fail` in the handshake so the host CLI can refuse to proceed
# without burying the cause in subprocess noise. Stdout/stderr of each
# step is tee'd to /tmp/agents-bootstrap.log so the host CLI can pull
# it back on failure.

set -uo pipefail

LOG=/tmp/agents-bootstrap.log
: > "$LOG"
# Save the real stdout on fd 3 BEFORE the tee redirect. bash does not
# wait for process-substitution children at exit, so a final line written
# through the async tee can be lost before the host reads the stream —
# the closing BOOTSTRAP_OK handshake is emitted to fd 3 (a synchronous
# write to the real stdout) and tee'd into the log separately.
exec 3>&1
exec > >(tee -a "$LOG") 2>&1

UIDMAP_OK=fail
DBUS_OK=fail
LINGERING_OK=fail
DOCKER_HOST_OK=fail
DAEMON_OK=fail
USER_SOCKET_ACTIVE_OK=fail
NET_OK=fail

echo "[bootstrap] step 1: apt prerequisites"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq || echo "[bootstrap] apt-get update warned (continuing)"
sudo apt-get install -y -qq \
    uidmap dbus-user-session curl ca-certificates iptables jq slirp4netns \
    fuse-overlayfs \
    || echo "[bootstrap] apt install warned (continuing)"

command -v newuidmap >/dev/null 2>&1 && UIDMAP_OK=ok
[ -d /run/user ] && systemctl --version >/dev/null 2>&1 && DBUS_OK=ok

# Docker Inc's rootless install (get.docker.com/rootless) ships only the
# daemon + CLI — not the compose plugin. Add Docker's apt source and
# install `docker-compose-plugin` system-wide; the rootless docker CLI
# picks it up automatically from /usr/libexec/docker/cli-plugins/.
echo "[bootstrap] step 1d: install docker-compose-plugin via apt"
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc \
        || echo "[bootstrap] download docker apt gpg warned"
    sudo chmod a+r /etc/apt/keyrings/docker.asc || true
    DOCKER_LIST=/etc/apt/sources.list.d/docker.list
    DOCKER_ARCH="$(dpkg --print-architecture)"
    DOCKER_CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
    DOCKER_REPO_LINE="deb [arch=${DOCKER_ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${DOCKER_CODENAME} stable"
    if ! grep -qxF "$DOCKER_REPO_LINE" "$DOCKER_LIST" 2>/dev/null; then
        echo "$DOCKER_REPO_LINE" | sudo tee "$DOCKER_LIST" >/dev/null
    fi
    sudo apt-get update -qq || echo "[bootstrap] apt-get update (docker repo) warned"
    sudo apt-get install -y -qq docker-compose-plugin \
        || echo "[bootstrap] docker-compose-plugin install warned"
fi

# Rootless Docker on Ubuntu 24.04 needs nf_tables loaded; the setuptool
# refuses without it. Load it now (and persist for reboots).
echo "[bootstrap] step 1b: load nf_tables kernel module"
sudo modprobe nf_tables || echo "[bootstrap] modprobe nf_tables failed (continuing)"
if ! grep -qx nf_tables /etc/modules-load.d/agents-rootless.conf 2>/dev/null; then
    echo nf_tables | sudo tee /etc/modules-load.d/agents-rootless.conf >/dev/null
fi

# Rootless Docker installs binaries to /home/ubuntu/bin/. multipass exec
# inherits PATH from /etc/environment, NOT from .profile/.bashrc, so the
# toolkit-side `mp.exec_in_vm(..., "docker", ...)` calls would fail with
# `command not found` without this. Idempotent: only prepends once.
echo "[bootstrap] step 1c: put /home/ubuntu/bin on the system PATH"
if ! grep -q '/home/ubuntu/bin' /etc/environment; then
    sudo sed -i 's|^PATH="\(.*\)"|PATH="/home/ubuntu/bin:\1"|' /etc/environment \
        || echo "[bootstrap] /etc/environment PATH prepend warned"
fi

echo "[bootstrap] step 2: enable lingering for ubuntu"
sudo loginctl enable-linger ubuntu || echo "[bootstrap] enable-linger warned"
if loginctl show-user ubuntu --property=Linger 2>/dev/null | grep -q "Linger=yes"; then
    LINGERING_OK=ok
fi

UBUNTU_UID=$(id -u ubuntu)
UBUNTU_GID=$(id -g ubuntu)
SOCKET_PATH="/run/user/${UBUNTU_UID}/docker.sock"

as_ubuntu() {
    sudo -iu ubuntu env XDG_RUNTIME_DIR="/run/user/${UBUNTU_UID}" \
        DOCKER_HOST="unix://${SOCKET_PATH}" bash -c "$1"
}

# Ubuntu 23.10+ restricts unprivileged user namespaces, which blocks
# rootlesskit's fork-into-child step. Install the AppArmor profile that
# the rootlesskit project itself recommends BEFORE running docker install
# (the install internally invokes rootlesskit; without the profile, it
# fails partway through and leaves stale state that re-runs trip over).
echo "[bootstrap] step 3a: install AppArmor profile for rootlesskit"
sudo tee /etc/apparmor.d/home.ubuntu.bin.rootlesskit >/dev/null <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>

/home/ubuntu/bin/rootlesskit flags=(unconfined) {
  userns,

  include if exists <local/home.ubuntu.bin.rootlesskit>
}
PROFILE
sudo systemctl reload apparmor.service \
    || sudo systemctl restart apparmor.service \
    || echo "[bootstrap] apparmor reload/restart warned"

echo "[bootstrap] step 3b: download rootless Docker bits"
if ! as_ubuntu 'command -v dockerd-rootless-setuptool.sh >/dev/null 2>&1'; then
    as_ubuntu 'curl -fsSL https://get.docker.com/rootless | sh' \
        || echo "[bootstrap] get.docker.com/rootless install warned"
fi

echo "[bootstrap] step 3c: setuptool install (clean stale state only if daemon is broken)"
# The uninstall + rm -rf below wipe every container and the whole image
# cache. They exist to clear stale state left by a previously FAILED
# install — but `agents vm init` re-runs this script on an existing VM,
# so the destructive path must be guarded: a working `docker info` means
# there is nothing stale to clean and everything to lose.
if as_ubuntu 'docker info >/dev/null 2>&1'; then
    echo "[bootstrap] docker already functional — skipping uninstall/reinstall"
else
    as_ubuntu 'dockerd-rootless-setuptool.sh uninstall -f' 2>/dev/null || true
    as_ubuntu 'rm -rf /home/ubuntu/.local/share/docker' || true
    as_ubuntu 'dockerd-rootless-setuptool.sh install' \
        || echo "[bootstrap] setuptool install warned"
fi
as_ubuntu 'systemctl --user enable docker.service' \
    || echo "[bootstrap] systemctl --user enable warned"
as_ubuntu 'systemctl --user start docker.service' \
    || echo "[bootstrap] systemctl --user start warned"

echo "[bootstrap] step 4: export DOCKER_HOST in ubuntu's shell rc files"
EXPORT_LINE="export DOCKER_HOST=unix://${SOCKET_PATH}"
for rc in /home/ubuntu/.profile /home/ubuntu/.bashrc; do
    if [ -f "$rc" ] && ! sudo grep -qF "$EXPORT_LINE" "$rc"; then
        echo "$EXPORT_LINE" | sudo tee -a "$rc" >/dev/null
    fi
done

if as_ubuntu "test -S '${SOCKET_PATH}'"; then
    DOCKER_HOST_OK=ok
fi

echo "[bootstrap] step 5: probe Docker daemon"
if as_ubuntu "docker version --format '{{.Server.Version}}'"; then
    DAEMON_OK=ok
fi

echo "[bootstrap] step 6: systemctl --user is-active docker.service"
if as_ubuntu 'systemctl --user is-active docker.service'; then
    USER_SOCKET_ACTIVE_OK=ok
fi

echo "[bootstrap] step 7: network reach probe"
# Probe the real Docker install endpoint (returns 200 OK on GET). ghcr.io's
# root is 405 to HEAD and 401 to /v2/, which `-f` treats as failure.
if curl -sf -o /dev/null --max-time 10 https://download.docker.com/linux/ubuntu/gpg; then
    NET_OK=ok
fi

echo "[bootstrap] handshake"
# Emit to fd 3 — the real stdout saved before the tee redirect — so the
# handshake can't be dropped in the tee child's exit race; tee a copy
# into the log so failure diagnostics still show it.
echo "BOOTSTRAP_OK uidmap=${UIDMAP_OK} dbus=${DBUS_OK} lingering=${LINGERING_OK} docker_host=${DOCKER_HOST_OK} daemon=${DAEMON_OK} user_socket_active=${USER_SOCKET_ACTIVE_OK} net=${NET_OK} ubuntu_uid=${UBUNTU_UID} ubuntu_gid=${UBUNTU_GID} socket_path=${SOCKET_PATH}" | tee -a "$LOG" >&3
"""


# s6-overlay cont-init.d script, bind-mounted read-only into every Hermes
# container by the compose fragment. Runs as root on EVERY boot — after the
# image's own ``01-hermes-setup`` usermod, before any service starts — so the
# toolkit no longer has to chase rootless-Docker permission drift with
# ``agents fix-perms`` after each upgrade/recreate. See
# ``compose.ensure_cont_init_script`` and the "durable in-container perms
# hook" pitfall in CLAUDE.md.
HERMES_CONT_INIT_FIXPERMS: Final = r"""#!/command/with-contenv sh
# 99-toolkit-fixperms — MANAGED BY THE `agents` HOST CLI. DO NOT EDIT.
#
# Runs as root on every container boot (s6-overlay cont-init.d), after the
# image's 01-hermes-setup usermod and before any service starts. Repairs two
# rootless-Docker permission problems the toolkit otherwise has to chase with
# `agents fix-perms` after every upgrade/recreate:
#
#   1. Image-baked install paths (/opt/hermes/scripts, /home/hermes) live on
#      the container overlay fs owned by the image build-time uid; a recreate
#      resets them, so the runtime `hermes` user can't `npm install` bridge
#      deps etc. Chown them AS ROOT — the overlay fs honors the container's
#      CAP_CHOWN. We chown to the NUMERIC uid:gid of the live `hermes` user
#      (resolved via `id`), not the name `hermes:hermes`: a name target would
#      silently no-op (with the trailing `exit 0`) if the group were ever
#      renamed, whereas the numeric form mirrors fixperms_cmd and can't drift.
#      Prune the :ro Codex import bridge so chown never touches a read-only
#      mount.
#
#   2. The gateway log dir. The image's gateway log run script does a root
#      `mkdir -p` under /opt/data, but /opt/data is a 9p-backed bind mount
#      that ignores the container's root caps — only the hermes uid can write
#      there. Pre-create the tree AS hermes (via s6-setuidgid, called by
#      absolute path so it resolves regardless of the cont-init PATH) so the
#      root-side mkdir no-ops and s6-log (which runs as hermes) can write.
#      (The upstream run script's own later `chown -R "$log_dir"` is `|| true`,
#      so it stays non-fatal once the leaf already exists hermes-owned.)
#
#   3. The WhatsApp bridge's node_modules. It lives on the container overlay
#      fs, so every recreate wipes it — and even when it is reinstalled, the
#      bridge's @whiskeysockets/baileys **git** dependency sometimes lands
#      empty (ERR_MODULE_NOT_FOUND at runtime). When the bridge's
#      package.json is present but node_modules — or the baileys package
#      inside it — is missing/empty, self-heal with `npm ci`. Constraints
#      encoded below:
#        - npm runs AS the hermes uid via s6-setuidgid with the NUMERIC
#          "$HUID:$HGID" — the exact ids the chown in (1) just applied to the
#          overlay paths npm writes to (/opt/hermes/scripts/whatsapp-bridge,
#          $HOME/.npm cache under /home/hermes); a name lookup could drift
#          from those ids exactly the way the chown comment explains.
#        - it is BACKGROUNDED: npm ci can take minutes, cont-init blocks
#          every service start, and a slow heal must never stall boot. The
#          orphaned job is reparented to the container's init and keeps
#          running after this script exits.
#        - all output appends to /opt/data/logs/toolkit-npm-ci.log, which is
#          on the 9p mount — so the log must be opened AS hermes (same
#          split-privilege rule as (2)); the redirection happens inside the
#          setuidgid'd shell, never in the root-side wrapper.
#        - a pid file under /tmp guards against a crash-looping container
#          stacking concurrent installs. /tmp is the root-writable overlay:
#          wiped on recreate (which also wipes node_modules and should
#          re-arm the heal from scratch), preserved across in-place restarts
#          (where a previous boot's install may still be running — the
#          kill -0 liveness probe detects that; a stale pid from before a
#          restart is dead and gets overwritten).
#      Every failure path logs a line (as hermes) or falls back silently;
#      none of them can affect the script's exit status.
#
# Idempotent. Ends with `exit 0`: a non-zero cont-init.d exit can abort
# container init depending on S6_BEHAVIOUR_IF_STAGE2_FAILS, so the hook must
# never let a best-effort chown/mkdir/npm failure surface.

HUID="$(id -u hermes 2>/dev/null || true)"
HGID="$(id -g hermes 2>/dev/null || true)"
if [ -n "$HUID" ] && [ -n "$HGID" ]; then
    if [ -e /opt/hermes/scripts ]; then
        find /opt/hermes/scripts -exec chown "$HUID:$HGID" {} + 2>/dev/null
    fi
    if [ -e /home/hermes ]; then
        find /home/hermes -path /home/hermes/.codex -prune -o -exec chown "$HUID:$HGID" {} + 2>/dev/null
    fi
fi

/command/s6-setuidgid hermes mkdir -p /opt/data/logs/gateways/default 2>/dev/null

# --- 3. WhatsApp-bridge self-heal (rationale in the header) -----------------
BRIDGE=/opt/hermes/scripts/whatsapp-bridge
BAILEYS="$BRIDGE/node_modules/@whiskeysockets/baileys"
NPM_LOG=/opt/data/logs/toolkit-npm-ci.log
NPM_LOCK=/tmp/toolkit-npm-ci.pid

# Append one line to the heal log AS hermes — /opt/data is 9p-backed, so a
# root-side open() for append would be denied (split-privilege rule).
npm_ci_log() {
    echo "[toolkit-npm-ci] $(date -u +%Y-%m-%dT%H:%M:%SZ) $1" \
        | /command/s6-setuidgid "$HUID:$HGID" sh -c "cat >> $NPM_LOG" 2>/dev/null
}

# Heal is needed iff the bridge is installed (package.json present) but its
# node_modules — or the baileys git-dep inside it — is missing or empty.
bridge_needs_heal() {
    [ -f "$BRIDGE/package.json" ] || return 1
    [ -d "$BRIDGE/node_modules" ] || return 0
    [ -d "$BAILEYS" ] || return 0
    [ -z "$(ls -A "$BAILEYS" 2>/dev/null)" ]
}

if [ -n "$HUID" ] && [ -n "$HGID" ] && bridge_needs_heal; then
    /command/s6-setuidgid "$HUID:$HGID" mkdir -p /opt/data/logs 2>/dev/null
    if [ -f "$NPM_LOCK" ] && kill -0 "$(cat "$NPM_LOCK" 2>/dev/null)" 2>/dev/null; then
        npm_ci_log "skip: a previous heal is still running (pid $(cat "$NPM_LOCK" 2>/dev/null))"
    else
        # The cont-init PATH is minimal and may miss the node install. Try the
        # ambient PATH first (apt installs land at /usr/bin), then probe the
        # locations node distributions ship to (/usr/local/bin for official
        # tarball/docker-node layouts, plus less common prefixes).
        NPM_BIN="$(command -v npm 2>/dev/null || true)"
        if [ -z "$NPM_BIN" ]; then
            for d in /usr/local/bin /usr/bin /opt/node/bin /usr/local/lib/nodejs/bin; do
                if [ -x "$d/npm" ]; then NPM_BIN="$d/npm"; break; fi
            done
        fi
        if [ -z "$NPM_BIN" ]; then
            npm_ci_log "skip: npm not found (PATH, /usr/local/bin, /usr/bin) - cannot heal $BRIDGE"
        else
            NPM_DIR="$(dirname "$NPM_BIN")"
            (
                NPM_DIR="$NPM_DIR" BRIDGE="$BRIDGE" NPM_LOG="$NPM_LOG" \
                    /command/s6-setuidgid "$HUID:$HGID" sh -c '
                        exec >>"$NPM_LOG" 2>&1
                        echo "[toolkit-npm-ci] $(date -u +%Y-%m-%dT%H:%M:%SZ) npm ci starting in $BRIDGE"
                        export HOME="${HOME:-/home/hermes}"
                        export PATH="$NPM_DIR:$PATH"
                        cd "$BRIDGE" || exit 1
                        npm ci
                        echo "[toolkit-npm-ci] $(date -u +%Y-%m-%dT%H:%M:%SZ) npm ci exit=$?"
                    '
                rm -f "$NPM_LOCK"
            ) >/dev/null 2>&1 &
            echo $! > "$NPM_LOCK" 2>/dev/null || true
            npm_ci_log "backgrounded npm ci (pid $!) in $BRIDGE, log: $NPM_LOG"
        fi
    fi
fi

exit 0
"""


def render_codex_sandbox_dockerfile(base_image: str) -> str:
    """Render the Dockerfile for `agents codex skill-enable`.

    The derived image adds Node + the Codex CLI on top of whatever the
    agent's current ``terminal.docker_image`` is, so user customizations
    survive. It also sets ``CODEX_HOME=/codex`` so we can mount the
    toolkit-scoped auth at a fixed, user-independent path — works for
    base images that run as root, ``pn``, ``node``, or anything else.
    """
    return (
        f"FROM {base_image}\n"
        "RUN apt-get update \\\n"
        " && apt-get install -y --no-install-recommends nodejs npm curl ca-certificates \\\n"
        " && npm install -g @openai/codex \\\n"
        " && apt-get clean && rm -rf /var/lib/apt/lists/* \\\n"
        " && mkdir -p /codex\n"
        "ENV CODEX_HOME=/codex\n"
    )


def initial_compose_content() -> str:
    """Empty compose file written into the data-dir at `vm init`."""
    return "services: {}\n"


def initial_env_content(host_uid: int, host_gid: int) -> str:
    """The .env file written alongside the compose at `vm init`."""
    return f"HERMES_UID={host_uid}\nHERMES_GID={host_gid}\n"
