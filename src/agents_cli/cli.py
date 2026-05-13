"""Root CLI group: wires every subcommand into click.

Each subcommand body funnels through ``_run`` so :class:`CliError`
subclasses translate to their declared exit codes (``UserError`` -> 1,
``RuntimeFailure`` -> 2, ``HealthCheckFailure`` -> 3), and any other
exception becomes a generic runtime failure (2) with a stack trace
when ``--verbose`` is set.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from agents_cli import (
    __version__,
    agents_cmd,
    export_cmd,
    ops,
    ui,
    upgrades,
)
from agents_cli import (
    codex as codex_mod,
)
from agents_cli import (
    config as cfg_mod,
)
from agents_cli import (
    vm as vm_mod,
)


def _run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Invoke a backend function and translate any exception into an exit code."""
    try:
        fn(*args, **kwargs)
    except SystemExit:
        raise
    except BaseException as e:
        sys.exit(ui.handle_cli_errors(e))


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON output.")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip confirmation prompts.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
@click.pass_context
def main(ctx: click.Context, json_output: bool, assume_yes: bool, verbose: bool) -> None:
    """Manage AI agents inside a Multipass VM."""
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_output
    ctx.obj["yes"] = assume_yes
    ctx.obj["verbose"] = verbose


# ---------- vm lifecycle ----------


@main.group()
def vm() -> None:
    """Multipass VM lifecycle."""


@vm.command("init")
@click.option(
    "--data-dir", required=True, type=click.Path(), help="Host directory where agent state lives."
)
@click.option(
    "--data-dir-in-vm", is_flag=True, help="Keep data on the VM disk instead of host-mounting."
)
@click.option("--cpus", type=int)
@click.option("--memory", "memory_gb", type=int, metavar="GB")
@click.option("--disk", "disk_gb", type=int, metavar="GB")
@click.option(
    "--ubuntu-release",
    "ubuntu_release",
    default=None,
    metavar="VERSION",
    help="Override Ubuntu LTS for the VM (default 24.04). Use 26.04 once Docker CE's repos include it.",
)
def vm_init(
    data_dir: str,
    data_dir_in_vm: bool,
    cpus: int | None,
    memory_gb: int | None,
    disk_gb: int | None,
    ubuntu_release: str | None,
) -> None:
    """Provision the Multipass VM and mount the host data dir."""
    _run(
        vm_mod.init,
        data_dir=Path(data_dir).expanduser(),
        data_dir_in_vm=data_dir_in_vm,
        cpus=cpus,
        memory_gb=memory_gb,
        disk_gb=disk_gb,
        ubuntu_release=ubuntu_release,
    )


@vm.command("start")
def vm_start() -> None:
    """Start the VM and re-verify the data-dir mount."""
    _run(vm_mod.start)


@vm.command("stop")
def vm_stop() -> None:
    """Stop the VM."""
    _run(vm_mod.stop)


@vm.command("resize")
@click.option("--cpus", type=int, default=None, help="New CPU count.")
@click.option(
    "--memory", "memory_gb", type=int, default=None, metavar="GB", help="New memory size."
)
@click.option(
    "--disk",
    "disk_gb",
    type=int,
    default=None,
    metavar="GB",
    help="New disk size (grow-only; multipass cannot shrink).",
)
def vm_resize(cpus: int | None, memory_gb: int | None, disk_gb: int | None) -> None:
    """Reallocate CPUs / memory / disk on the existing VM.

    The VM stops, gets the new sizing applied via `multipass set`, then
    starts back up. Mounts are re-verified and the config is updated.
    Disk can only grow — destroy and re-init to shrink (data-dir survives).
    """
    _run(vm_mod.resize, cpus=cpus, memory_gb=memory_gb, disk_gb=disk_gb)


@vm.command("shell")
def vm_shell() -> None:
    """Drop into the VM via `multipass shell`."""
    _run(vm_mod.shell)


@vm.command("status")
def vm_status() -> None:
    """Show VM state, mounts, IPs."""
    _run(vm_mod.status)


@vm.command("snapshot")
@click.argument("name")
@click.option("--comment", default=None)
def vm_snapshot(name: str, comment: str | None) -> None:
    """Take a multipass snapshot."""
    _run(vm_mod.take_snapshot, name, comment=comment)


@vm.command("restore")
@click.argument("name")
@click.option("--destructive", is_flag=True, help="Skip multipass's safety check.")
def vm_restore(name: str, destructive: bool) -> None:
    """Restore a multipass snapshot by name."""
    _run(vm_mod.restore_snapshot, name, destructive=destructive)


@vm.group("snapshots")
def vm_snapshots_group() -> None:
    """Multipass snapshot management."""


@vm_snapshots_group.command("list")
def vm_snapshots_list() -> None:
    """List multipass snapshots for this VM."""
    _run(vm_mod.list_snapshots)


