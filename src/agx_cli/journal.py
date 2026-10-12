"""Phase journals for multi-phase mutations.

Every multi-step op (``create``, ``remove``, ``rename``, ``upgrade``)
writes a JSON journal at ``<data-dir>/.journal/<op>-<name>-<utc>.json``
after each completed phase. If the process crashes mid-op, re-running
the command resumes from the last completed phase. ``doctor`` flags
any journal older than 24 hours.

Journal lifecycle:

- :meth:`Journal.begin` — declares an op, lists its phases, writes the
  initial file.
- :meth:`Journal.advance` — append a completed phase, atomic same-dir
  rename.
- :meth:`Journal.complete` — delete the file. Success state = no
  journal.
- :meth:`Journal.find_resumable` — look up any existing journal for
  ``(op, name)``.
- :meth:`Journal.find_dangling` — return every journal older than the
  threshold across all ops.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agx_cli.errors import UserError

JOURNAL_DIR_NAME = ".journal"


def _journal_dir(data_dir: Path) -> Path:
    d = data_dir / JOURNAL_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_stamp(dt: datetime) -> str:
    """ISO-8601 with seconds resolution, filesystem-safe (no colons)."""
    return dt.strftime("%Y%m%dT%H%M%SZ")


@dataclass
class Journal:
    path: Path
    op: str
    name: str
    started_at: datetime
    phases: list[str]
    completed_phases: list[str]
    # Op-scoped metadata captured at begin-time (e.g. the upgrade
    # state machine records {"old_tag", "new_tag"} so a resumed run
    # doesn't have to re-derive them from state the interrupted run
    # already mutated). Journals written before this field existed
    # load with an empty dict.
    meta: dict[str, Any] = field(default_factory=dict)

    # ---------- begin / advance / complete ----------

    @classmethod
    def begin(
        cls,
        data_dir: Path,
        op: str,
        name: str,
        phases: list[str],
        *,
        meta: dict[str, Any] | None = None,
    ) -> Journal:
        started = _utc_now()
        path = _journal_dir(data_dir) / f"{op}-{name}-{_utc_stamp(started)}.json"
        j = cls(
            path=path,
            op=op,
            name=name,
            started_at=started,
            phases=list(phases),
            completed_phases=[],
            meta=dict(meta) if meta else {},
        )
        j._write()
        return j

    def advance(self, phase: str) -> None:
        if phase not in self.phases:
            raise UserError(
                f"phase {phase!r} not declared for op {self.op!r}; expected one of {self.phases}"
            )
        if phase in self.completed_phases:
            return  # idempotent on re-runs
        self.completed_phases.append(phase)
        self._write()

    def complete(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def _write(self) -> None:
        payload = {
            "op": self.op,
            "name": self.name,
            "started_at": self.started_at.isoformat(),
            "phases": self.phases,
            "completed_phases": self.completed_phases,
            "meta": self.meta,
        }
        tmp = self.path.parent / f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()
            raise

    # ---------- resume / discovery ----------

    @classmethod
    def load(cls, path: Path) -> Journal:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            path=path,
            op=data["op"],
            name=data["name"],
            started_at=datetime.fromisoformat(data["started_at"]),
            phases=list(data.get("phases", [])),
            completed_phases=list(data.get("completed_phases", [])),
            meta=dict(data.get("meta") or {}),
        )

    @classmethod
    def find_resumable(cls, data_dir: Path, op: str, name: str) -> Journal | None:
        """Return the most-recent journal for ``(op, name)`` if any exists.

        The filename glob is a prefix match, so agent ``foo`` would also
        pick up ``upgrade-foo-bar-<utc>.json`` (and ``foo-bar`` sorts
        AFTER ``foo``'s own journals — letters beat the digits of the
        timestamp — so the wrong journal would win). Verify the loaded
        payload's ``(op, name)`` before returning; skip mismatches and
        corrupt files rather than giving up at the newest candidate.
        """
        d = _journal_dir(data_dir)
        for candidate in sorted(d.glob(f"{op}-{name}-*.json"), reverse=True):
            try:
                j = cls.load(candidate)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if j.op == op and j.name == name:
                return j
        return None

    @classmethod
    def find_dangling(
        cls,
        data_dir: Path,
        *,
        older_than: timedelta = timedelta(hours=24),
    ) -> list[Journal]:
        """Return every journal older than ``older_than``."""
        d = _journal_dir(data_dir)
        now = _utc_now()
        out: list[Journal] = []
        for p in d.glob("*.json"):
            try:
                j = cls.load(p)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if now - j.started_at >= older_than:
                out.append(j)
        return sorted(out, key=lambda j: j.started_at)

    # ---------- helpers ----------

    def remaining_phases(self) -> list[str]:
        return [p for p in self.phases if p not in self.completed_phases]

    def is_phase_done(self, phase: str) -> bool:
        return phase in self.completed_phases

    def age(self) -> timedelta:
        return _utc_now() - self.started_at
