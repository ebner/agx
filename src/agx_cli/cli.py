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

from agx_cli import (
    __version__,
    access_cmd,
    agents_cmd,
    conversations_cmd,
    edit_cmd,
    export_cmd,
    firecrawl_cmd,
    fixperms_cmd,
    inference_cmd,
    migrate_cmd,
    models_cmd,
    mounts_cmd,
    ollama_cmd,
    ops,
    outdated_cmd,
    profile_cmd,
    prune_cmd,
    secrets_cmd,
    tailscale_cmd,
    ui,
    upgrades,
)
from agx_cli import (
    codex as codex_mod,
)
from agx_cli import (
    config as cfg_mod,
)
from agx_cli import (
    memory as memory_mod,
)
from agx_cli import (
    vm as vm_mod,
)
from agx_cli.errors import UserError


def _run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Invoke a backend function and translate any exception into an exit code."""
    try:
        fn(*args, **kwargs)
    except SystemExit:
        raise
    except BaseException as e:
        sys.exit(ui.handle_cli_errors(e))


class _RootGroup(click.Group):
    """Root group that runs click in non-standalone mode.

    Why: click's standalone mode hardwires ``UsageError`` (bad flags,
    missing arguments — both its own parse errors and the explicit
    ``raise click.UsageError`` calls in command bodies below) to exit
    code 2, but this toolkit's contract (errors.py) defines user error
    = 1 and reserves 2 for runtime failures. Forcing
    ``standalone_mode=False`` makes click *raise* those exceptions out
    of ``super().main()`` instead of printing + ``sys.exit(2)``, so we
    can route them through :func:`ui.handle_cli_errors` — the single
    place the exit-code mapping lives.

    Preserved behaviors:

    - ``--help`` / ``--version`` raise ``click.exceptions.Exit(0)``,
      which non-standalone ``main`` returns as ``0`` — the console-
      script wrapper (``sys.exit(main())``) and CliRunner both read
      that as success.
    - Successful commands return ``None`` (every callback funnels
      through :func:`_run`) → exit 0.
    - ``KeyboardInterrupt``/``EOFError`` during parse or prompts are
      converted to ``Abort`` by click before leaving ``main`` — mapped
      back to the interrupt exit code (130) here. Interrupts inside
      command bodies already exit 130 via ``_run``.
    - ``CliError`` subclasses raised in command bodies never reach
      this handler: ``_run`` translates them to ``sys.exit(code)``,
      and ``SystemExit`` passes straight through non-standalone click.
    """

    def main(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["standalone_mode"] = False
        # Capture the raw argv up front: root-level parse errors can fire
        # before click records any root flag, and the fallback flag scan
        # below needs the original tokens.
        raw_args = args[0] if args else kwargs.get("args")
        argv = list(raw_args) if raw_args is not None else sys.argv[1:]
        try:
            return super().main(*args, **kwargs)
        except click.ClickException as e:
            # By the time the exception reaches here the context stack
            # has been unwound, so ui.is_json() would report False even
            # for a --json run. Click attaches the failing context to
            # its parse errors (UsageError.ctx) — re-push it so the
            # error object honors the root flags. When the failure
            # precedes the root callback (`--json --bogus`, `--json
            # bogus`, bare `--json`), ctx.obj was never populated;
            # rebuild it from what click parsed (or the argv itself) so
            # --json is still honored. Body-raised UsageErrors without
            # a ctx degrade to the human form.
            ectx = e.ctx if isinstance(e, click.UsageError) else None
            if ectx is not None:
                if ectx.obj is None:
                    ectx.obj = _root_flags_for_error(ectx, argv)
                with ectx:
                    sys.exit(ui.handle_cli_errors(e))
            sys.exit(ui.handle_cli_errors(e))
        except click.exceptions.Abort as e:
            # Ctrl-C/EOF during click-level parsing or prompts (click
            # converts both to Abort before they leave main). Prompt
            # aborts inside command bodies take the same mapping via
            # _run -> handle_cli_errors.
            sys.exit(ui.handle_cli_errors(e))


def _root_flags_for_error(ectx: click.Context, argv: list[str]) -> dict[str, bool]:
    """Reconstruct the root ``--json``/``--yes``/``--verbose`` flags for
    an error path where the root callback never ran, so the error emit
    can still honor ``--json``.

    Preference order: the root context's already-parsed params — click
    populates ``ctx.params`` even when the callback hasn't run (e.g.
    bare ``agx --json`` failing on "Missing command.") — then a
    conservative literal scan of the argv tokens *before* the first
    non-option token (root options only), for parses that abort before
    any param lands (e.g. ``agx --json --bogus``).
    """
    root = ectx
    while root.parent is not None:
        root = root.parent
    if root.params:
        return {
            "json": bool(root.params.get("json_output")),
            "yes": bool(root.params.get("assume_yes")),
            "verbose": bool(root.params.get("verbose")),
        }
    flags = {"json": False, "yes": False, "verbose": False}
    for token in argv:
        if not token.startswith("-"):
            break  # root options end at the subcommand name
        if token == "--json":
            flags["json"] = True
        elif token == "--yes":
            flags["yes"] = True
        elif token in ("-v", "--verbose"):
            flags["verbose"] = True
    return flags


@click.group(cls=_RootGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON output.")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip confirmation prompts.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
@click.pass_context
def main(ctx: click.Context, json_output: bool, assume_yes: bool, verbose: bool) -> None:
    """agx: manage AI agents (Hermes, OpenClaw) in a VM or on bare metal."""
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_output
    ctx.obj["yes"] = assume_yes
    ctx.obj["verbose"] = verbose

    # One-time move of the pre-rename config dir (~/.config/agents).
    if cfg_mod.migrate_legacy_config_dir():
        ui.warn(
            f"moved config {cfg_mod.LEGACY_CONFIG_PATH.parent} -> "
            f"{cfg_mod.DEFAULT_CONFIG_PATH.parent} (CLI renamed `agents` -> `agx`)"
        )

    # Wire up the VM-backend dispatcher from config (if a config exists).
    # `vm init` runs before any config exists — it sets the backend via
    # its own --backend flag before invoking any backend primitive.
    try:
        cfg = cfg_mod.load()
    except Exception:
        # No config yet (e.g. first-time `vm init`). Dispatcher defaults
        # to multipass; `vm init` will override based on --backend flag.
        return
    from agx_cli import mp
    from agx_cli.backends import get_backend_module

    backend_mod = get_backend_module(cfg.vm.backend)
    mp.set_backend(backend_mod)
    # libvirt needs the data dir for image / ssh-key caching.
    if cfg.vm.backend == "libvirt":
        from agx_cli.backends import libvirt as libvirt_mod

        libvirt_mod.configure(cfg.paths.host_data_dir)
    # bare_metal pins DOCKER_HOST to the recorded rootless socket.
    elif cfg.vm.backend == "bare_metal":
        from agx_cli.backends import bare_metal as bare_metal_mod

        bare_metal_mod.configure(cfg.vm_runtime.docker_socket_path)


# ---------- vm lifecycle ----------


@main.group()
def vm() -> None:
    """Multipass VM lifecycle."""


@vm.command("init")
@click.option(
    "--data-dir",
    required=False,
    default=None,
    type=click.Path(),
    help=(
        "Host directory where agent state lives. Required for multipass/libvirt; "
        "defaults to ~/.local/share/agents/ for bare_metal."
    ),
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
@click.option(
    "--backend",
    type=click.Choice(["multipass", "libvirt", "bare_metal"]),
    default="multipass",
    help=(
        "VM hypervisor backend. libvirt is Linux-only + experimental; enables GPU passthrough. "
        "bare_metal runs containers directly on the host (Linux-only, opt-in trust trade-off)."
    ),
)
@click.option(
    "--gpu",
    "gpu",
    multiple=True,
    metavar="PCI",
    help="GPU PCI passthrough (libvirt only). 'auto' for autodetect, or DDDD:BB:SS.F. Repeatable.",
)
def vm_init(
    data_dir: str | None,
    data_dir_in_vm: bool,
    cpus: int | None,
    memory_gb: int | None,
    disk_gb: int | None,
    ubuntu_release: str | None,
    backend: str,
    gpu: tuple[str, ...],
) -> None:
    """Provision the VM (Multipass or libvirt) and mount the host data dir.

    With ``--backend bare_metal`` no VM is provisioned: the toolkit
    verifies that rootless Docker is installed on the host and treats the
    host as the runtime. The VM trust boundary is forfeited; only use
    bare_metal on single-user dev workstations.
    """
    if gpu and backend != "libvirt":
        raise click.UsageError("--gpu requires --backend libvirt")
    if backend == "bare_metal":
        if data_dir_in_vm:
            raise click.UsageError("--data-dir-in-vm is meaningless with --backend bare_metal")
        if data_dir is None:
            data_dir = str(Path.home() / ".local" / "share" / "agents")
    else:
        if data_dir is None:
            raise click.UsageError(
                "--data-dir is required for multipass/libvirt backends "
                "(use --backend bare_metal to omit it)"
            )
    _run(
        vm_mod.init,
        data_dir=Path(data_dir).expanduser(),
        data_dir_in_vm=data_dir_in_vm,
        cpus=cpus,
        memory_gb=memory_gb,
        disk_gb=disk_gb,
        ubuntu_release=ubuntu_release,
        backend=backend,
        gpu=list(gpu),
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
    Disk can only grow — use `agx vm rebuild --disk N` to shrink.
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


@vm.command("migrate-data")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip the downtime confirmation.")
@click.pass_context
def vm_migrate_data(ctx: click.Context, yes_local: bool) -> None:
    """Move agent state from the host mount onto the VM disk, in place.

    Converts an existing host-mount deployment to ``--data-dir-in-vm``:
    per-agent safety exports to ``<data-dir>/backups/``, stop all
    services, VM-side ``cp -a`` of the whole data dir onto the VM disk,
    detach the host mount, flip the config, restart, and require a green
    doctor. Phased + journaled — a crashed run resumes by re-running.

    The host data dir is NEVER modified: it stays in place until you
    manually archive/delete it after verifying the migration (the final
    summary lists exactly which paths qualify).
    """
    yes = yes_local or ctx.obj.get("yes", False)
    _run(migrate_cmd.migrate_data, yes=yes)


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
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip the downtime confirmation.")
@click.pass_context
def vm_snapshot(ctx: click.Context, name: str, comment: str | None, yes_local: bool) -> None:
    """Take a VM snapshot.

    On libvirt with an active virtiofs mount, prompts to confirm a
    brief VM stop (~30s) because the vhost-user backend can't be
    snapshotted while running. ``-y`` / global ``--yes`` skip the
    prompt.
    """
    yes = bool(ctx.obj.get("yes")) or yes_local
    _run(vm_mod.take_snapshot, name, comment=comment, yes=yes)


@vm.command("restore")
@click.argument("name")
@click.option("--destructive", is_flag=True, help="Skip multipass's safety check.")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip the downtime confirmation.")
@click.pass_context
def vm_restore(ctx: click.Context, name: str, destructive: bool, yes_local: bool) -> None:
    """Restore a snapshot by name.

    On libvirt with an active virtiofs mount, prompts to confirm a
    brief VM stop. ``-y`` / global ``--yes`` skip the prompt.
    """
    yes = bool(ctx.obj.get("yes")) or yes_local
    _run(vm_mod.restore_snapshot, name, destructive=destructive, yes=yes)


@vm.group("snapshots")
def vm_snapshots_group() -> None:
    """Multipass snapshot management."""


@vm_snapshots_group.command("list")
def vm_snapshots_list() -> None:
    """List multipass snapshots for this VM."""
    _run(vm_mod.list_snapshots)


@vm.command("destroy")
@click.option("--export-first", is_flag=True, help="Run `agx export --all` before destroying.")
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
            # emit=False: destroy owns this command's single stdout
            # object; export_all narrates per-agent via ui.progress.
            export_cmd.export_all(emit=False)
        vm_mod.destroy(yes=yes)

    _run(body)


# ---------- agent CRUD ----------


@main.command("create")
@click.argument("name")
@click.option("--type", "agent_type", required=True, type=click.Choice(["hermes", "openclaw"]))
@click.option("--port", type=int, default=None, help="Host port to bind. Default: auto-allocate.")
@click.option("--image-tag", default=None, help="Use this image tag (skips the latest lookup).")
@click.option(
    "--setup", "run_setup", is_flag=True, help="Run the agent's setup wizard immediately."
)
@click.option("--reuse-data", is_flag=True, help="Attach to an existing data dir.")
@click.option(
    "--pinned",
    is_flag=True,
    help="Use the default tag from config.toml instead of resolving the latest upstream release.",
)
def agents_create(
    name: str,
    agent_type: str,
    port: int | None,
    image_tag: str | None,
    run_setup: bool,
    reuse_data: bool,
    pinned: bool,
) -> None:
    """Create a new agent on the latest upstream release (see --pinned / --image-tag)."""
    _run(
        agents_cmd.create,
        name=name,
        agent_type=agent_type,
        port=port,
        image_tag=image_tag,
        run_setup=run_setup,
        reuse_data=reuse_data,
        pinned=pinned,
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
    help="Run `agx export <name>` before removing.",
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
        effective_yes = yes
        if export_first and not effective_yes:
            # Consent must precede the export: otherwise a declined
            # removal still leaves a fresh tarball (and an extra JSON
            # object) behind. Fail fast on unknown agents so we don't
            # prompt for something that can't be removed anyway.
            cfg = cfg_mod.load()
            if agents_cmd._detect_type_from_compose(cfg, name) is None:
                raise UserError(f"agent {name!r} not found in compose")
            if not ui.confirm(
                agents_cmd.removal_confirm_message(cfg, name, delete_data=not keep_data),
                default=False,
            ):
                raise UserError("aborted by user")
            effective_yes = True
        if export_first:
            # emit=False: remove's own emit is this command's single
            # stdout object; surface the archive path as narration.
            archive = export_cmd.export(name, emit=False)
            ui.progress(f"exported {name!r} -> {archive}")
        agents_cmd.remove(
            name,
            yes=effective_yes,
            delete_data=not keep_data,
        )

    _run(body)


@main.command("rename")
@click.argument("old_name")
@click.argument("new_name")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip the downtime confirmation.")
@click.pass_context
def agents_rename(ctx: click.Context, old_name: str, new_name: str, yes_local: bool) -> None:
    """Rename an agent (compose service + dir + container).

    Force-recreates the running container under the new name — brief
    downtime. Prompts before proceeding unless ``-y`` / global ``--yes``.
    """
    yes = yes_local or ctx.obj.get("yes", False)
    _run(agents_cmd.rename, old_name, new_name, yes=yes)


@main.command("reseat")
@click.argument("name")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip confirmation.")
@click.pass_context
def agents_reseat(ctx: click.Context, name: str, yes_local: bool) -> None:
    """Regenerate the agent's compose service block from the current fragment.

    Use this when the toolkit's service-fragment template has evolved since
    the agent was created — new env var, changed volume layout, etc. Reseat
    rewrites the compose entry to match what `agx create` would produce
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


