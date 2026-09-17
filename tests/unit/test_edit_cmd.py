"""edit_cmd.py: `agents edit` — container-routed edits of Hermes text files."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agents_cli import config as cfg_mod
from agents_cli import edit_cmd, hermes_config
from agents_cli.agents_cmd import create
from agents_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agents_cli.errors import UserError
from agents_cli.mp import MpResult


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


def _fail(stderr: str, rc: int = 1) -> MpResult:
    return MpResult(stdout="", stderr=stderr, returncode=rc)


class FakeContainer:
    """Just enough of a container filesystem for the coreutils calls
    ``edit_cmd`` makes through ``docker compose exec``."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/opt/data"}
        self.links: dict[str, str] = {}
        self.modes: dict[str, str] = {}
        self.running = True
        self.calls: list[tuple[str, ...]] = []
        self.on_fetch = None  # hook(path) fired on every base64 read

    def _real(self, path: str) -> str | None:
        path = self.links.get(path, path)
        return path if path in self.files or path in self.dirs else None

    def run(self, inner: tuple[str, ...], stdin: bytes | None = None) -> MpResult:
        self.calls.append(inner)
        if not self.running:
            return _fail('service "hermes-coder" is not running')
        args = [a for a in inner[1:] if a != "--"]
        match inner[0]:
            case "realpath":
                real = self._real(args[-1])
                return _ok(real + "\n") if real else _fail("No such file or directory")
            case "stat":
                path = args[-1]
                if path in self.dirs:
                    return _ok("directory|4096\n")
                return _ok(f"regular file|{len(self.files[path])}\n")
            case "base64":
                path = args[-1]
                if self.on_fetch:
                    self.on_fetch(path)
                return _ok(base64.b64encode(self.files[path]).decode())
            case "sh":
                assert inner[2] == 'umask 077; cat > "$1"' and stdin is not None
                self.files[inner[4]] = stdin
                return _ok()
            case "chmod":
                ref = next(a for a in args if a.startswith("--reference="))
                self.modes[args[-1]] = self.modes.get(ref.split("=", 1)[1], "644")
                return _ok()
            case "mv":
                src, dst = args[-2], args[-1]
                self.files[dst] = self.files.pop(src)
                self.modes[dst] = self.modes.pop(src, "644")
                return _ok()
            case "rm":
                self.files.pop(args[-1], None)
                return _ok()
        return _ok()


@pytest.fixture
def cfg(tmp_path: Path) -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (data_dir / ".env").write_text("HERMES_UID=1000\nHERMES_GID=1000\n", encoding="utf-8")
    return AgentsConfig(
        paths=PathsConfig(host_data_dir=str(data_dir)),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock", ubuntu_uid=1000, ubuntu_gid=1000
        ),
        defaults=DefaultsConfig(hermes_image_tag="v1.0.0", openclaw_image_tag="0.1.0"),
    )


@pytest.fixture
def env(cfg: AgentsConfig, tmp_path: Path, mocker, monkeypatch):
    """A created hermes agent `coder` (+ openclaw `claw`) whose container
    exec calls are answered by a FakeContainer."""
    from tests.conftest import install_vm_fs_simulation

    mocks = install_vm_fs_simulation(mocker, cfg)
    mocker.patch("agents_cli.compose_runner.up", return_value=_ok())
    config_path = tmp_path / "config.toml"
    cfg_mod.save(cfg, config_path)
    create(name="coder", agent_type="hermes", config_path=config_path)
    create(name="claw", agent_type="openclaw", config_path=config_path)

    box = FakeContainer()
    box.files["/opt/data/SOUL.md"] = b"# Persona\n"
    box.modes["/opt/data/SOUL.md"] = "600"

    def inner_of(cmd: tuple[str, ...]) -> tuple[str, ...] | None:
        if "exec" in cmd and "hermes-coder" in cmd:
            return tuple(cmd[cmd.index("hermes-coder") + 1 :])
        return None

    simulator = mocks["exec_in_vm"].side_effect

    def exec_in_vm(vm, *cmd, check=True):
        inner = inner_of(cmd)
        return box.run(inner) if inner is not None else simulator(vm, *cmd, check=check)

    def exec_with_stdin(vm, *cmd, input_bytes, check=True):
        inner = inner_of(cmd)
        assert inner is not None
        return box.run(inner, stdin=input_bytes)

    mocks["exec_in_vm"].side_effect = exec_in_vm
    mocks["exec_in_vm_with_stdin"].side_effect = exec_with_stdin
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    return box, config_path, tmp_path / "run"