@vm.command("destroy")
@click.option("--export-first", is_flag=True, help="Run `agents export --all` before destroying.")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def vm_destroy(ctx: click.Context, export_first: bool, yes_local: bool) -> None:
    """Tear down the VM (host data-dir untouched).

    Prompts to run an export of every agent first unless ``--yes`` is set.
    Override the prompt with ``--export-first`` to always export, or
    skip with ``--yes`` to skip the offer.
    """
    yes = yes_local or ctx.obj.get("yes", False)

    def body() -> None:
        do_export = export_first
        if not do_export and not yes:
            do_export = ui.confirm(
                "export all agents to <data-dir>/backups/ before destroying the VM?",
                default=False,
            )
        if do_export:
            export_cmd.export_all()
        vm_mod.destroy(yes=yes)

    _run(body)


# ---------- agent CRUD ----------


@main.command("create")
@click.argument("name")
@click.option("--type", "agent_type", required=True, type=click.Choice(["hermes", "openclaw"]))
@click.option("--port", type=int, default=None, help="Host port to bind. Default: auto-allocate.")
@click.option("--image-tag", default=None, help="Override pinned tag.")
@click.option(
    "--setup", "run_setup", is_flag=True, help="Run the agent's setup wizard immediately."
)
@click.option("--reuse-data", is_flag=True, help="Attach to an existing data dir.")
def agents_create(
    name: str,
    agent_type: str,
    port: int | None,
    image_tag: str | None,
    run_setup: bool,
    reuse_data: bool,
) -> None:
    """Create a new agent."""
    _run(
        agents_cmd.create,
        name=name,
        agent_type=agent_type,
        port=port,
        image_tag=image_tag,
        run_setup=run_setup,
        reuse_data=reuse_data,
    )


@main.command("list")
def agents_list() -> None:
    """List configured agents."""
    _run(agents_cmd.list_agents)


@main.command("remove")
@click.argument("name")
@click.option(
    "--export-first",
    is_flag=True,
    help="Run `agents export <name>` before removing.",
)
@click.option("--keep-data", is_flag=True, help="Leave the agent's data dir on disk.")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def agents_remove(
    ctx: click.Context, name: str, export_first: bool, keep_data: bool, yes_local: bool
) -> None:
    """Remove an agent (and its data dir unless --keep-data).

    Pass ``--export-first`` to dump a tarball to ``<data-dir>/backups/``
    before tearing down — convenient if you want a final snapshot
    before deletion.
    """
    yes = yes_local or ctx.obj.get("yes", False)

    def body() -> None:
        if export_first:
            export_cmd.export(name)
        agents_cmd.remove(
            name,
            yes=yes,
            delete_data=not keep_data,
        )

    _run(body)


@main.command("rename")
@click.argument("old_name")
@click.argument("new_name")
def agents_rename(old_name: str, new_name: str) -> None:
    """Rename an agent (compose service + dir + container)."""
    _run(agents_cmd.rename, old_name, new_name)


# ---------- day-2 ops ----------


@main.command("start")
@click.argument("name")
def agent_start(name: str) -> None:
    """Start a single agent's container."""
    _run(ops.start, name)


@main.command("stop")
@click.argument("name")
def agent_stop(name: str) -> None:
    """Stop a single agent's container."""
    _run(ops.stop, name)


@main.command("restart")
@click.argument("name")
def agent_restart(name: str) -> None:
    """Restart a single agent's container."""
    _run(ops.restart, name)


@main.command("logs")
@click.argument("name")
@click.option("-f", "--follow", is_flag=True, help="Stream logs interactively.")
@click.option("--since", default=None, help="docker compose --since value, e.g. 10m.")
def agent_logs(name: str, follow: bool, since: str | None) -> None:
    """Tail an agent's logs."""
    _run(ops.logs, name, follow=follow, since=since)


