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
    conversations_cmd,
    export_cmd,
    firecrawl_cmd,
    fixperms_cmd,
    ollama_cmd,
    ops,
    secrets_cmd,
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
    memory as memory_mod,
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
    Disk can only grow — use `agents vm rebuild --disk N` to shrink.
    """
    _run(vm_mod.resize, cpus=cpus, memory_gb=memory_gb, disk_gb=disk_gb)


@vm.command("rebuild")
@click.option("--cpus", type=int, default=None, help="New CPU count (default: keep current).")
@click.option(
    "--memory",
    "memory_gb",
    type=int,
    default=None,
    metavar="GB",
    help="New memory size (default: keep current).",
)
@click.option(
    "--disk",
    "disk_gb",
    type=int,
    default=None,
    metavar="GB",
    help="New disk size — can shrink or grow (default: keep current).",
)
@click.option(
    "--ubuntu-release",
    default=None,
    help="New Ubuntu release for the VM (default: keep current).",
)
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip confirmation.")
@click.pass_context
def vm_rebuild(
    ctx: click.Context,
    cpus: int | None,
    memory_gb: int | None,
    disk_gb: int | None,
    ubuntu_release: str | None,
    yes_local: bool,
) -> None:
    """Non-destructively re-create the VM.

    Phased + journaled: exports all agents → destroys VM → re-inits with
    the new sizing → imports each agent back → starts services. A crashed
    rebuild can be resumed by re-running the command — completed phases
    are skipped via the on-disk journal.

    The case this exists for: disk SHRINK (which `vm resize` can't do),
    bumping the Ubuntu release, or fixing a wedged VM without losing
    agent state.
    """
    yes = bool(ctx.obj.get("yes")) or yes_local
    _run(
        vm_mod.rebuild,
        cpus=cpus,
        memory_gb=memory_gb,
        disk_gb=disk_gb,
        ubuntu_release=ubuntu_release,
        yes=yes,
    )


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


@main.command("reseat")
@click.argument("name")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip confirmation.")
@click.pass_context
def agents_reseat(ctx: click.Context, name: str, yes_local: bool) -> None:
    """Regenerate the agent's compose service block from the current fragment.

    Use this when the toolkit's service-fragment template has evolved since
    the agent was created — new env var, changed volume layout, etc. Reseat
    rewrites the compose entry to match what `agents create` would produce
    today, preserving the port, image tag, env_file additions (per-agent
    secrets), and depends_on additions (sidecars). Force-recreates the
    container; for Hermes, also runs the fix-perms chown/chmod dance.
    """
    yes = yes_local or ctx.obj.get("yes", False)
    _run(agents_cmd.reseat, name, yes=yes)


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
@click.option(
    "--root",
    "as_root",
    is_flag=True,
    help=(
        "Run as the container's image USER (root), not the runtime "
        "user (uid 1000). Reach for this only for admin operations "
        "(chown, apt install). Files created during a root exec end "
        "up unreadable by the long-running gateway process, which "
        "drops to uid 1000 — using --root by default silently breaks "
        "things like `hermes pairing approve`."
    ),
)
@click.argument("cmd", nargs=-1, type=click.UNPROCESSED)
def agent_run_in(name: str, as_root: bool, cmd: tuple[str, ...]) -> None:
    """Run a command inside the agent's container (TTY shell if no cmd).

    Defaults to running as the runtime user (uid 1000) so files share
    ownership with the gateway. Pass --root for admin operations.
    """
    _run(ops.run_in_agent, name, cmd, as_root=as_root)


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


@main.command("conversations")
@click.argument("name")
@click.option(
    "--limit",
    type=int,
    default=20,
    help="Max active sessions to render in the human table (JSON is unbounded).",
)
def agent_conversations(name: str, limit: int) -> None:
    """Show active/open/total conversation counts for a Hermes agent.

    Reads ``/opt/data/state.db`` inside the container and reports how
    many sessions are within the gateway's auto-continue freshness
    window — i.e. still "hot" in the gateway's context cache.
    Per-platform breakdown plus a top-N active list. Read-only.
    """
    _run(conversations_cmd.status, name, limit=limit)


@main.command("fix-perms")
@click.argument("name")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip confirmation.")
@click.pass_context
def agent_fix_perms(ctx: click.Context, name: str, yes_local: bool) -> None:
    """Re-create the container and align ``/opt/data`` ownership.

    Fixes the recurring "files at uid 10000, agents exec at uid 1000 hits
    EACCES on /opt/data" class of bug. Stops the container, force-removes
    it so compose can't decide "no drift, skip entrypoint", brings it back
    up so the entrypoint runs fresh, then chowns and chmod's /opt/data to
    line up with whatever uid hermes ends up at inside the container.
    """
    yes = yes_local or ctx.obj.get("yes", False)
    _run(fixperms_cmd.fix_perms, name, yes=yes)


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


# ---------- ollama ----------


@main.group("ollama")
def ollama_group() -> None:
    """Configure an external Ollama / OpenAI-compatible endpoint.

    The toolkit does NOT run Ollama for you — point it at one you
    already run (host, LAN, Tailscale, or hosted vendor). Memory
    plugins read this configuration when they need embeddings or LLM
    fact-extraction.
    """


@ollama_group.command("configure")
@click.option(
    "--url",
    required=True,
    help=(
        "OpenAI-compatible endpoint URL. Must be reachable from inside "
        "the VM; `localhost` is refused because it would resolve to "
        "the VM itself."
    ),
)
@click.option(
    "--api-key",
    default=None,
    help=(
        "Bearer token for the endpoint (vanilla Ollama needs none; pass "
        "for nginx-protected Ollama or hosted vendors). Stored in "
        "<data-dir>/.env, never in config.toml. Pass `--api-key ''` to "
        "clear a previously-stored token."
    ),
)
@click.option(
    "--api-key-env",
    default=None,
    help="Name of the env var to store the token under (default: OLLAMA_API_KEY).",
)
@click.option(
    "--embed-model", default=None, help="Embeddings model name (default: nomic-embed-text)."
)
@click.option("--llm-model", default=None, help="LLM model name (default: qwen2.5:7b-instruct).")
def ollama_configure(
    url: str,
    api_key: str | None,
    api_key_env: str | None,
    embed_model: str | None,
    llm_model: str | None,
) -> None:
    """Persist the endpoint and probe reachability from VM + containers."""
    _run(
        ollama_cmd.configure,
        url=url,
        api_key=api_key,
        api_key_env=api_key_env,
        embed_model=embed_model,
        llm_model=llm_model,
    )


@ollama_group.command("status")
def ollama_status() -> None:
    """Show the configured URL + last probe results."""
    _run(ollama_cmd.status)


@ollama_group.command("clear")
def ollama_clear() -> None:
    """Drop the Ollama configuration and any stored API key."""
    _run(ollama_cmd.clear)


# ---------- memory ----------


@main.group("memory")
def memory_group() -> None:
    """Manage memory backends for Hermes agents."""


@memory_group.command("list")
def memory_list() -> None:
    """List memory plugins the toolkit knows about."""

    def body() -> None:
        rows = memory_mod.list_plugins()
        ui.table(rows, ["name", "tier", "summary"])

    _run(body)


@memory_group.command("enable")
@click.argument("plugin")
@click.argument("agent")
@click.option(
    "--embedder",
    type=click.Choice(["auto", "ollama", "openai"]),
    default="auto",
    help="Embeddings provider (mem0 only). 'auto' picks Ollama if configured, else OpenAI.",
)
@click.option(
    "--llm",
    type=click.Choice(["auto", "ollama", "openai"]),
    default="auto",
    help="LLM provider (mem0 only). 'auto' picks Ollama if configured, else OpenAI.",
)
@click.option(
    "--image-tag",
    default=None,
    help="Override the pinned image tag for sibling-service backends (openviking).",
)
def memory_enable(plugin: str, agent: str, embedder: str, llm: str, image_tag: str | None) -> None:
    """Enable a memory backend for an agent."""
    opts: dict[str, Any] = {"embedder": embedder, "llm": llm}
    if image_tag is not None:
        opts["image_tag"] = image_tag
    _run(memory_mod.enable, plugin, agent, **opts)


@memory_group.command("disable")
@click.argument("agent")
def memory_disable(agent: str) -> None:
    """Disable the currently-enabled memory backend for an agent."""
    _run(memory_mod.disable, agent)


@memory_group.command("status")
@click.argument("agent", required=False)
def memory_status(agent: str | None) -> None:
    """Show memory state per agent (or for a single agent if NAME is given)."""
    _run(memory_mod.status, agent)


# ---------- firecrawl ----------


@main.group("firecrawl")
def firecrawl_group() -> None:
    """Self-hosted Firecrawl backend (shared across all Hermes agents).

    One stack per VM: api + playwright + redis + rabbitmq + postgres.
    Auto-wires every Hermes agent's terminal.env_passthrough on `up`
    so the FIRECRAWL_API_KEY / FIRECRAWL_API_URL pair reaches both the
    Hermes Python tools AND the LLM's shell-based diagnostics.
    """


@firecrawl_group.command("up")
@click.option(
    "--upgrade",
    is_flag=True,
    help="Re-resolve upstream floating tags into fresh digests (refreshes images).",
)
def firecrawl_up(upgrade: bool) -> None:
    """Install (or upgrade) the firecrawl stack and wire all Hermes agents."""
    _run(firecrawl_cmd.up, upgrade=upgrade)


@firecrawl_group.command("down")
def firecrawl_down() -> None:
    """Stop the firecrawl stack; postgres data preserved on disk."""
    _run(firecrawl_cmd.down)


@firecrawl_group.command("destroy")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip confirmation.")
@click.pass_context
def firecrawl_destroy(ctx: click.Context, yes_local: bool) -> None:
    """Stop and remove the stack, drop compose entries, wipe postgres data."""
    yes = yes_local or ctx.obj.get("yes", False)
    _run(firecrawl_cmd.destroy, yes=yes)


@firecrawl_group.command("status")
def firecrawl_status() -> None:
    """Show whether the stack is running and which agents are wired."""
    _run(firecrawl_cmd.status)


@firecrawl_group.command("logs")
@click.argument("service", required=False)
@click.option("-f", "--follow", is_flag=True, help="Stream logs.")
@click.option("--since", default=None, help="docker compose --since value (e.g. 10m).")
def firecrawl_logs(service: str | None, follow: bool, since: str | None) -> None:
    """Tail logs from a firecrawl service (default: api)."""
    _run(firecrawl_cmd.logs, service=service, follow=follow, since=since)


# ---------- secrets ----------


@main.group("secret")
def secret_group() -> None:
    """Per-agent secrets (API keys etc.) wired into the container env.

    Each agent has its own ``<data-dir>/<name>/.env`` whose KEY=VALUE
    pairs become process environment inside the container — exactly
    what skills like ``firecrawl`` read at runtime. Mutations restart
    the container so the new env is visible.
    """


@secret_group.command("set")
@click.argument("agent")
@click.argument("key")
@click.argument("value", required=False)
def secret_set(agent: str, key: str, value: str | None) -> None:
    """Set a secret. Value is prompted hidden when omitted.

    Examples:

        agents secret set coder FIRECRAWL_API_KEY      # hidden prompt
        agents secret set coder FIRECRAWL_API_KEY fc-x # value as arg
    """
    if value is None:
        value = click.prompt(f"value for {key}", hide_input=True, confirmation_prompt=False)
    _run(secrets_cmd.set_secret, agent, key, value)


@secret_group.command("unset")
@click.argument("agent")
@click.argument("key")
def secret_unset(agent: str, key: str) -> None:
    """Remove a secret from the per-agent .env."""
    _run(secrets_cmd.unset_secret, agent, key)


@secret_group.command("list")
@click.argument("agent")
def secret_list(agent: str) -> None:
    """List secret names for an agent (values are NEVER printed)."""
    _run(secrets_cmd.list_secrets, agent)


@secret_group.command("clear")
@click.argument("agent")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip confirmation.")
@click.pass_context
def secret_clear(ctx: click.Context, agent: str, yes_local: bool) -> None:
    """Wipe ALL secrets for an agent."""
    yes = yes_local or ctx.obj.get("yes", False)
    _run(secrets_cmd.clear_secrets, agent, yes=yes)


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
