#!/usr/bin/env bash
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

echo "[bootstrap] step 3c: setuptool install (clean any stale state first)"
as_ubuntu 'dockerd-rootless-setuptool.sh uninstall -f' 2>/dev/null || true
as_ubuntu 'rm -rf /home/ubuntu/.local/share/docker' || true
as_ubuntu 'dockerd-rootless-setuptool.sh install' \
    || echo "[bootstrap] setuptool install warned"
as_ubuntu 'systemctl --user enable docker.service' \
    || echo "[bootstrap] systemctl --user enable warned"
as_ubuntu 'systemctl --user start docker.service' \
    || echo "[bootstrap] systemctl --user start warned"

# `incus exec` resolves the binary name BEFORE applying any --env PATH=
# we pass, so prepending /home/ubuntu/bin to /etc/environment isn't
# enough on its own — the incus agent runs as root with its own minimal
# PATH and won't find /home/ubuntu/bin/docker. Symlink the rootless
# docker binaries into /usr/local/bin (which IS in the agent's
# resolution path) so plain `docker ...` works from `incus exec`.
echo "[bootstrap] step 3d: symlink rootless docker binaries into /usr/local/bin"
for binary in docker dockerd containerd ctr runc; do
    src="/home/ubuntu/bin/${binary}"
    dst="/usr/local/bin/${binary}"
    if [ -e "$src" ]; then
        sudo ln -sf "$src" "$dst" || echo "[bootstrap] symlink ${binary} warned"
    fi
done

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
echo "BOOTSTRAP_OK uidmap=${UIDMAP_OK} dbus=${DBUS_OK} lingering=${LINGERING_OK} docker_host=${DOCKER_HOST_OK} daemon=${DAEMON_OK} user_socket_active=${USER_SOCKET_ACTIVE_OK} net=${NET_OK} ubuntu_uid=${UBUNTU_UID} ubuntu_gid=${UBUNTU_GID} socket_path=${SOCKET_PATH}"
