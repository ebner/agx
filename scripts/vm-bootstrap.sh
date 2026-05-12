#!/usr/bin/env bash
# Idempotent rootless-Docker bootstrap for the `agents` Multipass VM.
# Emits a final `BOOTSTRAP_OK ...` line that mp.parse_bootstrap_ok reads.
#
# Re-runnable: every step checks before doing. Failures land as
# `<field>=fail` in the handshake so the host CLI can refuse to proceed
# without burying the cause in subprocess noise.

set -uo pipefail

UIDMAP_OK=fail
DBUS_OK=fail
LINGERING_OK=fail
DOCKER_HOST_OK=fail
DAEMON_OK=fail
USER_SOCKET_ACTIVE_OK=fail
NET_OK=fail

# --- 1. Rootless-Docker prerequisites ---------------------------------------
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq >/dev/null 2>&1 || true
sudo apt-get install -y -qq \
    uidmap dbus-user-session curl ca-certificates iptables jq slirp4netns \
    fuse-overlayfs >/dev/null 2>&1 || true

command -v newuidmap >/dev/null 2>&1 && UIDMAP_OK=ok
[ -d /run/user ] && systemctl --version >/dev/null 2>&1 && DBUS_OK=ok

# --- 2. Lingering so the user systemd survives multipass exec disconnect ----
sudo loginctl enable-linger ubuntu >/dev/null 2>&1 || true
if loginctl show-user ubuntu --property=Linger 2>/dev/null | grep -q "Linger=yes"; then
    LINGERING_OK=ok
fi

# --- 3. Install rootless Docker as `ubuntu` ---------------------------------
UBUNTU_UID=$(id -u ubuntu)
UBUNTU_GID=$(id -g ubuntu)
SOCKET_PATH="/run/user/${UBUNTU_UID}/docker.sock"

# Run-as-ubuntu helper that mirrors a login shell so systemd-user is available.
as_ubuntu() {
    sudo -iu ubuntu env XDG_RUNTIME_DIR="/run/user/${UBUNTU_UID}" \
        DOCKER_HOST="unix://${SOCKET_PATH}" bash -c "$1"
}

if ! as_ubuntu 'command -v dockerd-rootless-setuptool.sh >/dev/null 2>&1'; then
    # Official install: pulls the standard docker-ce packages and lays down
    # the rootless setup tool. Network failures bubble up via NET_OK.
    as_ubuntu 'curl -fsSL https://get.docker.com/rootless | sh' >/dev/null 2>&1 || true
fi

as_ubuntu 'dockerd-rootless-setuptool.sh install --force' >/dev/null 2>&1 || true
as_ubuntu 'systemctl --user enable docker.service' >/dev/null 2>&1 || true
as_ubuntu 'systemctl --user start docker.service' >/dev/null 2>&1 || true

# --- 4. Export DOCKER_HOST in ubuntu's shell rc files -----------------------
EXPORT_LINE="export DOCKER_HOST=unix://${SOCKET_PATH}"
for rc in /home/ubuntu/.profile /home/ubuntu/.bashrc; do
    if [ -f "$rc" ] && ! sudo grep -qF "$EXPORT_LINE" "$rc"; then
        echo "$EXPORT_LINE" | sudo tee -a "$rc" >/dev/null
    fi
done

if as_ubuntu "test -S '${SOCKET_PATH}'"; then
    DOCKER_HOST_OK=ok
fi

# --- 5. Daemon reachable ----------------------------------------------------
if as_ubuntu "docker version --format '{{.Server.Version}}' >/dev/null 2>&1"; then
    DAEMON_OK=ok
fi

# --- 6. systemd --user docker.service is-active -----------------------------
if as_ubuntu 'systemctl --user is-active docker.service >/dev/null 2>&1'; then
    USER_SOCKET_ACTIVE_OK=ok
fi

# --- 7. Network reach (so image pulls don't fail mysteriously later) --------
if curl -sfI --max-time 5 https://ghcr.io >/dev/null 2>&1; then
    NET_OK=ok
fi

# --- 8. Handshake -----------------------------------------------------------
echo "BOOTSTRAP_OK uidmap=${UIDMAP_OK} dbus=${DBUS_OK} lingering=${LINGERING_OK} docker_host=${DOCKER_HOST_OK} daemon=${DAEMON_OK} user_socket_active=${USER_SOCKET_ACTIVE_OK} net=${NET_OK} ubuntu_uid=${UBUNTU_UID} ubuntu_gid=${UBUNTU_GID} socket_path=${SOCKET_PATH}"
