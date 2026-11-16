"""Config schema and round-trip persistence.

`~/.config/agx/config.toml` is owned by the CLI. The pydantic v2 schema
validates every load; tomlkit preserves any user comments across saves.

The schema encodes the architecture (see docs/architecture.md): a single
host data-dir as the source of truth, captured VM runtime fields populated
by BOOTSTRAP_OK, and pinned image tags managed by `vm init`.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agx_cli.errors import UserError

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agx" / "config.toml"


def _refuse_forbidden_data_dir(path: Path) -> None:
    """Multipass-snap can't read paths under /home/*/snap or /var/snap."""
    parts = path.parts
    if parts[:2] == ("/", "var") and len(parts) >= 3 and parts[2] == "snap":
        raise ValueError(f"data-dir under /var/snap is unreadable by multipass-snap: {path}")
    if len(parts) >= 4 and parts[0] == "/" and parts[1] == "home" and parts[3] == "snap":
        raise ValueError(f"data-dir under /home/*/snap is unreadable by multipass-snap: {path}")


def default_backend() -> str:
    """Backend ``agx vm init`` uses when ``--backend`` is omitted:
    ``libvirt`` on Linux (virtiofs share, GPU passthrough, no snap),
    ``multipass`` elsewhere (macOS)."""
    return "libvirt" if platform.system() == "Linux" else "multipass"


class VmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "agents"
    cpus: int = Field(default=4, ge=1)
    memory_gb: int = Field(default=8, ge=2)
    disk_gb: int = Field(default=25, ge=10)
    # 24.04 LTS is the Docker-CE stable target — `get.docker.com/rootless`
    # supports it directly. 26.04 may not be in Docker's apt repos at any
    # given moment (newer LTS releases lag a few months); opt in with
    # `vm init --ubuntu-release 26.04` once that's no longer the case.
    ubuntu_release: str = "24.04"
    # VM hypervisor backend, chosen at ``agx vm init`` (without
    # ``--backend``: :func:`default_backend`) and persisted here; every
    # host↔VM call in ``mp.py`` dispatches to the matching module under
    # ``agx_cli.backends``. The field default stays ``multipass`` on
    # purpose: a config.toml without a ``backend`` key predates the
    # libvirt backend and describes a multipass deployment. Making it
    # platform-dependent would silently switch those deployments.
    backend: str = "multipass"
    # GPU PCI IDs to pass through (libvirt backend only). Form:
    # ``["0000:01:00.0", "0000:01:00.1"]``. Populated by
    # ``agx vm init --backend libvirt --gpu`` (auto-detect) or
    # ``--gpu <pci-id>`` (explicit). Empty list = no passthrough.
    gpu_passthrough: list[str] = Field(default_factory=list)

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, v: str) -> str:
        if v not in ("multipass", "libvirt", "bare_metal"):
            raise ValueError(
                f"vm.backend must be 'multipass', 'libvirt', or 'bare_metal', got {v!r}"
            )
        return v

    @field_validator("gpu_passthrough")
    @classmethod
    def _validate_gpu_ids(cls, v: list[str]) -> list[str]:
        import re

        for pci in v:
            if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", pci):
                raise ValueError(
                    f"gpu_passthrough id {pci!r} must be DDDD:BB:SS.F (e.g. 0000:01:00.0)"
                )
        return v


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_data_dir: Path
    vm_agents_root: Path = Path("/home/ubuntu/agents")
    data_dir_in_vm: bool = False
    # Root for `agx access mount` views. Unset = ``~/agents-view``
    # (resolved at use time and never written back to config.toml).
    host_view_dir: Path | None = None

    @field_validator("host_data_dir", mode="before")
    @classmethod
    def _resolve_host_data_dir(cls, v: Any) -> Path:
        if not v:
            raise ValueError("host_data_dir is required")
        p = Path(str(v)).expanduser()
        if not p.is_absolute():
            raise ValueError(f"host_data_dir must be absolute: {v}")
        _refuse_forbidden_data_dir(p)
        return p

    @field_validator("host_view_dir", mode="before")
    @classmethod
    def _resolve_host_view_dir(cls, v: Any) -> Path | None:
        if v is None or v == "":
            return None
        p = Path(str(v)).expanduser()
        if not p.is_absolute():
            raise ValueError(f"host_view_dir must be absolute: {v}")
        return p

    @field_validator("vm_agents_root", mode="before")
    @classmethod
    def _resolve_vm_agents_root(cls, v: Any) -> Path:
        # Path goes into VM-side shell scripts via exec_bash heredocs, so
        # restrict to characters that don't need shell escaping. Defaults
        # to /home/ubuntu/agents and rarely needs changing.
        import re

        p = Path(str(v))
        if not p.is_absolute():
            raise ValueError(f"vm_agents_root must be absolute: {v}")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", str(p)):
            raise ValueError(
                f"vm_agents_root must contain only [A-Za-z0-9_./-]; got {v!r}. "
                "Shell-meta characters in this path would break exec_bash heredocs."
            )
        # `agx remove` deletes `<vm_agents_root>/<name>` as root inside
        # the VM; a root this shallow (or one that climbs back out) would
        # aim that at system directories.
        if len(p.parts) < 3 or ".." in p.parts:
            raise ValueError(f"vm_agents_root must be at least two levels deep with no '..': {v!r}")
        return p


class HostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uid: int = Field(default_factory=os.getuid)
    gid: int = Field(default_factory=os.getgid)


class VmRuntimeConfig(BaseModel):
    """Populated by `vm init` from the BOOTSTRAP_OK handshake. No hand defaults."""

    model_config = ConfigDict(extra="forbid")

    docker_socket_path: str = ""
    ubuntu_uid: int = 0
    ubuntu_gid: int = 0

    def is_populated(self) -> bool:
        return bool(self.docker_socket_path) and self.ubuntu_uid > 0


class DefaultsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hermes_image_tag: str = ""
    openclaw_image_tag: str = ""
    hermes_base_port: int = 8642
    openclaw_base_port: int = 18789
    hermes_mem_limit: str = "2g"
    hermes_cpus: float = 1.5


class OllamaConfig(BaseModel):
    """Optional OpenAI-compatible endpoint used by memory backends.

    The toolkit does NOT manage an Ollama instance — the user points the
    toolkit at one they already run (host-side, on a LAN box, behind
    Tailscale, or a hosted Ollama-compatible service). Memory plugins
    that need embeddings/LLM read from this block and translate it into
    each backend's own config dialect.

    ``url`` must be reachable *from inside the VM* (and, indirectly, from
    inside the Hermes container). ``localhost`` resolves to the VM
    itself, not the user's host — ``agx ollama configure`` rejects it
    explicitly and suggests the right form (multipass NAT gateway IP,
    LAN/Tailscale IP, or a public OpenAI-compatible URL).

    ``api_key_env`` references a key name in ``<data-dir>/.env`` so the
    actual credential never lands in this TOML file. Empty string means
    no auth (vanilla Ollama, no reverse proxy).
    """

    model_config = ConfigDict(extra="forbid")

    url: str = ""
    api_key_env: str = ""
    embed_model: str = "nomic-embed-text"
    llm_model: str = "qwen2.5:7b-instruct"

    def is_configured(self) -> bool:
        return bool(self.url)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        if not v:
            return v
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"ollama.url must be http(s)://... — got {v!r}")
        # Reject the obvious host-vs-VM trap. Memory backends called from
        # the Hermes container would route localhost back to the VM.
        # urlsplit().hostname lowercases and strips IPv6 brackets, so
        # ``http://LOCALHOST`` and ``http://[::1]`` can't slip past the
        # blocklist the way a hand-rolled split let them.
        try:
            host = urlsplit(v).hostname or ""
        except ValueError as e:
            raise ValueError(f"ollama.url is not a valid URL: {v!r} ({e})") from e
        if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
            raise ValueError(
                f"ollama.url host {host!r} is not reachable from inside the "
                "VM. Use the multipass NAT gateway (typically 10.x.x.1), a "
                "LAN/Tailscale IP, or a public hostname."
            )
        return v


