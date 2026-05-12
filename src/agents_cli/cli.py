"""Root CLI group. Subcommand groups are registered here as they land."""

from __future__ import annotations

import click

from agents_cli import __version__


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


# Subcommand groups are imported and registered here once they land. Keeping
# the import block local to this function so partial implementations don't
# break the entry point during early development.
def _register_subcommands() -> None:
    # Imports deferred — wired up as each module lands.
    pass


_register_subcommands()


if __name__ == "__main__":
    main()