@main.group("conversations")
def conversations_group() -> None:
    """Inspect a Hermes agent's conversation store (status + export)."""


@conversations_group.command("status")
@click.argument("name")
@click.option(
    "--limit",
    type=int,
    default=20,
    help="Max active sessions to render in the human table (JSON is unbounded).",
)
@click.option(
    "--profile",
    default=None,
    help="Target a named Hermes profile (its own HERMES_HOME under profiles/<name>/).",
)
def conversations_status(name: str, limit: int, profile: str | None) -> None:
    """Show active/open/total conversation counts for a Hermes agent.

    Reads ``/opt/data/state.db`` inside the container and reports how
    many sessions are within the gateway's auto-continue freshness
    window — i.e. still "hot" in the gateway's context cache.
    Per-platform breakdown plus a top-N active list. Read-only.
    """
    _run(conversations_cmd.status, name, limit=limit, profile=profile)


@conversations_group.command("export")
@click.argument("name")
@click.option(
    "--to",
    "to_dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Output directory; one Markdown file per session lands here.",
)
@click.option(
    "--profile",
    default=None,
    help="Target a named Hermes profile (its own HERMES_HOME under profiles/<name>/).",
)
def conversations_export(name: str, to_dir: Path, profile: str | None) -> None:
    """Export every session as a Markdown transcript.

    One ``<session_id>.md`` per session, with metadata header + per-
    message sections. Idempotent: existing files overwritten. Used
    for migration (Hermes→OpenClaw) and long-term archival. See
    ``docs/migration.md`` for the runbook.
    """
    _run(conversations_cmd.export, name, to_dir=to_dir, profile=profile)