class InferenceConfig(BaseModel):
    """Local inference engines running INSIDE the VM as sibling Docker
    services. Distinct from :class:`OllamaConfig` which describes an
    EXTERNAL Ollama instance reachable over the network — both can
    coexist (e.g. external GPU box for the heavy model, in-VM CPU
    Ollama for a small embedding model).

    The toolkit owns the lifecycle of in-VM engines (``agx inference
    up/down``), pins their images by digest on first up, and writes
    the compose entries. Memory backends + agent wiring prefer this
    block over the external ``[ollama]`` when both are populated AND
    the in-VM engine is healthy — see ``agx model use`` for the
    explicit per-agent wiring command.
    """

    model_config = ConfigDict(extra="forbid")

    # True after `agx inference up ollama` has succeeded at least
    # once. Read by doctor / models commands to decide whether to
    # probe the in-VM engine at all.
    ollama_enabled: bool = False
    ollama_port: int = 11434
    # Digest-pinned image refs, written on first `agx inference up`
    # and reused thereafter (same model as firecrawl-state). Empty
    # until first up.
    ollama_image: str = ""
    # llama.cpp doesn't have a single "enabled" — each loaded model is
    # a separate service. Just tracks the pinned image used to spawn
    # them.
    llamacpp_image: str = ""


class ExportCronConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Comma-separated zero-padded HH:MM list. One crontab entry per
    # time. Allows multi-times-a-day backups (e.g. "02:30,08:30,14:30,
    # 20:30") for SQLite-heavy workloads where 24h granularity is too
    # coarse.
    time: str = "02:30"
    # Per-agent retention of toolkit-written tarballs in the default
    # ``<data-dir>/backups/`` directory. After every successful
    # ``agx export`` the toolkit deletes the agent's oldest
    # archives, keeping only the newest ``keep_last``. Duplicity (or
    # the user's chosen host-backup tool) is the long-history layer;
    # this rolling window is just in-flight protection between
    # nightly off-host runs.
    #
    # Default 14 = ~3.5 days at the example 4x/day schedule, or 14
    # days at a single nightly run. Set to 0 to disable pruning
    # (full history accumulates; user responsible for cleanup).
    # Archives the user wrote to a custom ``--to <path>`` location
    # are NEVER pruned by the toolkit.
    keep_last: int = Field(default=14, ge=0)

    @field_validator("time")
    @classmethod
    def _validate_hhmm(cls, v: str) -> str:
        for piece in v.split(","):
            piece = piece.strip()
            parts = piece.split(":")
            if (
                len(parts) != 2
                or len(parts[0]) != 2
                or len(parts[1]) != 2
                or not all(p.isdigit() for p in parts)
            ):
                raise ValueError(
                    f"export_cron.time must be zero-padded HH:MM "
                    f"(or comma-separated HH:MM,HH:MM,...), got {v!r}"
                )
            hh, mm = int(parts[0]), int(parts[1])
            if not (0 <= hh < 24 and 0 <= mm < 60):
                raise ValueError(f"export_cron.time out of range: {v!r}")
        return v


