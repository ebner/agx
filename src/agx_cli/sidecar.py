"""Shared leaf helpers for project-scoped sidecar services.

Four small, stateless functions extracted from ``firecrawl_cmd`` so that
``inference_cmd``, ``ollama_cmd``, and the memory plugins stop carrying
their own copies with drifting semantics. Deliberately NOT a framework:
each caller keeps owning its own flow (locking, compose writes, state
files); these are the leaves they all share.

- :func:`docker_arch` — host CPU arch → Docker tag convention.
- :func:`resolve_digest` — pull a floating tag in the VM and pin it to
  its ``<repo>@sha256:…`` digest, allowlist-validated (rule #5) BEFORE
  the ref is returned to anything that could write it into compose.
- :func:`service_running` — compose-service liveness with one shared
  definition of "up": container state ``running`` OR ``healthy``.
- :func:`read_project_env` / :func:`write_project_env` — mode-aware
  (``data_dir_in_vm``) I/O for the project-level ``.env``.
"""

from __future__ import annotations

import platform

from agx_cli import compose, compose_runner, mp, ui
from agx_cli.config import AgentsConfig
from agx_cli.errors import RuntimeFailure


def docker_arch() -> str:
    """Map the host's CPU arch to Docker's tag convention.

    Multipass always launches a same-arch guest VM (no cross-arch
    virtualization), so the host's arch IS the in-VM Docker daemon's
    arch — which is what tag selection actually needs to match. The
    same identity holds trivially for the bare-metal backend, and
    libvirt keeps its own copy (``backends/libvirt.py``) because the
    backend layer must not import feature modules.

    Returns ``"arm64"`` on aarch64 / arm64, ``"amd64"`` everywhere else.
    Uncommon architectures (riscv64, ppc64le, etc.) fall through to
    ``"amd64"`` since no upstream sidecar image targets them — emulation
    is the only path and the amd64 tag is the safer bet than a guessed
    mapping.
    """
    m = platform.machine().lower()
    if m in ("aarch64", "arm64"):
        return "arm64"
    return "amd64"


def resolve_digest(cfg: AgentsConfig, floating_ref: str) -> str:
    """Pull ``floating_ref`` in the VM, harvest its repo-digest, and
    return the pinned ``<repo>@sha256:…`` form.

    The digest ref is checked against the image allowlist
    (:func:`compose.validate_image_ref`, architecture rule #5) BEFORE it
    is returned — nothing off-allowlist can flow from here into a
    compose write, no matter what the daemon reports.
    """
    ui.info(f"pulling {floating_ref}")
    pull = mp.exec_in_vm(cfg.vm.name, "docker", "pull", floating_ref, check=False)
    if pull.returncode != 0:
        raise RuntimeFailure(f"docker pull failed for {floating_ref}: {pull.stderr.strip()}")
    inspect = mp.exec_in_vm(
        cfg.vm.name,
        "docker",
        "image",
        "inspect",
        floating_ref,
        "--format",
        "{{index .RepoDigests 0}}",
        check=False,
    )
    if inspect.returncode != 0 or not inspect.stdout.strip():
        raise RuntimeFailure(
            f"could not resolve digest for {floating_ref}: {inspect.stderr.strip()}"
        )
    digest_ref = inspect.stdout.strip()
    if "@sha256:" not in digest_ref:
        raise RuntimeFailure(f"unparseable repo-digest for {floating_ref}: {digest_ref!r}")
    compose.validate_image_ref(digest_ref)
    return digest_ref


def service_running(cfg: AgentsConfig, svc: str) -> bool:
    """True iff the compose service's container is up.

    One shared definition of "up": docker compose reports the state as
    ``running`` for services without a healthcheck and ``healthy`` for
    services whose healthcheck passes — both count. (firecrawl's old
    hand-rolled parse accepted only ``running``, which misreported a
    healthy healthchecked service as down.)
    """
    # Lazy import: ops.py imports this module's callers for its doctor
    # checks; keeping ops out of this module's import graph avoids any
    # import-order coupling.
    from agx_cli.ops import _container_state_for

    result = compose_runner.ps(cfg)
    if result.returncode != 0:
        return False
    state = _container_state_for(result.stdout, svc).lower()
    return state in ("running", "healthy")


def read_project_env(cfg: AgentsConfig) -> dict[str, str]:
    """Mode-aware read of the project ``.env``. Returns ``{}`` when the
    file is missing.

    In ``--data-dir-in-vm`` mode the authoritative copy lives at
    ``<vm_agents_root>/.env`` inside the VM (compose reads it via
    ``--env-file``); the host has no copy, so it is fetched via
    ``multipass transfer``. In host-mount mode (and bare-metal) the
    host file IS the file compose reads.
    """
    if cfg.paths.data_dir_in_vm:
        import os as _os
        import uuid as _uuid

        vm_path = f"{cfg.paths.vm_agents_root}/.env"
        check = mp.exec_in_vm(cfg.vm.name, "test", "-f", vm_path, check=False)
        if check.returncode != 0:
            return {}
        host_temp = (
            compose.host_staging_dir(cfg)
            / f"projectenv-read-{_os.getpid()}-{_uuid.uuid4().hex[:8]}.env"
        )
        try:
            mp.transfer_from_vm(cfg.vm.name, vm_path, host_temp)
            return _parse_env_string(host_temp.read_text(encoding="utf-8"))
        finally:
            host_temp.unlink(missing_ok=True)
    path = cfg.paths.host_data_dir / ".env"
    if not path.exists():
        return {}
    return _parse_env_string(path.read_text(encoding="utf-8"))


def write_project_env(cfg: AgentsConfig, env: dict[str, str]) -> None:
    """Mode-aware write of the project ``.env``. In ``--data-dir-in-vm``
    mode, stages on host (``compose.host_staging_dir`` — never ``/tmp``)
    and ``multipass transfer``\\ s into the VM.

    The project ``.env`` is at ``<vm_agents_root>/.env`` (ubuntu-owned),
    NOT inside any container — so SFTP works in both modes. Unlike
    per-agent ``.env`` files which live under subuid-owned ``/opt/data``.

    Whole-file render from the dict: comments in the project ``.env``
    are not preserved. Callers must hold ``compose.acquire_lock`` around
    the read-modify-write.
    """
    content = _render_env_string(env)
    if cfg.paths.data_dir_in_vm:
        import os as _os
        import uuid as _uuid

        vm_path = f"{cfg.paths.vm_agents_root}/.env"
        host_temp = (
            compose.host_staging_dir(cfg)
            / f"projectenv-write-{_os.getpid()}-{_uuid.uuid4().hex[:8]}.env"
        )
        try:
            host_temp.write_text(content, encoding="utf-8")
            mp.transfer_to_vm(host_temp, cfg.vm.name, vm_path)
        finally:
            host_temp.unlink(missing_ok=True)
        return
    path = cfg.paths.host_data_dir / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    compose.await_vm_view(cfg, path)


def _parse_env_string(content: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v
    return out


def _render_env_string(env: dict[str, str]) -> str:
    return "\n".join(f"{k}={v}" for k, v in env.items()) + ("\n" if env else "")
