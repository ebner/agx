"""agents_cmd.reseat: regenerate compose service from current fragment."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agx_cli import agents_cmd, compose
from agx_cli import config as cfg_mod
from agx_cli.agents_cmd import create
from agx_cli.config import (
    AgentsConfig,
    DefaultsConfig,
    HostConfig,
    PathsConfig,
    VmRuntimeConfig,
)
from agx_cli.errors import UserError
from agx_cli.mp import MpResult


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


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
            docker_socket_path="/run/user/1000/docker.sock",
            ubuntu_uid=1000,
            ubuntu_gid=1000,
        ),
        defaults=DefaultsConfig(hermes_image_tag="v1.0.0", openclaw_image_tag="0.1.0"),
    )


@pytest.fixture
def saved_config(cfg: AgentsConfig, tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg, p)
    return p


@pytest.fixture
def vm_fs_mocks(cfg: AgentsConfig, mocker: Any) -> dict[str, Any]:
    from tests.conftest import install_vm_fs_simulation

    return install_vm_fs_simulation(mocker, cfg)


def _stub_in_vm(vm_fs_mocks: dict[str, Any], uid_value: str = "1000") -> Any:
    """Chain a `id -u hermes` stub onto the FS simulator's exec_in_vm so
    compose mutations (mv, transfer) keep working in tests.
    """
    sim_runner = vm_fs_mocks["exec_in_vm"].side_effect

    def runner(vm_name: str, *cmd: str, check: bool = True) -> MpResult:
        if "id" in cmd and "-u" in cmd and "hermes" in cmd:
            return _ok(stdout=f"{uid_value}\n")
        return sim_runner(vm_name, *cmd, check=check)

    return runner


@pytest.fixture
def with_coder_stale_fragment(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> Path:
    """Create coder, then manually strip HERMES_UID/HERMES_GID from its
    compose env block to simulate "agent created before those vars were
    added to the toolkit fragment".
    """
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    create(name="coder", agent_type="hermes", config_path=saved_config)

    doc = compose.read_compose_doc(cfg)
    svc = doc["services"]["hermes-coder"]
    env = svc.get("environment") or {}
    env.pop("HERMES_UID", None)
    env.pop("HERMES_GID", None)
    compose.write_compose_atomically(cfg, doc)
    return saved_config


# ---------- happy path ----------


def test_reseat_restores_hermes_uid_in_env(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """Core scenario: a stale agent's compose entry is missing HERMES_UID,
    reseat regenerates the block from the current fragment which DOES
    include HERMES_UID, and the env var reappears.
    """
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    # Pre-condition: stale entry has no HERMES_UID
    doc = compose.read_compose_doc(cfg)
    pre_env = doc["services"]["hermes-coder"]["environment"]
    assert "HERMES_UID" not in pre_env

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    doc = compose.read_compose_doc(cfg)
    post_env = doc["services"]["hermes-coder"]["environment"]
    assert post_env["HERMES_UID"] == "${HERMES_UID}"
    assert post_env["HERMES_GID"] == "${HERMES_GID}"


def test_reseat_preserves_port(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """Reseat must NOT silently change the port allocation. The host port
    from the existing service block is carried through to the new one.
    """
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    pre_doc = compose.read_compose_doc(cfg)
    pre_ports = pre_doc["services"]["hermes-coder"]["ports"]

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    post_doc = compose.read_compose_doc(cfg)
    post_ports = post_doc["services"]["hermes-coder"]["ports"]
    assert pre_ports == post_ports


def test_reseat_preserves_image_tag(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """User may have pinned a specific tag on this agent; reseat must
    keep it.
    """
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    doc = compose.read_compose_doc(cfg)
    doc["services"]["hermes-coder"]["image"] = "nousresearch/hermes-agent:v9.9.9"
    compose.write_compose_atomically(cfg, doc)

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    doc = compose.read_compose_doc(cfg)
    assert doc["services"]["hermes-coder"]["image"] == "nousresearch/hermes-agent:v9.9.9"


def test_reseat_preserves_env_file(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """The secrets command writes ``env_file: ./<name>/.env`` into the
    service entry. Reseat must not drop that — would break every secret.
    """
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    # The new fragment already includes env_file. Set it to a non-default
    # value to be sure reseat preserves user-modified env_file (e.g. multi-file).
    doc = compose.read_compose_doc(cfg)
    doc["services"]["hermes-coder"]["env_file"] = ["./coder/.env", "./shared.env"]
    compose.write_compose_atomically(cfg, doc)

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    doc = compose.read_compose_doc(cfg)
    assert doc["services"]["hermes-coder"]["env_file"] == [
        "./coder/.env",
        "./shared.env",
    ]


def test_reseat_preserves_depends_on(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """OpenViking sibling adds a depends_on entry; reseat must preserve it."""
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    doc = compose.read_compose_doc(cfg)
    doc["services"]["hermes-coder"]["depends_on"] = ["openviking-coder"]
    compose.write_compose_atomically(cfg, doc)

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    doc = compose.read_compose_doc(cfg)
    assert "openviking-coder" in doc["services"]["hermes-coder"]["depends_on"]


def test_reseat_force_recreates(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """The whole point of reseat is to make the new compose entry actually
    take effect — which requires a force-recreate (stop + docker rm + up),
    not just `compose up -d`.
    """
    stop_spy = mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    up_spy = mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    in_vm_spy = mocker.patch("agx_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    assert stop_spy.called
    assert up_spy.called
    rm_calls = [
        c
        for c in in_vm_spy.call_args_list
        if "docker" in c.args and "rm" in c.args and "-f" in c.args
    ]
    assert rm_calls, "expected `docker rm -f` force-remove"


def test_reseat_runs_chown_chmod_for_hermes(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """Hermes reseat must also re-align /opt/data perms via the fixperms
    helpers — adding HERMES_UID to env will only chown newly created
    files, but stale files from before the fragment change are still at
    the old uid.
    """
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    in_vm_spy = mocker.patch("agx_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "10000"))

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    chown_calls = [c for c in in_vm_spy.call_args_list if "chown" in c.args]
    chmod_calls = [c for c in in_vm_spy.call_args_list if "chmod" in c.args]
    assert chown_calls, "expected chown invocation"
    assert chmod_calls, "expected chmod invocation"
    # chown uses the numeric uid we queried, not the toolkit's hardcoded one
    assert "10000:10000" in " ".join(chown_calls[0].args)


# ---------- OpenClaw path ----------


@pytest.fixture
def with_personal_openclaw(
    cfg: AgentsConfig, saved_config: Path, mocker: Any, vm_fs_mocks: dict[str, Any]
) -> Path:
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    create(name="personal", agent_type="openclaw", config_path=saved_config)
    return saved_config


@pytest.fixture
def with_personal_stale_customized(with_personal_openclaw: Path, cfg: AgentsConfig) -> Path:
    """Simulate an OpenClaw agent created before the firecrawl env entries
    landed in the fragment template, that the user ALSO customized: pinned
    an older tag, moved the published port, added a second env_file entry,
    a depends_on addition, and a hand mount in the main compose.
    """
    doc = compose.read_compose_doc(cfg)
    svc = doc["services"]["openclaw-personal"]
    env = svc.get("environment") or {}
    env.pop("FIRECRAWL_API_KEY", None)
    env.pop("FIRECRAWL_BASE_URL", None)
    svc["image"] = "openclaw/openclaw:0.0.9"
    svc["ports"] = ["127.0.0.1:19999:18789"]
    svc["env_file"] = ["./personal/.env", "./shared.env"]
    svc["depends_on"] = ["some-sidecar"]
    svc["volumes"] = [*svc["volumes"], "/home/ubuntu/notes:/home/node/notes:ro"]
    compose.write_compose_atomically(cfg, doc)
    return with_personal_openclaw


def test_reseat_openclaw_preserves_customizations_and_gains_new_keys(
    with_personal_stale_customized: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """The golden OpenClaw scenario: a stale, customized block is rebuilt
    from the current template — gaining the fragment's new keys (the
    firecrawl env interpolation entries) — while every user knob (port,
    pinned tag, extra env_file, depends_on addition, extra volume)
    survives, and stock entries are not duplicated.
    """
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())

    agents_cmd.reseat("personal", yes=True, config_path=with_personal_stale_customized)

    doc = compose.read_compose_doc(cfg)
    svc = doc["services"]["openclaw-personal"]
    # Gains the current fragment's firecrawl interpolation entries.
    assert svc["environment"]["FIRECRAWL_API_KEY"] == "${FIRECRAWL_API_KEY:-}"
    assert svc["environment"]["FIRECRAWL_BASE_URL"] == "${FIRECRAWL_API_URL:-}"
    # Preserves the user knobs.
    assert svc["image"] == "openclaw/openclaw:0.0.9"
    assert svc["ports"] == ["127.0.0.1:19999:18789"]
    assert svc["env_file"] == ["./personal/.env", "./shared.env"]
    assert "some-sidecar" in svc["depends_on"]
    volumes = list(svc["volumes"])
    assert "/home/ubuntu/notes:/home/node/notes:ro" in volumes
    # Stock volumes stay, exactly once each.
    assert "./personal/.openclaw:/home/node/.openclaw" in volumes
    assert "./personal/workspace:/home/node/.openclaw/workspace" in volumes
    # Gained from the 2026-10 template: OpenClaw's auth-profile key dir
    # (.config/openclaw) and agent-installed tools (.local) now persist.
    assert "./personal/.config:/home/node/.config" in volumes
    assert "./personal/.local:/home/node/.local" in volumes
    assert len(volumes) == 5


def test_reseat_openclaw_stock_is_clean_template_rewrite(
    with_personal_openclaw: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """An uncustomized OpenClaw agent reseats to exactly what the current
    template would produce — no leftovers, no additions.
    """
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())

    pre = compose.read_compose_doc(cfg)
    port = agents_cmd._extract_host_port(pre["services"]["openclaw-personal"])

    agents_cmd.reseat("personal", yes=True, config_path=with_personal_openclaw)

    doc = compose.read_compose_doc(cfg)
    assert doc["services"]["openclaw-personal"] == compose.openclaw_service_fragment(
        name="personal", port=port, image_tag="0.1.0"
    )


def test_reseat_openclaw_skips_chown_chmod(
    with_personal_openclaw: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """OpenClaw reseat regenerates compose + force-recreates, but doesn't
    chown /opt/data (that's a Hermes-specific path).
    """
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    in_vm_spy = mocker.patch("agx_cli.mp.exec_in_vm", return_value=_ok())

    agents_cmd.reseat("personal", yes=True, config_path=with_personal_openclaw)

    assert not any("chown" in c.args and "/opt/data" in c.args for c in in_vm_spy.call_args_list)
    assert not any("chmod" in c.args and "/opt/data" in c.args for c in in_vm_spy.call_args_list)


def test_reseat_openclaw_skips_cont_init_hook(
    with_personal_openclaw: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """The s6 cont-init perms hook is Hermes-only: OpenClaw reseat must
    neither materialize the script nor mount it into the container.
    """
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    cont_init_spy = mocker.spy(compose, "ensure_cont_init_script")

    agents_cmd.reseat("personal", yes=True, config_path=with_personal_openclaw)

    assert not cont_init_spy.called
    doc = compose.read_compose_doc(cfg)
    volumes = doc["services"]["openclaw-personal"]["volumes"]
    assert not any("cont-init" in v for v in volumes)


def test_reseat_openclaw_aborts_without_yes(
    with_personal_openclaw: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    """Same confirmation semantics as the Hermes path: no --yes and a
    declined prompt means no compose mutation and no recreate.
    """
    mocker.patch("agx_cli.ui.confirm", return_value=False)
    pre = compose.read_compose_doc(cfg)["services"]["openclaw-personal"]

    with pytest.raises(UserError, match="aborted by user"):
        agents_cmd.reseat("personal", yes=False, config_path=with_personal_openclaw)

    post = compose.read_compose_doc(cfg)["services"]["openclaw-personal"]
    assert pre == post


def test_reseat_openclaw_yes_skips_confirmation(
    with_personal_openclaw: Path, cfg: AgentsConfig, mocker: Any
) -> None:
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    confirm_spy = mocker.patch("agx_cli.ui.confirm", return_value=False)

    agents_cmd.reseat("personal", yes=True, config_path=with_personal_openclaw)

    assert not confirm_spy.called


# ---------- refusal cases ----------


def test_reseat_refuses_unknown_agent(saved_config: Path) -> None:
    with pytest.raises(UserError, match="not in compose"):
        agents_cmd.reseat("ghost", yes=True, config_path=saved_config)


def test_reseat_refuses_without_yes(
    with_coder_stale_fragment: Path,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))
    mocker.patch("agx_cli.ui.confirm", return_value=False)

    with pytest.raises(UserError, match="aborted by user"):
        agents_cmd.reseat("coder", yes=False, config_path=with_coder_stale_fragment)


# ---------- _extract_host_port / _extract_image_tag ----------


def test_extract_host_port_handles_127_format() -> None:
    p = agents_cmd._extract_host_port({"ports": ["127.0.0.1:8642:8642"]})
    assert p == 8642


def test_extract_host_port_handles_plain_format() -> None:
    p = agents_cmd._extract_host_port({"ports": ["8642:8642"]})
    assert p == 8642


def test_extract_host_port_raises_on_malformed() -> None:
    with pytest.raises(UserError, match="could not extract host port"):
        agents_cmd._extract_host_port({"ports": []})


def test_extract_image_tag_works() -> None:
    assert (
        agents_cmd._extract_image_tag({"image": "nousresearch/hermes-agent:v2026.5.7"})
        == "v2026.5.7"
    )


def test_extract_image_tag_refuses_no_colon() -> None:
    with pytest.raises(UserError, match="no parseable image tag"):
        agents_cmd._extract_image_tag({"image": "nousresearch/hermes-agent"})


# ---------- preserve classifier ----------


def test_reseat_preserves_extra_user_volume_hermes(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """A hand-added volume in the main compose survives Hermes reseat too
    (same classifier as OpenClaw); stock volumes are not duplicated.
    """
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    doc = compose.read_compose_doc(cfg)
    svc = doc["services"]["hermes-coder"]
    port = agents_cmd._extract_host_port(svc)
    svc["volumes"] = [*svc["volumes"], "/home/ubuntu/notes:/home/hermes/notes:ro"]
    compose.write_compose_atomically(cfg, doc)

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    doc = compose.read_compose_doc(cfg)
    volumes = list(doc["services"]["hermes-coder"]["volumes"])
    stock_volumes = compose.hermes_service_fragment(
        name="coder", cfg=cfg, port=port, image_tag="v1.0.0"
    )["volumes"]
    assert volumes == [*stock_volumes, "/home/ubuntu/notes:/home/hermes/notes:ro"]


def test_reseat_drops_retired_codex_import_bridge_but_keeps_user_volumes(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """Agents created before 2026-09 carry the toolkit's own
    ``./.codex:/home/hermes/.codex:ro`` import bridge. The fresh template
    no longer emits it, so the carry-over diff would classify it as a
    user addition and preserve it on every reseat — reseat must drop it
    explicitly (it is what tied the agent's Codex credential to the
    host copy's single-use refresh chain) while still keeping genuine
    user volumes.
    """
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    doc = compose.read_compose_doc(cfg)
    svc = doc["services"]["hermes-coder"]
    port = agents_cmd._extract_host_port(svc)
    svc["volumes"] = [
        *svc["volumes"],
        "./.codex:/home/hermes/.codex:ro",
        "/home/ubuntu/notes:/home/hermes/notes:ro",
    ]
    compose.write_compose_atomically(cfg, doc)

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    doc = compose.read_compose_doc(cfg)
    volumes = list(doc["services"]["hermes-coder"]["volumes"])
    stock_volumes = compose.hermes_service_fragment(
        name="coder", cfg=cfg, port=port, image_tag="v1.0.0"
    )["volumes"]
    assert volumes == [*stock_volumes, "/home/ubuntu/notes:/home/hermes/notes:ro"]
    assert not any(compose.LEGACY_CODEX_BRIDGE_TARGET in v for v in volumes)


def test_carry_user_additions_supersedes_reshaped_stock_volume() -> None:
    """A template-era variant of a stock volume (same container target,
    different source) is superseded by the fresh stock entry — never
    duplicated, since docker refuses duplicate mount targets. The user's
    own mounts (targets the stock fragment doesn't claim) survive.
    """
    new_fragment: dict[str, object] = {
        "volumes": ["./coder/.data:/opt/data", "./.toolkit/x:/etc/cont-init.d/99:ro"]
    }
    existing: dict[str, object] = {
        "volumes": [
            "./coder/old-data:/opt/data",  # reshaped stock entry: stale form loses
            "/home/ubuntu/notes:/notes:ro",  # user addition: preserved
        ]
    }

    agents_cmd._carry_user_additions(new_fragment, existing)

    assert new_fragment["volumes"] == [
        "./coder/.data:/opt/data",
        "./.toolkit/x:/etc/cont-init.d/99:ro",
        "/home/ubuntu/notes:/notes:ro",
    ]


def test_carry_user_additions_dedups_ports_by_host_port() -> None:
    """A bind-address drift of the stock port entry doesn't produce a
    second binding on the same host port; genuinely extra published
    ports survive.
    """
    new_fragment: dict[str, object] = {"ports": ["127.0.0.1:8642:8642"]}
    existing: dict[str, object] = {"ports": ["0.0.0.0:8642:8642", "127.0.0.1:9000:9000"]}

    agents_cmd._carry_user_additions(new_fragment, existing)

    assert new_fragment["ports"] == ["127.0.0.1:8642:8642", "127.0.0.1:9000:9000"]


def test_carry_user_additions_keeps_udp_twin_of_stock_tcp_port() -> None:
    """Port identity is (host port, protocol): TCP and UDP can publish
    the same numeric host port simultaneously, so a user's UDP twin of
    the stock TCP port is an addition and survives. An explicit ``/tcp``
    variant of the stock entry still dedupes to the stock form.
    """
    new_fragment: dict[str, object] = {"ports": ["127.0.0.1:8642:8642"]}
    existing: dict[str, object] = {
        "ports": [
            "0.0.0.0:8642:8642/tcp",  # same protocol as stock: stock wins
            "127.0.0.1:8642:9000/udp",  # UDP twin: preserved
        ]
    }

    agents_cmd._carry_user_additions(new_fragment, existing)

    assert new_fragment["ports"] == ["127.0.0.1:8642:8642", "127.0.0.1:8642:9000/udp"]


def test_reseat_preserves_udp_twin_of_stock_port(
    with_coder_stale_fragment: Path,
    cfg: AgentsConfig,
    mocker: Any,
    vm_fs_mocks: dict[str, Any],
) -> None:
    """Full-flow regression: an existing block carrying both the stock
    TCP publication and a user UDP publication on the same host port
    keeps both after reseat, each exactly once.
    """
    mocker.patch("agx_cli.compose_runner.stop", return_value=_ok())
    mocker.patch("agx_cli.compose_runner.up", return_value=_ok())
    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=_stub_in_vm(vm_fs_mocks, "1000"))

    doc = compose.read_compose_doc(cfg)
    svc = doc["services"]["hermes-coder"]
    port = agents_cmd._extract_host_port(svc)
    udp_entry = f"127.0.0.1:{port}:9000/udp"
    svc["ports"] = [*svc["ports"], udp_entry]
    compose.write_compose_atomically(cfg, doc)

    agents_cmd.reseat("coder", yes=True, config_path=with_coder_stale_fragment)

    doc = compose.read_compose_doc(cfg)
    assert list(doc["services"]["hermes-coder"]["ports"]) == [
        f"127.0.0.1:{port}:8642",
        udp_entry,
    ]


def test_carry_user_additions_preserves_unrecognized_shapes_wholesale() -> None:
    """Shapes the classifier can't additively diff (dict-form depends_on,
    string-form env_file) are preserved untouched — when in doubt,
    preserve.
    """
    new_fragment: dict[str, object] = {"env_file": ["./coder/.env"]}
    existing: dict[str, object] = {
        "depends_on": {"openviking-coder": {"condition": "service_started"}},
        "env_file": "./custom.env",
    }

    agents_cmd._carry_user_additions(new_fragment, existing)

    assert new_fragment["depends_on"] == {"openviking-coder": {"condition": "service_started"}}
    assert new_fragment["env_file"] == "./custom.env"