@main.command("fix-perms")
@click.argument("name")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip confirmation.")
@click.pass_context
def agent_fix_perms(ctx: click.Context, name: str, yes_local: bool) -> None:
    """Re-create the container and align ``/opt/data`` ownership.

    Fixes the recurring "files at uid 10000, agx exec at uid 1000 hits
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


@main.command("outdated")
@click.argument("name", required=False)
@click.option(
    "--include-prerelease",
    is_flag=True,
    help="Include beta/rc/alpha tags when fetching the latest upstream tag.",
)
def agents_outdated(name: str | None, include_prerelease: bool) -> None:
    """Report which agents are behind upstream.

    Compose pinning means agents stay frozen at their captured tag
    until you run ``agx upgrade <name>``. This command fetches the
    latest upstream tag from GitHub Releases for each agent type and
    classifies each agent as current / outdated / unknown.
    """
    _run(outdated_cmd.check, name, include_prerelease=include_prerelease)


@main.command("upgrade")
@click.argument("name", required=False)
@click.option("--all", "all_agents", is_flag=True, help="Upgrade every agent in compose.")
@click.option(
    "--tag",
    default=None,
    help=(
        "Pin to a specific image tag (single-agent only). Skips the upstream "
        "check that bare ``upgrade`` does."
    ),
)
@click.option(
    "--latest",
    "to_latest",
    is_flag=True,
    help=(
        "Resolve and pin to the latest upstream tag (single-agent only). "
        "Equivalent to bare ``upgrade`` — kept for explicit documentation."
    ),
)
@click.option("--no-snapshot", is_flag=True, help="Skip both snapshots.")
@click.option(
    "--recreate",
    is_flag=True,
    help=(
        "Force-refresh in place even when the target tag matches the current "
        "pin. Useful for picking up env-file changes or recovering from a "
        "wedged container. Without this, ``upgrade`` no-ops when there's no "
        "version change."
    ),
)
@click.option(
    "--canary",
    default=None,
    help="With --all: roll this agent first; halt on failure.",
)
def agents_upgrade(
    name: str | None,
    all_agents: bool,
    tag: str | None,
    to_latest: bool,
    no_snapshot: bool,
    recreate: bool,
    canary: str | None,
) -> None:
    """Upgrade an agent (or every agent with ``--all``).

    Bare ``agx upgrade <name>`` fetches the latest upstream tag and
    upgrades if newer — mirrors the convention from ``apt upgrade`` /
    ``brew upgrade``. No-ops with an informational message if the agent
    is already at the latest. Network required (GitHub releases API).

    Pass ``--tag X`` to pin to a specific tag (skips the upstream
    check). Pass ``--recreate`` to force a snapshot+recreate+doctor
    cycle even when there's no version change — useful for picking up
    env-file edits or recovering from a wedged container.
    """
    if all_agents:
        if name:
            raise click.UsageError("--all is mutually exclusive with NAME")
        if tag:
            raise click.UsageError("--tag is single-agent only")
        if to_latest:
            raise click.UsageError("--latest is single-agent only")
        if recreate:
            raise click.UsageError("--recreate is single-agent only")
        _run(upgrades.upgrade_all, canary=canary)
        return
    if not name:
        raise click.UsageError("provide NAME or --all")
    if canary:
        raise click.UsageError("--canary is meaningful only with --all")
    if tag and to_latest:
        raise click.UsageError("--tag and --latest are mutually exclusive")
    _run(
        upgrades.upgrade,
        name,
        tag=tag,
        to_latest=to_latest,
        no_snapshot=no_snapshot,
        recreate=recreate,
    )


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


@main.command("prune")
@click.argument("name", required=False)
@click.option(
    "--keep",
    type=click.IntRange(min=0),
    default=None,
    help=(
        "Upgrade generations to keep per agent (host tarball + VM snapshot + "
        "image tag each). Default: [upgrades].keep_generations from config, "
        "or 2. 0 deletes every upgrade artifact — no rollback afterwards."
    ),
)
@click.option("--dry-run", is_flag=True, help="Show what would be deleted; delete nothing.")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def agents_prune(
    ctx: click.Context, name: str | None, keep: int | None, dry_run: bool, yes_local: bool
) -> None:
    """Delete old upgrade artifacts (tarballs, VM snapshots, image tags).

    Runs automatically after every successful ``agx upgrade`` (see
    ``[upgrades].keep_generations``); this command is the manual sweep
    for history that predates that hook, or for a custom ``--keep``.
    """
    _run(
        prune_cmd.prune,
        name,
        keep=keep,
        dry_run=dry_run,
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


# ---------- doctor-cron ----------


@main.group("doctor-cron")
def doctor_cron_group() -> None:
    """Host cron that runs `agx doctor` and mails on state change.

    Catches "the agent silently stopped working" (e.g. an expired Codex
    OAuth credential) within one interval instead of when you next need
    the agent. Mail is delivered through the host's `sendmail`.
    """


@doctor_cron_group.command("install")
@click.option(
    "--every",
    "every_minutes",
    type=click.IntRange(1, 59),
    default=15,
    show_default=True,
    help="Poll interval in minutes.",
)
@click.option(
    "--mail",
    "mail_to",
    multiple=True,
    help="Recipient address (repeatable). Required on first install.",
)
def doctor_cron_install(every_minutes: int, mail_to: tuple[str, ...]) -> None:
    """Install (or replace) the host cron entry."""
    from agx_cli import doctor_cron

    _run(doctor_cron.install, every_minutes=every_minutes, mail_to=list(mail_to))


@doctor_cron_group.command("uninstall")
def doctor_cron_uninstall() -> None:
    """Remove the host cron entry."""
    from agx_cli import doctor_cron

    _run(doctor_cron.uninstall)


@doctor_cron_group.command("status")
def doctor_cron_status() -> None:
    """Show install state, last run, and currently failing checks."""
    from agx_cli import doctor_cron

    _run(doctor_cron.status)


@doctor_cron_group.command("run")
def doctor_cron_run() -> None:
    """One poll: run doctor, mail if the failing set changed (what cron calls)."""
    from agx_cli import doctor_cron

    _run(doctor_cron.run)


# ---------- codex helpers ----------


@main.group("codex")
def codex_group() -> None:
    """Codex CLI *skill* for the Hermes sandbox (toolkit-scoped auth).

    This is NOT how a Hermes agent authenticates its `openai-codex` LLM
    provider — do that inside the container, where Hermes owns the
    credential and its refresh cycle:

        agx exec <name> -- hermes auth add openai-codex --type oauth --no-browser
    """


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
    """Authenticate the toolkit-scoped Codex account (sandbox skill only)."""
    _run(codex_mod.login, device_auth=device_auth)


def _retired_codex_refresh() -> None:
    raise UserError(
        "`agx codex refresh` was retired: refreshing the toolkit-scoped Codex "
        "auth rotates a single-use OAuth refresh chain that a running Hermes agent "
        "may share, invalidating the agent's token (\"Your authentication token has "
        'been invalidated"). Re-authenticate the agent inside its container '
        "instead: `agx exec <name> -- hermes auth add openai-codex --type oauth "
        "--no-browser`. For the sandbox skill, `agx codex login`."
    )


@codex_group.command("refresh", hidden=True, context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def codex_refresh(args: tuple[str, ...]) -> None:
    """Retired — see `hermes auth add openai-codex` inside the container."""
    _run(_retired_codex_refresh)


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
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip confirmation.")
@click.pass_context
def ollama_clear(ctx: click.Context, yes_local: bool) -> None:
    """Drop the Ollama configuration and any stored API key."""
    yes = yes_local or ctx.obj.get("yes", False)
    _run(ollama_cmd.clear, yes=yes)


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
@click.option(
    "--profile",
    default=None,
    help="Target a named Hermes profile (its own HERMES_HOME under profiles/<name>/).",
)
def memory_enable(
    plugin: str,
    agent: str,
    embedder: str,
    llm: str,
    image_tag: str | None,
    profile: str | None,
) -> None:
    """Enable a memory backend for an agent (optionally a named profile)."""
    opts: dict[str, Any] = {"embedder": embedder, "llm": llm}
    if image_tag is not None:
        opts["image_tag"] = image_tag
    _run(memory_mod.enable, plugin, agent, profile=profile, **opts)


@memory_group.command("disable")
@click.argument("agent")
@click.option(
    "--profile",
    default=None,
    help="Target a named Hermes profile (its own HERMES_HOME under profiles/<name>/).",
)
def memory_disable(agent: str, profile: str | None) -> None:
    """Disable the currently-enabled memory backend for an agent."""
    _run(memory_mod.disable, agent, profile=profile)


@memory_group.command("status")
@click.argument("agent", required=False)
def memory_status(agent: str | None) -> None:
    """Show memory state per agent (or for a single agent if NAME is given)."""
    _run(memory_mod.status, agent)


# ---------- profiles ----------


@main.group("profile")
def profile_group() -> None:
    """Inspect a Hermes agent's profiles (Hermes owns create/delete)."""


