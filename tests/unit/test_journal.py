"""journal.py: phase tracking, resume, dangling detection."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agents_cli import journal
from agents_cli.errors import UserError
from agents_cli.journal import Journal


def test_begin_writes_initial_journal(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "create", "coder", ["a", "b", "c"])
    assert j.path.exists()
    payload = json.loads(j.path.read_text())
    assert payload["op"] == "create"
    assert payload["name"] == "coder"
    assert payload["phases"] == ["a", "b", "c"]
    assert payload["completed_phases"] == []


def test_advance_appends_completed_phase(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "create", "coder", ["a", "b"])
    j.advance("a")
    assert j.completed_phases == ["a"]
    on_disk = json.loads(j.path.read_text())
    assert on_disk["completed_phases"] == ["a"]


def test_advance_unknown_phase_raises(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "create", "coder", ["a", "b"])
    with pytest.raises(UserError, match="not declared"):
        j.advance("ghost")


def test_advance_is_idempotent(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "create", "coder", ["a", "b"])
    j.advance("a")
    j.advance("a")
    assert j.completed_phases == ["a"]


def test_complete_deletes_journal_file(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "create", "coder", ["a"])
    j.advance("a")
    j.complete()
    assert not j.path.exists()


def test_complete_when_already_deleted_is_safe(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "create", "coder", ["a"])
    j.path.unlink()
    j.complete()  # no raise


def test_find_resumable_returns_existing(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "create", "coder", ["a", "b"])
    j.advance("a")
    found = Journal.find_resumable(tmp_path, "create", "coder")
    assert found is not None
    assert found.completed_phases == ["a"]


def test_find_resumable_returns_none_when_absent(tmp_path: Path) -> None:
    assert Journal.find_resumable(tmp_path, "create", "coder") is None


def test_find_resumable_does_not_match_other_op_or_name(tmp_path: Path) -> None:
    Journal.begin(tmp_path, "create", "coder", ["a"])
    Journal.begin(tmp_path, "create", "personal", ["a"])
    Journal.begin(tmp_path, "remove", "coder", ["a"])
    found = Journal.find_resumable(tmp_path, "rename", "coder")
    assert found is None


def test_find_resumable_picks_most_recent_on_collision(tmp_path: Path) -> None:
    """If two journals exist for the same (op, name), pick the lexicographically
    last one — the timestamp-suffix ensures that's the newest."""
    d = tmp_path / ".journal"
    d.mkdir()
    older_payload = {
        "op": "create",
        "name": "coder",
        "started_at": "2026-05-01T00:00:00+00:00",
        "phases": ["a"],
        "completed_phases": [],
    }
    newer_payload = {**older_payload, "completed_phases": ["a"]}
    (d / "create-coder-20260501T000000Z.json").write_text(json.dumps(older_payload))
    (d / "create-coder-20260512T120000Z.json").write_text(json.dumps(newer_payload))
    found = Journal.find_resumable(tmp_path, "create", "coder")
    assert found is not None
    assert found.completed_phases == ["a"]


def test_find_resumable_skips_corrupt_files(tmp_path: Path) -> None:
    d = tmp_path / ".journal"
    d.mkdir()
    (d / "create-coder-bad.json").write_text("not json")
    assert Journal.find_resumable(tmp_path, "create", "coder") is None


def test_find_resumable_falls_back_past_corrupt_newest(tmp_path: Path) -> None:
    """A corrupt newest candidate must not hide an older valid journal —
    candidates are skipped individually, not abandoned at the last one."""
    d = tmp_path / ".journal"
    d.mkdir()
    valid = {
        "op": "create",
        "name": "coder",
        "started_at": "2026-05-01T00:00:00+00:00",
        "phases": ["a"],
        "completed_phases": ["a"],
    }
    (d / "create-coder-20260501T000000Z.json").write_text(json.dumps(valid))
    (d / "create-coder-20260512T120000Z.json").write_text("not json")
    found = Journal.find_resumable(tmp_path, "create", "coder")
    assert found is not None
    assert found.completed_phases == ["a"]