class DoctorCronConfig(BaseModel):
    """``agx doctor-cron``: host cron that runs ``agx doctor`` and
    mails on state change (a check turning red, or everything green
    again). Mail goes out through the host's ``sendmail`` — the toolkit
    carries no SMTP configuration of its own.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    every_minutes: int = 15
    mail_to: list[str] = Field(default_factory=list)
    # Hours between upstream release checks (one mail per new release
    # per agent, with condensed release notes). 0 disables them.
    update_check_hours: int = 24

    @field_validator("update_check_hours")
    @classmethod
    def _validate_update_check_hours(cls, v: int) -> int:
        if not (0 <= v <= 24 * 7):
            raise ValueError(f"doctor_cron.update_check_hours must be 0..168, got {v}")
        return v

    @field_validator("every_minutes")
    @classmethod
    def _validate_every(cls, v: int) -> int:
        if not (1 <= v <= 59):
            raise ValueError(f"doctor_cron.every_minutes must be 1..59, got {v}")
        return v

    @field_validator("mail_to")
    @classmethod
    def _validate_mail_to(cls, v: list[str]) -> list[str]:
        for addr in v:
            if "@" not in addr or any(c.isspace() for c in addr) or "," in addr:
                raise ValueError(f"doctor_cron.mail_to: not a mail address: {addr!r}")
        return v


class UpgradesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Post-upgrade retention. After every successful ``agx upgrade``
    # the toolkit keeps only the newest ``keep_generations`` upgrade
    # artifacts per agent — host tarball under ``.upgrades/``,
    # ``pre-upgrade-*`` VM snapshot, and superseded image tags — and
    # deletes the rest. Rationale: `agx rollback` only ever uses the
    # latest generation, and unbounded accumulation can fill a 58 GB VM
    # disk within ~3 months of routine upgrades.
    # 0 disables the automatic prune; `agx prune` remains available
    # for manual cleanup either way.
    keep_generations: int = Field(default=2, ge=0)


class AgentsConfig(BaseModel):
    """Top-level toolkit config."""

    model_config = ConfigDict(extra="forbid")

    vm: VmConfig = Field(default_factory=VmConfig)
    paths: PathsConfig
    host: HostConfig = Field(default_factory=HostConfig)
    vm_runtime: VmRuntimeConfig = Field(default_factory=VmRuntimeConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    export_cron: ExportCronConfig = Field(default_factory=ExportCronConfig)
    doctor_cron: DoctorCronConfig = Field(default_factory=DoctorCronConfig)
    upgrades: UpgradesConfig = Field(default_factory=UpgradesConfig)


def load(path: Path = DEFAULT_CONFIG_PATH) -> AgentsConfig:
    """Read and validate config. Raises UserError if missing or malformed."""
    if not path.exists():
        raise UserError(f"Config not found at {path}. Run `agx vm init --data-dir <path>` first.")
    raw = path.read_text(encoding="utf-8")
    try:
        doc = tomlkit.parse(raw)
    except Exception as e:
        raise UserError(f"Config at {path} is not valid TOML: {e}") from e

    data = _toml_to_plain(doc)
    try:
        return AgentsConfig.model_validate(data)
    except Exception as e:
        raise UserError(f"Config at {path} failed schema validation: {e}") from e


def persist_default_image_tag(
    cfg: AgentsConfig, agent_type: str, tag: str | None, path: Path = DEFAULT_CONFIG_PATH
) -> None:
    """Make ``tag`` the ``defaults.<type>_image_tag`` baseline (saved only
    on change). Callers invoke it on SUCCESS paths only — a tag becomes
    the baseline once an agent demonstrably runs it."""
    if tag is None:
        return
    if agent_type == "hermes" and cfg.defaults.hermes_image_tag != tag:
        cfg.defaults.hermes_image_tag = tag
        save(cfg, path)
    elif agent_type == "openclaw" and cfg.defaults.openclaw_image_tag != tag:
        cfg.defaults.openclaw_image_tag = tag
        save(cfg, path)


def save(cfg: AgentsConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Write the config, preserving any existing comments via tomlkit round-trip."""
    path.parent.mkdir(parents=True, exist_ok=True)

    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()

    new_data = _model_to_plain(cfg)
    _merge_into_doc(doc, new_data)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)


def _toml_to_plain(obj: Any) -> Any:
    """Convert a tomlkit container/value tree into plain Python types."""
    if hasattr(obj, "unwrap"):
        return obj.unwrap()
    if isinstance(obj, dict):
        return {k: _toml_to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_toml_to_plain(v) for v in obj]
    return obj


def _model_to_plain(cfg: AgentsConfig) -> dict[str, Any]:
    """Pydantic dump with Paths coerced to strings (tomlkit doesn't grok PosixPath)."""
    # exclude_none: TOML has no null, and unset optionals must stay unset.
    data: dict[str, Any] = cfg.model_dump(mode="json", exclude_none=True)
    return data


def _merge_into_doc(doc: Any, new: dict[str, Any]) -> None:
    """Update `doc` in place with values from `new`, preserving comments on unchanged keys."""
    for k, v in new.items():
        if isinstance(v, dict):
            if k in doc and isinstance(doc[k], dict):
                _merge_into_doc(doc[k], v)
            else:
                doc[k] = v
        else:
            doc[k] = v