@profile_group.command("list")
@click.argument("agent")
def profile_list(agent: str) -> None:
    """List profiles with config/session presence and gateway state.

    Profile names listed here are what every ``--profile`` flag targets
    (secret, conversations, model use, memory). Create/delete profiles
    inside the container: ``agx exec <agent> hermes profile create <p>``.
    """
    _run(profile_cmd.list_profiles, agent)


# ---------- edit ----------


@main.command("edit")
@click.argument("agent")
@click.argument("path")
@click.option(
    "--profile",
    default=None,
    help="Resolve PATH inside this named profile's home instead of /opt/data.",
)
def edit(agent: str, path: str, profile: str | None) -> None:
    """Edit one of a Hermes agent's text files in $VISUAL/$EDITOR.

    PATH is relative to the agent's /opt/data (e.g. ``SOUL.md``,
    ``skills/<id>/SKILL.md``). The file is read and written through the
    running container as its runtime user — no sudo and no host access to
    the subuid-owned data dir needed. ``config.yaml`` edits go through the
    same validation as every other toolkit config write (a ``0.0.0.0``
    dashboard bind is refused). Needs an interactive terminal.
    """
    _run(edit_cmd.edit, agent=agent, path=path, profile=profile)


# ---------- tailscale ----------


@main.group("tailscale")
def tailscale_group() -> None:
    """Tailnet access to the VM (tailscaled as a VM-OS systemd service)."""