def _editor(mocker, *contents: bytes | None):
    """Fake editor: each invocation writes the next content (None = leave
    the buffer untouched). Returns the list of buffer paths it was given."""
    seen: list[Path] = []
    queue = list(contents)

    def run(path: Path) -> None:
        seen.append(path)
        nxt = queue.pop(0)
        if nxt is not None:
            path.write_bytes(nxt)

    mocker.patch("agents_cli.edit_cmd._run_editor", side_effect=run)
    return seen


def test_edit_round_trips_a_text_file_preserving_mode(env, mocker) -> None:
    box, config_path, run_dir = env
    seen = _editor(mocker, b"# Persona\nbe kind\n")
    edit_cmd.edit(agent="coder", path="SOUL.md", config_path=config_path)
    assert box.files["/opt/data/SOUL.md"] == b"# Persona\nbe kind\n"
    assert box.modes["/opt/data/SOUL.md"] == "600"
    assert not [p for p in box.files if p.endswith(".new")]
    # Buffer: private dir on the runtime tmpfs, named like the target, gone after.
    assert seen[0].name == "SOUL.md" and seen[0].parent.parent == run_dir
    assert list(run_dir.iterdir()) == []


def test_edit_without_changes_writes_nothing(env, mocker) -> None:
    box, config_path, run_dir = env
    _editor(mocker, None)
    edit_cmd.edit(agent="coder", path="SOUL.md", config_path=config_path)
    assert not any(c[0] in ("sh", "mv") for c in box.calls)
    assert list(run_dir.iterdir()) == []


@pytest.mark.parametrize("bad", ["", "/etc/passwd", "../x", "skills/../../x", "a\nb"])
def test_edit_rejects_paths_that_are_not_plain_relative(env, mocker, bad: str) -> None:
    box, config_path, _ = env
    _editor(mocker)
    with pytest.raises(UserError, match="invalid path"):
        edit_cmd.edit(agent="coder", path=bad, config_path=config_path)
    assert box.calls == []


def test_edit_refuses_symlink_escaping_the_data_dir(env, mocker) -> None:
    box, config_path, _ = env
    box.files["/etc/shadow"] = b"root:x"
    box.links["/opt/data/notes.md"] = "/etc/shadow"
    _editor(mocker)
    with pytest.raises(UserError, match="resolves outside"):
        edit_cmd.edit(agent="coder", path="notes.md", config_path=config_path)
    assert not any(c[0] == "base64" for c in box.calls)


def test_edit_refuses_profile_home_escaping_the_data_dir(env, mocker) -> None:
    box, config_path, _ = env
    box.dirs.add("/srv/elsewhere")
    box.links["/opt/data/profiles/work"] = "/srv/elsewhere"
    box.files["/srv/elsewhere/SOUL.md"] = b"x"
    box.links["/opt/data/profiles/work/SOUL.md"] = "/srv/elsewhere/SOUL.md"
    _editor(mocker)
    with pytest.raises(UserError, match="resolves outside /opt/data"):
        edit_cmd.edit(agent="coder", path="SOUL.md", profile="work", config_path=config_path)


def test_edit_missing_file_and_missing_profile(env, mocker) -> None:
    _, config_path, _ = env
    _editor(mocker)
    with pytest.raises(UserError, match="does not exist"):
        edit_cmd.edit(agent="coder", path="nope.md", config_path=config_path)
    with pytest.raises(UserError, match="profile 'ghost' does not exist"):
        edit_cmd.edit(agent="coder", path="SOUL.md", profile="ghost", config_path=config_path)


def test_edit_refuses_binary_oversized_and_non_regular(env, mocker) -> None:
    box, config_path, _ = env
    _editor(mocker)
    box.files["/opt/data/state.db"] = b"SQLite format 3\x00\x01"
    with pytest.raises(UserError, match="binary"):
        edit_cmd.edit(agent="coder", path="state.db", config_path=config_path)
    box.files["/opt/data/latin.txt"] = "café".encode("latin-1")
    with pytest.raises(UserError, match="not valid UTF-8"):
        edit_cmd.edit(agent="coder", path="latin.txt", config_path=config_path)
    box.files["/opt/data/big.log"] = b"x" * (edit_cmd.MAX_BYTES + 1)
    with pytest.raises(UserError, match="1 MiB"):
        edit_cmd.edit(agent="coder", path="big.log", config_path=config_path)
    box.dirs.add("/opt/data/skills")
    with pytest.raises(UserError, match="not a regular file"):
        edit_cmd.edit(agent="coder", path="skills", config_path=config_path)


