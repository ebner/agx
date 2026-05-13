"""Config schema and round-trip persistence.

`~/.config/agents/config.toml` is owned by the CLI. The pydantic v2 schema
validates every load; tomlkit preserves any user comments across saves.

The schema mirrors the design in the plan: a single host data-dir as the
source of truth, captured VM runtime fields populated by BOOTSTRAP_OK, and
pinned image tags managed by `vm init`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents_cli.errors import UserError

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agents" / "config.toml"


def _refuse_forbidden_data_dir(path: Path) -> None:
    """Multipass-snap can't read paths under /home/*/snap or /var/snap."""
    parts = path.parts
    if parts[:2] == ("/", "var") and len(parts) >= 3 and parts[2] == "snap":
        raise ValueError(f"data-dir under /var/snap is unreadable by multipass-snap: {path}")
    if len(parts) >= 4 and parts[0] == "/" and parts[1] == "home" and parts[3] == "snap":
        raise ValueError(f"data-dir under /home/*/snap is unreadable by multipass-snap: {path}")


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


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_data_dir: Path
    vm_agents_root: Path = Path("/home/ubuntu/agents")
    data_dir_in_vm: bool = False

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


class ExportCronConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Comma-separated zero-padded HH:MM list. One crontab entry per
    # time. Allows multi-times-a-day backups (e.g. "02:30,08:30,14:30,
    # 20:30") for SQLite-heavy workloads where 24h granularity is too
    # coarse.
    time: str = "02:30"

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


class AgentsConfig(BaseModel):
    """Top-level toolkit config."""

    model_config = ConfigDict(extra="forbid")

    vm: VmConfig = Field(default_factory=VmConfig)
    paths: PathsConfig
    host: HostConfig = Field(default_factory=HostConfig)
    vm_runtime: VmRuntimeConfig = Field(default_factory=VmRuntimeConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    export_cron: ExportCronConfig = Field(default_factory=ExportCronConfig)


def load(path: Path = DEFAULT_CONFIG_PATH) -> AgentsConfig:
    """Read and validate config. Raises UserError if missing or malformed."""
    if not path.exists():
        raise UserError(
            f"Config not found at {path}. Run `agents vm init --data-dir <path>` first."
        )
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
    data = cfg.model_dump(mode="json")
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
