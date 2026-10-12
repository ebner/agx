# Contributing

## Dev setup

Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/ebner/agx.git
cd agx
uv sync --locked --extra dev
```

## The gate

Every change must pass all four before a PR — CI runs the same set on
Python 3.11 and 3.12:

```bash
uv run pytest -q                     # 900+ hermetic unit tests
uv run mypy --strict src/agx_cli
uv run ruff check .
uv run ruff format --check .
```

The unit suite is hermetic by design: it must pass with no `multipass`
binary, no VM, and an empty `$HOME`. If your test needs a backend call,
stub `agx_cli.mp` — never invoke a real binary or read the developer's
real `~/.config/agents/config.toml` (both classes have bitten before).

## Integration tests

`tests/integration/` (marker `multipass`) drives the installed console
script against a **real, healthy deployment**. It is deselected by
default and never runs in CI:

```bash
uv run pytest -m multipass tests/integration/ -v
```

It creates one disposable `itest-` agent (image pull on first run) and
tears down everything it makes. See `tests/integration/conftest.py` for
the safety guarantees before extending it.

## Conventions that are load-bearing

`CLAUDE.md` is the full internal engineering guide (architecture rules,
cross-layer pitfalls); the short version:

- **All output goes through `ui.py`** — no `print`/`click.echo` in
  feature code, so `--json` always emits exactly one JSON object on
  stdout per command. Progress/warnings go to stderr via
  `ui.progress`/`ui.warn`.
- **Exit codes are contract:** 0 success, 1 user error, 2 runtime
  failure, 3 health-check failure.
- **Every mutation of shared state holds the `.agents.lock` flock**;
  compose writes use the staging → transfer → validate → atomic-rename
  pattern already in `compose.py`.
- **Pinned image tags only** (no `:latest`), and images must be on the
  allowlist in `compose.py`.
- Subprocesses use list-form argv — never `shell=True`, never
  interpolate user-controlled values into shell strings.
- Destructive operations confirm, and accept `-y/--yes`.
- Multi-phase operations write a resumable phase journal
  (`journal.py`); look at `upgrades.py` for the reference shape.

When you hit (or cause) a cross-layer surprise, add it to CLAUDE.md's
"Critical cross-layer pitfalls" section — that file is the project's
institutional memory.

## Pull requests

- Include tests for every behavior change, mirroring the existing style
  of the module's test file.
- Keep commits scoped and message bodies explanatory (the `git log` of
  this repo shows the house style).
- CI must be green.
