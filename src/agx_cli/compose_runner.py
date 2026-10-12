"""Wraps ``docker compose`` verbs via ``multipass exec``.

The host never talks to the in-VM Docker daemon directly — the
``--project-directory``, ``-f``, and ``--env-file`` flags are pinned to
the VM-side paths every call. All wrappers return :class:`mp.MpResult`
so callers can inspect rc/stdout/stderr.
"""

from __future__ import annotations

from agx_cli import compose, mp
from agx_cli.config import AgentsConfig
from agx_cli.mp import MpResult


def _base_argv(cfg: AgentsConfig) -> list[str]:
    # docker compose only auto-merges docker-compose.override.yml when no
    # -f is given at all; since we pin the main compose explicitly, the
    # override (architecture rule #10 — `agx mount` + user hand edits)
    # must be passed explicitly too, or it's silently ignored.
    root = str(cfg.paths.vm_agents_root)
    return [
        "docker",
        "compose",
        "--project-directory",
        root,
        "-f",
        f"{root}/docker-compose.yml",
        *compose.override_file_args(cfg),
        "--env-file",
        f"{root}/.env",
    ]


def up(cfg: AgentsConfig, *services: str, detach: bool = True) -> MpResult:
    argv = [*_base_argv(cfg), "up"]
    if detach:
        argv.append("-d")
    argv.extend(services)
    return mp.exec_in_vm(cfg.vm.name, *argv)


def down(cfg: AgentsConfig, *services: str, remove_orphans: bool = False) -> MpResult:
    """Bring the whole compose project (or a service subset) down.

    Note that ``docker compose down`` behavior with explicit services is
    version-dependent; prefer ``stop`` + ``rm`` for a service-scoped
    teardown, which is what ``agents_cmd.remove`` and ``rename`` use.
    """
    argv = [*_base_argv(cfg), "down"]
    if remove_orphans:
        argv.append("--remove-orphans")
    argv.extend(services)
    return mp.exec_in_vm(cfg.vm.name, *argv)


def rm(cfg: AgentsConfig, *services: str, force: bool = True, stop: bool = True) -> MpResult:
    """Remove specific compose services' containers. ``force`` skips the
    prompt; ``stop`` first stops them. Service-scoped, unlike ``down``."""
    argv = [*_base_argv(cfg), "rm"]
    if force:
        argv.append("-f")
    if stop:
        argv.append("-s")
    argv.extend(services)
    return mp.exec_in_vm(cfg.vm.name, *argv)


def run_as_root(cfg: AgentsConfig, service: str, *cmd: str) -> MpResult:
    """One-off ``compose run --rm --no-deps --user 0`` of ``cmd`` in a
    fresh container of ``service`` (its volumes, override included),
    entrypoint bypassed. Root here is the rootless daemon's userns root,
    which can chown within the subuid range on the service's mounts."""
    return mp.exec_in_vm(
        cfg.vm.name,
        *_base_argv(cfg),
        "run",
        "--rm",
        "--no-deps",
        "--user",
        "0",
        "--entrypoint",
        cmd[0],
        service,
        *cmd[1:],
        check=False,
    )


def stop(cfg: AgentsConfig, *services: str) -> MpResult:
    return mp.exec_in_vm(cfg.vm.name, *_base_argv(cfg), "stop", *services)


def start(cfg: AgentsConfig, *services: str) -> MpResult:
    return mp.exec_in_vm(cfg.vm.name, *_base_argv(cfg), "start", *services)


def restart(cfg: AgentsConfig, *services: str) -> MpResult:
    return mp.exec_in_vm(cfg.vm.name, *_base_argv(cfg), "restart", *services)


def pull(cfg: AgentsConfig, *services: str) -> MpResult:
    return mp.exec_in_vm(cfg.vm.name, *_base_argv(cfg), "pull", *services)


def ps(cfg: AgentsConfig, *, fmt: str = "json") -> MpResult:
    return mp.exec_in_vm(cfg.vm.name, *_base_argv(cfg), "ps", "--format", fmt, check=False)


def logs(
    cfg: AgentsConfig, service: str, *, follow: bool = False, since: str | None = None
) -> MpResult:
    argv = [*_base_argv(cfg), "logs"]
    if follow:
        argv.append("-f")
    if since:
        argv.extend(["--since", since])
    argv.append(service)
    return mp.exec_in_vm(cfg.vm.name, *argv)


def docker_image_inspect(cfg: AgentsConfig, image_ref: str) -> MpResult:
    """Used by the upgrade state machine to verify ``docker compose pull``
    actually landed all layers (exit 0 isn't enough on flaky networks).
    """
    return mp.exec_in_vm(cfg.vm.name, "docker", "image", "inspect", image_ref, check=False)
