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
def vm_init(
    data_dir: str,
    data_dir_in_vm: bool,
    cpus: int | None,
    memory_gb: int | None,
    disk_gb: int | None,
) -> None:
    """Provision the Multipass VM and mount the host data dir."""
    _run(
        vm_mod.init,
        data_dir=Path(data_dir).expanduser(),
        data_dir_in_vm=data_dir_in_vm,
        cpus=cpus,
        memory_gb=memory_gb,
        disk_gb=disk_gb,
    )


@vm.command("start")
def vm_start() -> None:
    """Start the VM and re-verify the data-dir mount."""
    _run(vm_mod.start)


@vm.command("stop")
def vm_stop() -> None:
    """Stop the VM."""
    _run(vm_mod.stop)


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


@vm.command("snapshots")
def vm_snapshots() -> None:
    """List multipass snapshots for this VM."""
    _run(vm_mod.list_snapshots)


@vm.command("destroy")
@click.pass_context
def vm_destroy(ctx: click.Context) -> None:
    """Tear down the VM (host data-dir untouched)."""
    _run(vm_mod.destroy, yes=ctx.obj.get("yes", False))


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
@click.option("--keep-data", is_flag=True, help="Leave the agent's data dir on disk.")
@click.pass_context
def agents_remove(ctx: click.Context, name: str, keep_data: bool) -> None:
    """Remove an agent (and its data dir unless --keep-data)."""
    _run(
        agents_cmd.remove,
        name,
        yes=ctx.obj.get("yes", False),
        delete_data=not keep_data,
    )


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
@click.argument("name")
@click.option("--tag", default=None, help="Target image tag.")
@click.option("--no-snapshot", is_flag=True, help="Skip both snapshots.")
def agents_upgrade(name: str, tag: str | None, no_snapshot: bool) -> None:
    """Upgrade one agent with auto-rollback on failure."""
    _run(upgrades.upgrade, name, tag=tag, no_snapshot=no_snapshot)


@main.command("upgrade-all")
@click.option("--canary", default=None, help="Roll this agent first; halt on failure.")
def agents_upgrade_all(canary: str | None) -> None:
    """Upgrade every agent, optional canary."""
    _run(upgrades.upgrade_all, canary=canary)


@main.command("rollback")
@click.argument("name")
@click.pass_context
def agents_rollback(ctx: click.Context, name: str) -> None:
    """Restore an agent from its latest pre-upgrade snapshot."""
    _run(upgrades.rollback, name, yes=ctx.obj.get("yes", False))


# ---------- export / import ----------


@main.command("export")
@click.argument("name")
@click.option("--to", "to", type=click.Path(), default=None, help="Override the output dir.")
def agents_export(name: str, to: str | None) -> None:
    """Export an agent's data dir as a tarball."""
    _run(export_cmd.export, name, to=Path(to) if to else None)


@main.command("export-all")
@click.option("--to", "to", type=click.Path(), default=None)
def agents_export_all(to: str | None) -> None:
    """Export every agent in compose."""
    _run(export_cmd.export_all, to=Path(to) if to else None)


@main.command("import")
@click.argument("name")
@click.argument("archive", type=click.Path(exists=True))
@click.pass_context
def agents_import(ctx: click.Context, name: str, archive: str) -> None:
    """Restore an agent from an archive."""
    _run(
        export_cmd.import_archive,
        name,
        Path(archive),
        yes=ctx.obj.get("yes", False),
    )


@main.group("export-cron")
def export_cron_group() -> None:
    """Host-cron integration for periodic exports."""


@export_cron_group.command("install")
@click.option("--time", "time_", default="02:30", help="HH:MM (zero-padded), host local time.")
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
def codex_login() -> None:
    """Interactively authenticate the toolkit-scoped Codex account."""
    _run(codex_mod.login)


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
