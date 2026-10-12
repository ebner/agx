"""idmap_view.py: spec/options/fstab builders and the mountinfo parser."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from agx_cli import idmap_view as iv


def test_spec_swaps_owner_and_maps_everything_else_to_itself() -> None:
    spec = iv.idmap_spec(host_uid=1000, host_gid=1000, disk_uid=100999, disk_gid=100999)
    assert spec.split() == [
        "u:1000:100999:1",
        "u:100999:1000:1",
        "u:0:0:1000",
        "u:1001:1001:99998",
        "u:101000:101000:4294866295",
        "g:1000:100999:1",
        "g:100999:1000:1",
        "g:0:0:1000",
        "g:1001:1001:99998",
        "g:101000:101000:4294866295",
    ]


def _covered(spec: str, kind: str) -> list[tuple[int, int]]:
    spans = []
    for entry in spec.split():
        k, mount_id, _host_id, count = entry.split(":")
        if k == kind:
            spans.append((int(mount_id), int(mount_id) + int(count)))
    return sorted(spans)


@pytest.mark.parametrize(
    ("host", "disk"), [(1000, 100999), (100999, 1000), (0, 1), (1000, 1001), (1000, 4294967294)]
)
def test_spec_ranges_tile_the_id_space_without_overlap(host: int, disk: int) -> None:
    """The kernel rejects overlapping extents, and a gap would turn the
    ids in it into `nobody`."""
    spec = iv.idmap_spec(host_uid=host, host_gid=host, disk_uid=disk, disk_gid=disk)
    for kind in ("u", "g"):
        spans = _covered(spec, kind)
        assert spans[0][0] == 0 and spans[-1][1] == 2**32 - 1
        assert all(a[1] == b[0] for a, b in pairwise(spans))


def test_spec_with_shared_group_keeps_gids_identity_mapped() -> None:
    spec = iv.idmap_spec(host_uid=1000, host_gid=1000, disk_uid=100999, disk_gid=1000)
    assert [e for e in spec.split() if e.startswith("g:")] == ["g:0:0:4294967295"]


def test_spec_refuses_a_source_the_host_user_already_owns() -> None:
    with pytest.raises(ValueError, match="already owned"):
        iv.idmap_spec(host_uid=1000, host_gid=1000, disk_uid=1000, disk_gid=1000)


def test_mount_options_default_read_only_and_always_hardened() -> None:
    ro = iv.mount_options("u:0:0:1", writable=False).split(",")
    rw = iv.mount_options("u:0:0:1", writable=True).split(",")
    assert ro[0] == "ro" and rw[0] == "rw"
    for opts in (ro, rw):
        assert {"nosuid", "nodev", "noexec", "nosymfollow"} <= set(opts)
        assert opts[-1] == "X-mount.idmap=u:0:0:1"


def test_fstab_line_escapes_every_field() -> None:
    line = iv.fstab_line(
        Path("/home/my user/agents/a/.data"),
        Path("/home/my user/agents-view/a"),
        iv.mount_options("u:1:2:1 g:1:2:1", writable=False),
    )
    fields = line.split(" ")
    assert len(fields) == 6  # no raw whitespace survives inside a field
    assert fields[0] == "/home/my\\040user/agents/a/.data"
    assert fields[1] == "/home/my\\040user/agents-view/a"
    assert fields[2] == "none"
    assert fields[3].startswith("bind,nofail,ro,") and "u:1:2:1\\040g:1:2:1" in fields[3]
    assert fields[4:] == ["0", "0"]
    assert iv.fstab_escape("a\\b\tc") == "a\\134b\\011c"


MOUNTINFO = """\
29 1 253:0 / / rw,relatime shared:1 - ext4 /dev/mapper/vg-lv rw
97 29 0:45 / /run/user/1000 rw,nosuid,nodev shared:500 - tmpfs tmpfs rw,size=1g
410 29 253:0 /home/u/agents/coder/.data /home/u/agents-view/coder ro,nosuid,nodev,noexec,relatime,nosymfollow,idmapped shared:1 - ext4 /dev/mapper/vg-lv rw
411 29 253:0 /home/u/agents/old/.data//deleted /home/u/agents-view/old ro,idmapped shared:1 - ext4 /dev/mapper/vg-lv rw
412 29 253:0 /home/u/my\\040dir /home/u/view\\040x rw shared:1 - ext4 /dev/mapper/vg-lv rw
garbage line without separator
"""


def test_parse_mountinfo_finds_views_roots_flags_and_unescapes() -> None:
    entries = iv.parse_mountinfo(MOUNTINFO)
    assert len(entries) == 5
    view = iv.mount_at(entries, Path("/home/u/agents-view/coder"))
    assert view is not None
    assert view.root == "/home/u/agents/coder/.data"
    assert view.idmapped and not view.deleted and "ro" in view.options
    stale = iv.mount_at(entries, Path("/home/u/agents-view/old"))
    assert stale is not None and stale.deleted
    spaced = iv.mount_at(entries, Path("/home/u/view x"))
    assert spaced is not None and spaced.root == "/home/u/my dir" and not spaced.idmapped
    assert iv.mount_at(entries, Path("/home/u/agents-view")) is None


def test_mount_at_returns_the_topmost_of_stacked_mounts() -> None:
    text = "1 0 8:1 /a /v rw - ext4 /dev/sda1 rw\n2 0 8:1 /b /v ro,idmapped - ext4 /dev/sda1 rw\n"
    top = iv.mount_at(iv.parse_mountinfo(text), Path("/v"))
    assert top is not None and top.root == "/b"


SEPARATE_HOME = """\
29 1 253:0 / / rw - ext4 /dev/root rw
30 29 253:2 / /home rw - ext4 /dev/home rw
410 30 253:2 /u/agents/coder/.data /home/u/agents-view/coder ro,idmapped - ext4 /dev/home rw
"""


def test_expected_root_is_relative_to_the_containing_filesystem() -> None:
    entries = iv.parse_mountinfo(SEPARATE_HOME)
    src = Path("/home/u/agents/coder/.data")
    assert iv.expected_root(entries, src) == "/u/agents/coder/.data"
    assert iv.expected_root(iv.parse_mountinfo(MOUNTINFO), src) == "/home/u/agents/coder/.data"
    assert iv.view_status(entries, src, Path("/home/u/agents-view/coder")) == "mounted"


def test_view_status_distinguishes_missing_stale_and_foreign() -> None:
    entries = iv.parse_mountinfo(MOUNTINFO)
    coder = Path("/home/u/agents/coder/.data")
    assert iv.view_status(entries, coder, Path("/home/u/agents-view/coder")) == "mounted"
    assert iv.view_status(entries, coder, Path("/home/u/agents-view/nope")) == "not-mounted"
    assert (
        iv.view_status(entries, Path("/home/u/agents/old/.data"), Path("/home/u/agents-view/old"))
        == "stale"
    )
    # right mountpoint, wrong source
    assert (
        iv.view_status(
            entries, Path("/home/u/agents/bob/.data"), Path("/home/u/agents-view/coder")
        )
        == "foreign"
    )
    # a plain (non-idmapped) bind mount is not a view
    assert iv.view_status(entries, Path("/home/u/my dir"), Path("/home/u/view x")) == "foreign"


def test_view_status_requires_the_same_filesystem_not_just_the_same_root() -> None:
    """The data dir got replaced/overmounted by another filesystem with
    the same layout: the old view's root string still matches, its device
    doesn't — it must not pass as a view of the new data."""
    text = (
        "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "2 1 8:1 /data/a/.data /views/a ro,idmapped - ext4 /dev/sda1 rw\n"
        "3 1 8:2 / /data rw - ext4 /dev/sdb1 rw\n"
    )
    entries = iv.parse_mountinfo(text)
    assert entries[1].device == "8:1"
    assert iv.view_status(entries, Path("/data/a/.data"), Path("/views/a")) == "foreign"
    before = iv.parse_mountinfo("".join(text.splitlines(keepends=True)[:2]))
    assert iv.view_status(before, Path("/data/a/.data"), Path("/views/a")) == "mounted"