@main.command(
    "exec",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("name")
@click.argument("cmd", nargs=-1, type=click.UNPROCESSED)
def agent_run_in(name: str, cmd: tuple[str, ...]) -> None:
    """Run a command inside the agent's container (TTY shell if no cmd)."""
    _run(ops.run_in_agent, name, cmd)


@main.command("setup")
@click.argument("name")
def agent_setup(name: str) -> None:
    """Run the agent's setup wizard (hermes setup / openclaw onboard)."""
    _run(ops.setup, name)


@main.command("doctor")
@click.argument("name", required=False)
def agent_doctor(name: str | None) -> None:
    """Health-check the VM, mount, and agents."""
    _run(ops.doctor, target=name)


# ---------- upgrades ----------


@main.command("pull")
@click.argument("name", required=False)
def agents_pull(name: str | None) -> None:
    """docker compose pull (one agent or all)."""
    _run(upgrades.pull, name)


@main.command("upgrade")
@click.argument("name", required=False)
@click.option("--all", "all_agents", is_flag=True, help="Upgrade every agent in compose.")
@click.option("--tag", default=None, help="Target image tag (single-agent upgrade only).")
@click.option("--no-snapshot", is_flag=True, help="Skip both snapshots.")
@click.option(
    "--canary",
    default=None,
    help="With --all: roll this agent first; halt on failure.",
)
def agents_upgrade(
    name: str | None,
    all_agents: bool,
    tag: str | None,
    no_snapshot: bool,
    canary: str | None,
) -> None:
    """Upgrade an agent (or every agent with ``--all``)."""
    if all_agents:
        if name:
            raise click.UsageError("--all is mutually exclusive with NAME")
        if tag:
            raise click.UsageError("--tag is single-agent only")
        _run(upgrades.upgrade_all, canary=canary)
        return
    if not name:
        raise click.UsageError("provide NAME or --all")
    if canary:
        raise click.UsageError("--canary is meaningful only with --all")
    _run(upgrades.upgrade, name, tag=tag, no_snapshot=no_snapshot)


@main.command("rollback")
@click.argument("name")
@click.option(
    "--snapshot",
    "snapshot_id",
    default=None,
    help="Pick a specific pre-upgrade archive name (default: latest).",
)
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def agents_rollback(
    ctx: click.Context, name: str, snapshot_id: str | None, yes_local: bool
) -> None:
    """Restore an agent from a pre-upgrade snapshot.

    Without ``--snapshot``, the latest archive under ``<data-dir>/.upgrades/``
    is used. Pass ``--snapshot <name>`` (with or without the ``.tar.gz``
    suffix) to pick a specific one.
    """
    _run(
        upgrades.rollback,
        name,
        snapshot_id=snapshot_id,
        yes=yes_local or ctx.obj.get("yes", False),
    )


# ---------- export / import ----------


@main.command("export")
@click.argument("name", required=False)
@click.option("--all", "all_agents", is_flag=True, help="Export every agent in compose.")
@click.option("--to", "to", type=click.Path(), default=None, help="Override the output dir.")
def agents_export(name: str | None, all_agents: bool, to: str | None) -> None:
    """Export an agent's data dir as a tarball (or all agents with ``--all``)."""
    to_path = Path(to) if to else None
    if all_agents:
        if name:
            raise click.UsageError("--all is mutually exclusive with NAME")
        _run(export_cmd.export_all, to=to_path)
        return
    if not name:
        raise click.UsageError("provide NAME or --all")
    _run(export_cmd.export, name, to=to_path)


@main.command("import")
@click.argument("name")
@click.argument("archive", type=click.Path(exists=True))
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def agents_import(ctx: click.Context, name: str, archive: str, yes_local: bool) -> None:
    """Restore an agent from an archive."""
    _run(
        export_cmd.import_archive,
        name,
        Path(archive),
        yes=yes_local or ctx.obj.get("yes", False),
    )


@main.group("export-cron")
def export_cron_group() -> None:
    """Host-cron integration for periodic exports."""


@export_cron_group.command("install")
@click.option(
    "--time",
    "time_",
    default="02:30",
    help=(
        "HH:MM (zero-padded), host local time. "
        "Pass a comma-separated list for multi-times-a-day backups, "
        "e.g. `--time 02:30,08:30,14:30,20:30`."
    ),
)
def export_cron_install(time_: str) -> None:
    """Install the host cron entry."""
    _run(export_cmd.export_cron_install, time=time_)


@export_cron_group.command("uninstall")
def export_cron_uninstall() -> None:
    """Remove the host cron entry."""
    _run(export_cmd.export_cron_uninstall)


@export_cron_group.command("status")
def export_cron_status() -> None:
    """Show whether the cron is installed and when it last ran."""
    _run(export_cmd.export_cron_status)


# ---------- codex helpers ----------


@main.group("codex")
def codex_group() -> None:
    """Codex CLI integration (toolkit-scoped auth)."""


@codex_group.command("login")
@click.option(
    "--device-auth",
    is_flag=True,
    help=(
        "Use the device-code flow (Codex prints a URL + code you enter "
        "on any browser-equipped machine) instead of the default "
        "browser+localhost-callback. Required on headless / remote hosts."
    ),
)
def codex_login(device_auth: bool) -> None:
    """Interactively authenticate the toolkit-scoped Codex account."""
    _run(codex_mod.login, device_auth=device_auth)


@codex_group.command("refresh")
def codex_refresh() -> None:
    """Silently refresh the token; fails if interactive re-auth is needed."""
    _run(codex_mod.refresh)


@codex_group.command("status")
def codex_status() -> None:
    """Show auth state, token expiry, and per-Hermes-agent skill state."""
    _run(codex_mod.status)


@codex_group.command("skill-enable")
@click.argument("name")
def codex_skill_enable(name: str) -> None:
    """Enable the Codex coding skill for a Hermes agent (build sandbox image)."""
    _run(codex_mod.skill_enable, name)


@codex_group.command("skill-disable")
@click.argument("name")
def codex_skill_disable(name: str) -> None:
    """Disable the Codex skill — restore the original sandbox image."""
    _run(codex_mod.skill_disable, name)


# ---------- config (read-only convenience) ----------


@main.command("config")
def show_config() -> None:
    """Print the current config (or relevant errors)."""

    def body() -> None:
        cfg = cfg_mod.load()
        ui.emit(cfg.model_dump(mode="json"))

    _run(body)


if __name__ == "__main__":
    main()