def test_edit_container_not_running_is_a_user_error(env, mocker) -> None:
    box, config_path, _ = env
    box.running = False
    _editor(mocker)
    with pytest.raises(UserError, match="agents start coder"):
        edit_cmd.edit(agent="coder", path="SOUL.md", config_path=config_path)


def test_edit_refuses_openclaw_and_unknown_agents(env, mocker) -> None:
    _, config_path, _ = env
    _editor(mocker)
    with pytest.raises(UserError, match="Hermes-only"):
        edit_cmd.edit(agent="claw", path="x.md", config_path=config_path)
    with pytest.raises(UserError, match="not found"):
        edit_cmd.edit(agent="ghost", path="x.md", config_path=config_path)


def test_edit_aborts_on_concurrent_change_and_keeps_the_buffer(env, mocker) -> None:
    box, config_path, _ = env
    seen = _editor(mocker, b"mine\n")
    fetches = {"n": 0}

    def on_fetch(path: str) -> None:
        fetches["n"] += 1
        if fetches["n"] == 2:  # the re-read under the lock
            box.files[path] = b"theirs\n"

    box.on_fetch = on_fetch
    with pytest.raises(UserError, match="changed inside"):
        edit_cmd.edit(agent="coder", path="SOUL.md", config_path=config_path)
    assert box.files["/opt/data/SOUL.md"] == b"theirs\n"
    assert seen[0].read_bytes() == b"mine\n"  # edits survive the refusal


def test_edit_config_yaml_goes_through_write_atomically_with_derived_profile(env, mocker) -> None:
    """Addressed from the default home, but it IS profile `work`'s
    config — the validated writer must get that profile, not None."""
    box, config_path, _ = env
    box.dirs |= {"/opt/data/profiles", "/opt/data/profiles/work"}
    box.files["/opt/data/profiles/work/config.yaml"] = b"model:\n  provider: x\n"
    _editor(mocker, b"model:\n  provider: y\n")
    write = mocker.patch("agents_cli.hermes_config.write_atomically")
    edit_cmd.edit(agent="coder", path="profiles/work/config.yaml", config_path=config_path)
    assert write.call_args.kwargs == {"profile": "work"}
    assert write.call_args.args[1] == "coder"
    assert write.call_args.args[2]["model"]["provider"] == "y"
    assert not any(c[0] == "sh" for c in box.calls)  # never the plain writer


def test_edit_config_yaml_via_symlink_is_still_routed_to_the_validated_writer(env, mocker) -> None:
    box, config_path, _ = env
    box.files["/opt/data/config.yaml"] = b"dashboard:\n  address: 127.0.0.1\n"
    box.links["/opt/data/innocent.md"] = "/opt/data/config.yaml"
    _editor(mocker, b"dashboard:\n  address: 0.0.0.0\n")
    mocker.patch("agents_cli.ui.confirm", return_value=False)
    write = mocker.patch("agents_cli.hermes_config.write_atomically")
    with pytest.raises(UserError, match="not changed"):
        edit_cmd.edit(agent="coder", path="innocent.md", config_path=config_path)
    write.assert_not_called()
    assert box.files["/opt/data/config.yaml"] == b"dashboard:\n  address: 127.0.0.1\n"


def test_edit_config_yaml_reopens_editor_after_a_yaml_error(env, mocker) -> None:
    box, config_path, _ = env
    box.files["/opt/data/config.yaml"] = b"a: 1\n"
    seen = _editor(mocker, b"a: [unclosed\n", b"a: 2\n")
    mocker.patch("agents_cli.ui.confirm", return_value=True)
    write = mocker.patch("agents_cli.hermes_config.write_atomically")
    edit_cmd.edit(agent="coder", path="config.yaml", config_path=config_path)
    assert len(seen) == 2
    assert write.call_args.args[2]["a"] == 2
    assert write.call_args.kwargs == {"profile": None}


def test_write_atomically_passes_stage_path_as_positional_not_shell_text(
    cfg: AgentsConfig, mocker
) -> None:
    write = mocker.patch("agents_cli.mp.exec_in_vm_with_stdin", return_value=_ok())
    mocker.patch("agents_cli.mp.exec_in_vm", return_value=_ok())
    hermes_config.write_atomically(cfg, "coder", hermes_config._yaml().load("a: 1\n"))
    argv = write.call_args.args
    i = argv.index("sh")
    assert argv[i : i + 4] == ("sh", "-c", 'cat > "$1"', "sh")
    assert argv[i + 4].startswith("/opt/data/config.yaml.")