@tailscale_group.command("up")
@click.option(
    "--auth-key",
    default=None,
    help=(
        "Tailscale auth key (from the admin console) for non-interactive join. "
        "Without it, the login URL is printed for browser authentication."
    ),
)
@click.option(
    "--hostname",
    default=None,
    help="Tailnet hostname for the VM (default: the VM name).",
)
def tailscale_up(auth_key: str | None, hostname: str | None) -> None:
    """Install tailscale in the VM (idempotent) and join the tailnet."""
    _run(tailscale_cmd.up, auth_key=auth_key, hostname=hostname)


@tailscale_group.command("down")
def tailscale_down() -> None:
    """Disconnect the VM from the tailnet (tailscaled stays installed)."""
    _run(tailscale_cmd.down)


@tailscale_group.command("status")
def tailscale_status() -> None:
    """Show tailnet connection state, IPs, and reachable published ports."""
    _run(tailscale_cmd.status)


@tailscale_group.command("upgrade")
def tailscale_upgrade() -> None:
    """Upgrade the tailscale package inside the VM."""
    _run(tailscale_cmd.upgrade)


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


# ---------- local inference engines ----------


@main.group("inference")
def inference_group() -> None:
    """Local inference engines (Ollama, llama.cpp) running inside the VM."""


@inference_group.command("up")
@click.argument("engine", type=click.Choice(["ollama", "llama-cpp"]))
@click.option("--upgrade", is_flag=True, help="Re-resolve the upstream image digest.")
def inference_up(engine: str, upgrade: bool) -> None:
    """Start the named engine (or pin its image for llama-cpp)."""
    _run(inference_cmd.up, engine, upgrade=upgrade)