def test_find_resumable_ignores_prefix_colliding_agent_name(tmp_path: Path) -> None:
    """The filename glob for agent ``foo`` also matches agent ``foo-bar``'s
    journals, and ``foo-bar``'s sort AFTER ``foo``'s own (letters beat the
    timestamp digits) — so without payload verification the wrong agent's
    journal wins. Regression for the prefix-glob collision class."""
    d = tmp_path / ".journal"
    d.mkdir()
    foo = {
        "op": "upgrade",
        "name": "foo",
        "started_at": "2026-01-01T00:00:00+00:00",
        "phases": ["a"],
        "completed_phases": ["a"],
    }
    foo_bar = {**foo, "name": "foo-bar", "completed_phases": []}
    (d / "upgrade-foo-20260101T000000Z.json").write_text(json.dumps(foo))
    (d / "upgrade-foo-bar-20260201T000000Z.json").write_text(json.dumps(foo_bar))

    found = Journal.find_resumable(tmp_path, "upgrade", "foo")
    assert found is not None
    assert found.name == "foo"
    assert found.completed_phases == ["a"]

    # And foo-bar's own lookup is unaffected.
    other = Journal.find_resumable(tmp_path, "upgrade", "foo-bar")
    assert other is not None
    assert other.name == "foo-bar"


def test_find_resumable_returns_none_when_only_prefix_collision_exists(tmp_path: Path) -> None:
    Journal.begin(tmp_path, "upgrade", "foo-bar", ["a"])
    assert Journal.find_resumable(tmp_path, "upgrade", "foo") is None


def test_find_dangling_returns_old_journals(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "create", "coder", ["a"])
    old_payload = json.loads(j.path.read_text())
    old_payload["started_at"] = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    j.path.write_text(json.dumps(old_payload))

    dangling = Journal.find_dangling(tmp_path, older_than=timedelta(hours=24))
    assert len(dangling) == 1
    assert dangling[0].name == "coder"


def test_find_dangling_ignores_fresh_journals(tmp_path: Path) -> None:
    Journal.begin(tmp_path, "create", "coder", ["a"])
    dangling = Journal.find_dangling(tmp_path, older_than=timedelta(hours=24))
    assert dangling == []


def test_find_dangling_ignores_corrupt(tmp_path: Path) -> None:
    d = tmp_path / ".journal"
    d.mkdir()
    (d / "corrupt.json").write_text("totally not json")
    assert Journal.find_dangling(tmp_path) == []


def test_remaining_phases_filters_completed(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "create", "coder", ["a", "b", "c"])
    j.advance("a")
    j.advance("b")
    assert j.remaining_phases() == ["c"]


def test_is_phase_done(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "create", "coder", ["a", "b"])
    j.advance("a")
    assert j.is_phase_done("a")
    assert not j.is_phase_done("b")


def test_age_grows_with_time(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "create", "coder", ["a"])
    age1 = j.age()
    j.started_at = j.started_at - timedelta(seconds=10)
    age2 = j.age()
    assert age2 > age1


def test_load_round_trip(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "upgrade", "coder", ["snap", "pull", "doctor"])
    j.advance("snap")
    j.advance("pull")
    loaded = Journal.load(j.path)
    assert loaded.op == "upgrade"
    assert loaded.name == "coder"
    assert loaded.completed_phases == ["snap", "pull"]


def test_atomic_write_leaves_no_tmp_files(tmp_path: Path) -> None:
    Journal.begin(tmp_path, "create", "coder", ["a"])
    d = tmp_path / journal.JOURNAL_DIR_NAME
    assert not any(d.glob("*.tmp"))


# ---------- meta ----------


def test_begin_without_meta_defaults_to_empty(tmp_path: Path) -> None:
    j = Journal.begin(tmp_path, "create", "coder", ["a"])
    assert j.meta == {}
    payload = json.loads(j.path.read_text())
    assert payload["meta"] == {}


def test_meta_round_trips_through_advance_and_load(tmp_path: Path) -> None:
    j = Journal.begin(
        tmp_path,
        "upgrade",
        "coder",
        ["a", "b"],
        meta={"old_tag": "v1.0.0", "new_tag": "v2.0.0"},
    )
    j.advance("a")  # rewrite must preserve meta
    loaded = Journal.load(j.path)
    assert loaded.meta == {"old_tag": "v1.0.0", "new_tag": "v2.0.0"}
    assert loaded.completed_phases == ["a"]


def test_load_legacy_journal_without_meta_defaults_to_empty(tmp_path: Path) -> None:
    """Journals written before the meta field existed must load with
    ``meta == {}`` rather than raising KeyError."""
    d = tmp_path / ".journal"
    d.mkdir()
    legacy = {
        "op": "upgrade",
        "name": "coder",
        "started_at": "2026-05-01T00:00:00+00:00",
        "phases": ["a"],
        "completed_phases": [],
    }
    p = d / "upgrade-coder-20260501T000000Z.json"
    p.write_text(json.dumps(legacy))
    loaded = Journal.load(p)
    assert loaded.meta == {}
    found = Journal.find_resumable(tmp_path, "upgrade", "coder")
    assert found is not None
    assert found.meta == {}
