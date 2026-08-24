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

# Rootless Docker ships with NO container-log rotation: the json-file
# driver grows unbounded, and one chatty container can fill the VM disk
# (firecrawl-postgres wrote 2.8 GB in 5 days; disk-full wedged rabbitmq
# and killed web search — 2026-08 outage class). Write the rotation
# defaults BEFORE the daemon's first start so every container created on
# this VM inherits them. Never clobber an existing daemon.json (user
# customization); on re-runs against an existing VM the file is left
# alone, and a freshly written one takes effect for containers created
# after the next daemon restart. Containers created before this step
# keep unbounded logs until their next recreate (reseat/upgrade).
echo "[bootstrap] step 3b2: default container-log rotation (daemon.json)"
if ! sudo test -f /home/ubuntu/.config/docker/daemon.json; then
    as_ubuntu 'mkdir -p ~/.config/docker'
    sudo tee /home/ubuntu/.config/docker/daemon.json >/dev/null <<'DAEMONJSON'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
DAEMONJSON
    sudo chown ubuntu:ubuntu /home/ubuntu/.config/docker/daemon.json
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