@inference_group.command("down")
@click.argument("engine", type=click.Choice(["ollama", "llama-cpp"]))
def inference_down(engine: str) -> None:
    """Stop and remove the engine's compose service(s). Model weights preserved."""
    _run(inference_cmd.down, engine)


@inference_group.command("status")
def inference_status() -> None:
    """Snapshot every in-VM inference engine."""
    _run(inference_cmd.status)


@inference_group.command("load")
@click.argument("engine", type=click.Choice(["llama-cpp"]))
@click.argument("slug")
@click.option("--gguf", required=True, help="GGUF filename under <data-dir>/models/llama-cpp/.")
@click.option("--port", type=int, default=8080, help="Host port (bound to 127.0.0.1).")
@click.option("--ctx-size", type=int, default=4096, help="Context window in tokens.")
@click.option("--n-gpu-layers", type=int, default=0, help="Layers to offload to GPU (CPU-only: 0).")
def inference_load(
    engine: str, slug: str, gguf: str, port: int, ctx_size: int, n_gpu_layers: int
) -> None:
    """Spawn a llama-server for one GGUF model.

    Ollama doesn't need this — it loads models on demand. llama-cpp's
    server is one-model-per-process, so each loaded model becomes its
    own compose service named ``llama-cpp-<slug>``.
    """
    if engine != "llama-cpp":
        raise click.UsageError("`inference load` is llama-cpp-only")
    _run(
        inference_cmd.up_llamacpp_model,
        slug,
        gguf_relative_path=gguf,
        port=port,
        ctx_size=ctx_size,
        n_gpu_layers=n_gpu_layers,
    )


@inference_group.command("unload")
@click.argument("engine", type=click.Choice(["llama-cpp"]))
@click.argument("slug")
def inference_unload(engine: str, slug: str) -> None:
    """Stop and remove one llama-cpp per-model service."""
    if engine != "llama-cpp":
        raise click.UsageError("`inference unload` is llama-cpp-only")
    _run(inference_cmd.down_llamacpp_model, slug)


# ---------- models ----------


@main.group("model")
def model_group() -> None:
    """Pull / list / remove local models; wire an agent to one."""


@model_group.command("pull")
@click.argument("engine", type=click.Choice(["ollama", "llama-cpp"]))
@click.argument("model")
def model_pull(engine: str, model: str) -> None:
    """Pull a model.

    For ollama: ``<name>:<tag>`` (Ollama Hub) or
    ``hf.co/<repo>:<quant>`` (HuggingFace via Ollama's native support).

    For llama-cpp: ``<hf-repo>:<filename>`` — downloads the GGUF
    into <data-dir>/models/llama-cpp/.
    """
    _run(models_cmd.pull, engine, model)


@model_group.command("list")
@click.argument("engine", required=False, type=click.Choice(["ollama", "llama-cpp"]))
def model_list(engine: str | None) -> None:
    """List downloaded models. Omits the engine arg to list both."""
    _run(models_cmd.list_models, engine)


@model_group.command("rm")
@click.argument("engine", type=click.Choice(["ollama", "llama-cpp"]))
@click.argument("model")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip confirmation.")
@click.pass_context
def model_rm(ctx: click.Context, engine: str, model: str, yes_local: bool) -> None:
    """Remove a downloaded model."""
    _run(models_cmd.rm, engine, model, yes=yes_local or ctx.obj.get("yes", False))


@model_group.command("use")
@click.argument("agent")
@click.option(
    "--engine", required=True, type=click.Choice(["ollama", "llama-cpp", "external-ollama"])
)
@click.option("--model", required=True, help="Model name as known by the engine.")
@click.option(
    "--aux",
    default=None,
    type=click.Choice(["compression", "vision", "all"]),
    help="Also route the named auxiliary task(s) to this engine.",
)
@click.option(
    "--context-length",
    type=click.IntRange(min=1024),
    default=None,
    help=(
        "Effective context window to declare to Hermes (model.context_length + a "
        "per-model custom_providers entry, so /model switches stay correct). "
        "REQUIRED for external-ollama: pass the server's OLLAMA_CONTEXT_LENGTH cap "
        "— it can't be probed remotely, and the model's native window is usually larger."
    ),
)
@click.option(
    "--profile",
    default=None,
    help="Target a named Hermes profile (its own HERMES_HOME under profiles/<name>/).",
)
def model_use(
    agent: str,
    engine: str,
    model: str,
    aux: str | None,
    context_length: int | None,
    profile: str | None,
) -> None:
    """Wire a Hermes agent's main model to an inference engine.

    Writes the structured ``model:`` block in the agent's config.yaml
    (provider=custom + base_url + api_key). In-VM engines resolve via
    Docker DNS; ``external-ollama`` targets the ``[ollama]`` config
    block's URL (e.g. GPU inference on the host). With ``--aux``, also
    reroutes auxiliary tasks (compression/vision/all) to the same
    endpoint.
    """
    _run(
        models_cmd.use,
        agent,
        engine=engine,
        model=model,
        aux=aux,
        context_length=context_length,
        profile=profile,
    )


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
@click.option(
    "--profile",
    default=None,
    help="Target a named Hermes profile (its own HERMES_HOME under profiles/<name>/).",
)
def secret_set(agent: str, key: str, value: str | None, profile: str | None) -> None:
    """Set a secret. Value is prompted hidden when omitted.

    Examples:

        agx secret set coder FIRECRAWL_API_KEY      # hidden prompt
        agx secret set coder FIRECRAWL_API_KEY fc-x # value as arg
    """

    def body() -> None:
        secret = value
        if secret is None:
            # A hidden prompt under --json would hang any machine
            # consumer waiting for the single stdout object.
            if ui.is_json():
                raise UserError("secret value required: pass it as an argument in --json mode")
            secret = click.prompt(f"value for {key}", hide_input=True, confirmation_prompt=False)
        secrets_cmd.set_secret(agent, key, secret, profile=profile)

    _run(body)


@secret_group.command("unset")
@click.argument("agent")
@click.argument("key")
@click.option(
    "--profile",
    default=None,
    help="Target a named Hermes profile (its own HERMES_HOME under profiles/<name>/).",
)
def secret_unset(agent: str, key: str, profile: str | None) -> None:
    """Remove a secret from the per-agent .env."""
    _run(secrets_cmd.unset_secret, agent, key, profile=profile)


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
    """Wipe ALL secrets for an agent (sweeps every profile's passthrough list)."""
    yes = yes_local or ctx.obj.get("yes", False)
    _run(secrets_cmd.clear_secrets, agent, yes=yes)


# ---------- mounts ----------


@main.group("mount")
def mount_group() -> None:
    """Give an agent access to specific host folders.

    Adds a per-agent host→VM→container mount chain. Read-only by
    default. ``--target sandbox`` (Hermes only) also exposes the
    folder to the LLM's shell tools via ``terminal.docker_volumes``;
    ``--target both`` does both runtime + sandbox. The default per
    agent type is ``both`` for Hermes, ``runtime`` for OpenClaw.
    """


@mount_group.command("add")
@click.argument("agent")
@click.option("--host", "host_path", required=True, type=click.Path(), help="Host path to share.")
@click.option(
    "--container",
    "container_path",
    required=True,
    metavar="PATH",
    help="Path inside the container (e.g. /workspace/x).",
)
@click.option(
    "--rw",
    "writable",
    is_flag=True,
    help="Allow the agent to write. Default is read-only; --rw warns explicitly.",
)
@click.option(
    "--target",
    type=click.Choice(["runtime", "sandbox", "both"]),
    default=None,
    help=(
        "Where to expose the mount: runtime container, Hermes sandbox container, "
        "or both. Defaults to 'both' for Hermes, 'runtime' for OpenClaw."
    ),
)
@click.option("--restart", is_flag=True, help="Restart the agent after adding to apply the mount.")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip the --rw safety prompt.")
@click.pass_context
def mount_add(
    ctx: click.Context,
    agent: str,
    host_path: str,
    container_path: str,
    writable: bool,
    target: str | None,
    restart: bool,
    yes_local: bool,
) -> None:
    """Add a host folder to ``agent``'s container."""
    yes = yes_local or ctx.obj.get("yes", False)

    def body() -> None:
        # ui.confirm (not raw click.confirm) so --json without --yes
        # refuses cleanly instead of hanging, and the decline maps to
        # the toolkit's user-error exit (1) rather than click.Abort.
        if (
            writable
            and not yes
            and not ui.confirm(
                f"--rw lets the agent (and any prompt-injected commands) modify files at "
                f"{host_path}. A compromised agent could overwrite or exfiltrate them. "
                f"Continue?",
                default=False,
            )
        ):
            raise UserError("aborted by user")
        mounts_cmd.add(
            agent=agent,
            host=host_path,
            container=container_path,
            readonly=not writable,
            target=target,
            restart=restart,
            config_path=cfg_mod.DEFAULT_CONFIG_PATH,
        )

    _run(body)


@mount_group.command("remove")
@click.argument("agent")
@click.option(
    "--container",
    "container_path",
    required=True,
    metavar="PATH",
    help="Container path of the mount to remove.",
)
@click.option("--restart", is_flag=True, help="Restart the agent after removal to drop the mount.")
def mount_remove(agent: str, container_path: str, restart: bool) -> None:
    """Remove a previously-added mount from ``agent``."""
    _run(
        mounts_cmd.remove,
        agent=agent,
        container=container_path,
        restart=restart,
        config_path=cfg_mod.DEFAULT_CONFIG_PATH,
    )


@mount_group.command("list")
@click.argument("agent", required=False)
def mount_list(agent: str | None) -> None:
    """List mounts for an agent (or all agents if omitted)."""
    _run(mounts_cmd.list_mounts, agent=agent, config_path=cfg_mod.DEFAULT_CONFIG_PATH)


# ---------- access (idmapped host view of an agent's state) ----------


@main.group("access")
def access_group() -> None:
    """Read (or edit) an agent's files from the host via a mounted view.

    The agent's container writes its state as a rootless-Docker subuid
    under mode 700, which the host user can't open. ``access mount``
    bind-mounts that directory at ``~/agents-view/<agent>`` with the owner
    ids swapped (an idmapped mount), so through the view you are the
    owner — immune to the chmod/ACL-mask drift the old ``access grant``
    suffered from. For a quick edit without any mount: ``agx edit``.
    """


@access_group.command("mount")
@click.argument("agent")
@click.option("--rw", "writable", is_flag=True, help="Mount read-write (default: read-only).")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip the sudo confirmation.")
@click.pass_context
def access_mount(ctx: click.Context, agent: str, writable: bool, yes_local: bool) -> None:
    """Mount a host view of AGENT's state under the view root.

    Needs sudo once (mounting is root-only); the exact command is printed
    before it runs. Always nosymfollow,nosuid,nodev,noexec. Prints a
    ``nofail`` /etc/fstab line to make the view survive reboots — the
    toolkit never edits /etc itself.
    """
    yes = yes_local or ctx.obj.get("yes", False)
    _run(access_cmd.mount, agent=agent, writable=writable, yes=yes)


@access_group.command("unmount")
@click.argument("agent")
@click.option("-y", "--yes", "yes_local", is_flag=True, help="Skip the sudo confirmation.")
@click.pass_context
def access_unmount(ctx: click.Context, agent: str, yes_local: bool) -> None:
    """Unmount AGENT's host view(s) and remove the empty mountpoint."""
    yes = yes_local or ctx.obj.get("yes", False)
    _run(access_cmd.unmount, agent=agent, yes=yes)


def _retired_access_command(name: str) -> None:
    raise UserError(
        f"`agx access {name}` was retired: host ACLs can't hold on an agent's data dir "
        "(files created with a restrictive mode are born with a masked ACL). Use "
        "`agx access mount <agent>` for a host view, or `agx edit <agent> <path>`."
    )


@access_group.command("grant", hidden=True, context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def access_grant(args: tuple[str, ...]) -> None:
    """Retired — see `agx access mount`."""
    _run(_retired_access_command, "grant")


@access_group.command("revoke", hidden=True, context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def access_revoke(args: tuple[str, ...]) -> None:
    """Retired — see `agx access mount`."""
    _run(_retired_access_command, "revoke")


@access_group.command("list")
@click.argument("agent", required=False)
def access_list(agent: str | None) -> None:
    """Show registered host views and whether each is mounted."""
    _run(access_cmd.list_views, agent=agent)


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
