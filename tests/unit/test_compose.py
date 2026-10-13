"""compose.py: round-trip, service fragments, port allocator, atomic write, flock."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agx_cli import compose
from agx_cli.compose import (
    ALLOWED_IMAGE_BASES,
    CONT_INIT_CONTAINER_PATH,
    CONT_INIT_REL,
    FIRECRAWL_API_IMAGE_BASE,
    FIRECRAWL_PLAYWRIGHT_IMAGE_BASE,
    FIRECRAWL_POSTGRES_IMAGE_BASE,
    FIRECRAWL_RABBITMQ_IMAGE_BASE,
    FIRECRAWL_REDIS_IMAGE_BASE,
    HERMES_IMAGE_BASE,
    LLAMACPP_DOWNLOAD_MOUNT,
    LLAMACPP_IMAGE_BASE,
    OLLAMA_IMAGE_BASE,
    OPENCLAW_IMAGE_BASE,
    OPENVIKING_IMAGE_BASE,
    add_service,
    cleanup_stale_temps,
    dump_compose,
    ensure_cont_init_script,
    hermes_service_fragment,
    llamacpp_service_fragment,
    load_compose,
    next_free_port,
    ollama_service_fragment,
    openclaw_service_fragment,
    override_file_args,
    read_override_doc,
    remove_service,
    rename_service,
    used_ports,
    validate_image_ref,
    write_compose_atomically,
    write_override_atomically,
)
from agx_cli.config import AgentsConfig, DefaultsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agx_cli.errors import UserError
from agx_cli.mp import MpResult


def _ok(stdout: str = "") -> MpResult:
    return MpResult(stdout=stdout, stderr="", returncode=0)


def _fail(stderr: str = "boom") -> MpResult:
    return MpResult(stdout="", stderr=stderr, returncode=1)


@pytest.fixture
def cfg(tmp_path: Path) -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return AgentsConfig(
        paths=PathsConfig(host_data_dir=str(data_dir)),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock",
            ubuntu_uid=1000,
            ubuntu_gid=1000,
        ),
        defaults=DefaultsConfig(
            hermes_image_tag="v1.0.0",
            openclaw_image_tag="0.1.0",
        ),
    )


# ---------- service fragments ----------


def test_hermes_fragment_has_required_mounts(cfg: AgentsConfig) -> None:
    s = hermes_service_fragment(name="coder", cfg=cfg, port=8642, image_tag="v1.0.0")

    assert s["image"] == "nousresearch/hermes-agent:v1.0.0"
    assert s["container_name"] == "hermes-coder"
    assert s["ports"] == ["127.0.0.1:8642:8642"]
    # No `user:` override — rootless docker maps container uid 0 to the
    # rootless host user, which owns the multipass-mounted data dir.
    # Forcing user: 1000:1000 would push the container into a subuid that
    # cannot write to that mount.
    assert "user" not in s

    # Two required mounts: data dir, socket. NO mount of the toolkit
    # Codex auth (a seeded copy would share a single-use OAuth refresh
    # chain with the host copy); provider auth is done inside the
    # container.
    assert "./coder/.data:/opt/data" in s["volumes"]
    assert "/run/user/1000/docker.sock:/run/user/1000/docker.sock" in s["volumes"]
    assert not any("/home/hermes/.codex" in v for v in s["volumes"])

    # The toolkit-managed cont-init.d perms hook is bind-mounted read-only
    # so it re-applies the install-path chown + gateway-logdir pre-create on
    # every boot — the durable fix for rootless-Docker perm drift.
    assert f"./{CONT_INIT_REL}:{CONT_INIT_CONTAINER_PATH}:ro" in s["volumes"]

    # DOCKER_HOST env must be set so the Hermes-bundled docker CLI finds
    # the rootless socket.
    assert s["environment"]["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"

    # HOME is the runtime user's persisted passwd home — never the
    # container overlay, where everything written under ~ by `agx exec`
    # sessions vanished on each recreate (the old /home/hermes pin).
    assert s["environment"]["HOME"] == "/opt/data"
    # PATH mirrors the image's Config.Env, incl. the /opt/hermes/bin
    # wrapper the old pin skipped; the venv stays for pre-env images.
    path = s["environment"]["PATH"].split(":")
    assert path[:2] == ["/opt/hermes/bin", "/opt/hermes/.venv/bin"]

    # HERMES_UID/GID are non-negotiable — the image entrypoint reads them
    # to remap its internal hermes user (default 10000) to host uid 1000
    # before `gosu hermes`. Without these vars the entrypoint skips the
    # remap and the toolkit's --user 1000:1000 exec hits permission-denied
    # on /opt/data (which the entrypoint creates as the un-remapped uid).
    # Interpolated from .env (HERMES_UID/HERMES_GID written there by vm init).
    assert s["environment"]["HERMES_UID"] == "${HERMES_UID}"
    assert s["environment"]["HERMES_GID"] == "${HERMES_GID}"


def test_hermes_fragment_never_binds_zero_address(cfg: AgentsConfig) -> None:
    s = hermes_service_fragment(name="coder", cfg=cfg, port=8642, image_tag="v1.0.0")
    for p in s["ports"]:
        assert p.startswith("127.0.0.1:"), f"insecure binding: {p!r}"


def test_ensure_cont_init_script_writes_executable_hook(cfg: AgentsConfig) -> None:
    # Host-mounted mode (data_dir_in_vm=False): the hook is written straight
    # into the project dir with the exec bit set, so the read-only bind mount
    # resolves to an executable file s6-overlay will actually run.
    ensure_cont_init_script(cfg)

    target = cfg.paths.host_data_dir / CONT_INIT_REL
    assert target.is_file()
    assert os.access(target, os.X_OK)
    assert (target.stat().st_mode & 0o777) == 0o755

    body = target.read_text(encoding="utf-8")
    # Split-privilege contract: chown the overlay-fs install paths AS ROOT,
    # but pre-create the 9p-backed gateway logdir AS hermes (root can't write
    # the 9p mount). Both are load-bearing — see the script's own comments.
    # The overlay chown targets the NUMERIC uid/gid (resolved via `id`), not
    # the name `hermes:hermes` — a name target would silently no-op if the
    # group were renamed, with the trailing `exit 0` hiding it.
    assert "id -u hermes" in body and "id -g hermes" in body
    assert 'chown "$HUID:$HGID"' in body
    assert "/opt/hermes/scripts" in body
    assert "/command/s6-setuidgid hermes mkdir -p /opt/data/logs/gateways" in body
    # A non-zero cont-init.d exit can abort container init (depending on
    # S6_BEHAVIOUR_IF_STAGE2_FAILS), so the hook must always end by succeeding.
    assert body.rstrip().endswith("exit 0")


def test_cont_init_script_precreates_profile_gateway_log_dirs(cfg: AgentsConfig) -> None:
    # GH #6 tier 1: beyond the default gateway's log dir, the hook globs
    # /opt/data/profiles/*/ and pre-creates each profile's leaf under
    # /opt/data/logs/gateways/ AS hermes — a restored/imported data dir
    # arrives without it, the image's log `run` script can only mkdir it
    # as root (which the 9p/virtiofs mount refuses), and s6-log is not
    # verified to create a missing leaf itself.
    ensure_cont_init_script(cfg)
    body = (cfg.paths.host_data_dir / CONT_INIT_REL).read_text(encoding="utf-8")
    assert "/opt/data/profiles/*/" in body
    assert '"/opt/data/logs/gateways/$(basename "$_profile_dir")"' in body
    # The per-profile mkdir must run AS hermes too (split-privilege contract).
    profile_block = body.split("/opt/data/profiles/*/", 1)[1]
    assert "/command/s6-setuidgid hermes mkdir -p" in profile_block


def test_cont_init_script_is_valid_posix_sh(cfg: AgentsConfig) -> None:
    # The hook is a shell template assembled in Python — a quoting slip
    # would only surface as a container boot failure. `sh -n` parses
    # without executing.
    ensure_cont_init_script(cfg)
    proc = subprocess.run(
        ["sh", "-n", str(cfg.paths.host_data_dir / CONT_INIT_REL)],
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()


def test_ensure_cont_init_script_is_idempotent(cfg: AgentsConfig) -> None:
    ensure_cont_init_script(cfg)
    first = (cfg.paths.host_data_dir / CONT_INIT_REL).read_text(encoding="utf-8")
    ensure_cont_init_script(cfg)
    second = (cfg.paths.host_data_dir / CONT_INIT_REL).read_text(encoding="utf-8")
    assert first == second
    # No stray staging temps left behind by the atomic write.
    assert not list((cfg.paths.host_data_dir / ".staging").glob("continit-*"))


def test_ensure_cont_init_script_repairs_docker_created_directory(cfg: AgentsConfig) -> None:
    # If a `compose up` raced ahead of materialization, Docker creates an
    # (empty) directory at the bind-mount source. ensure_cont_init_script must
    # remove it and land a real executable file rather than raise EISDIR.
    target = cfg.paths.host_data_dir / CONT_INIT_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    assert target.is_dir()

    ensure_cont_init_script(cfg)

    assert target.is_file()
    assert os.access(target, os.X_OK)


def test_ensure_cont_init_script_vm_mode_is_atomic(cfg: AgentsConfig, mocker) -> None:
    # --data-dir-in-vm: transfer to a VM-side temp, chmod the temp, then `mv`
    # into place — never transfer straight to the final path (a crash mid-way
    # would otherwise leave a partial/non-executable hook the container mounts).
    cfg.paths.data_dir_in_vm = True
    vm_path = f"{cfg.paths.vm_agents_root}/{CONT_INIT_REL}"
    calls: list[tuple[str, tuple[str, ...]]] = []

    def rec_exec(_vm: str, *args: str, **_kw: object) -> MpResult:
        calls.append(("exec", args))
        return MpResult(stdout="", stderr="", returncode=0)

    def rec_transfer(_host_path: Path, _vm: str, vm_dst: str) -> MpResult:
        calls.append(("transfer", (vm_dst,)))
        return MpResult(stdout="", stderr="", returncode=0)

    mocker.patch("agx_cli.mp.exec_in_vm", side_effect=rec_exec)
    mocker.patch("agx_cli.mp.transfer_to_vm", side_effect=rec_transfer)

    ensure_cont_init_script(cfg)

    transfers = [a[0] for kind, a in calls if kind == "transfer"]
    assert transfers, "expected a transfer call"
    # Transferred to a temp under the final path, never the final path itself.
    assert all(dst != vm_path and dst.startswith(vm_path + ".") for dst in transfers)
    vm_tmp = transfers[0]

    exec_args = [a for kind, a in calls if kind == "exec"]
    chmod_idx = next(i for i, a in enumerate(exec_args) if a[0] == "chmod")
    mv_idx = next(i for i, a in enumerate(exec_args) if a[0] == "mv")
    # chmod targets the temp; mv moves temp -> final; chmod happens before mv.
    assert exec_args[chmod_idx][-1] == vm_tmp
    assert exec_args[mv_idx][1] == vm_tmp and exec_args[mv_idx][2] == vm_path
    assert chmod_idx < mv_idx


def test_openclaw_fragment_uses_per_agent_paths(cfg: AgentsConfig) -> None:
    s = openclaw_service_fragment(name="personal", port=18789, image_tag="0.1.0")
    assert s["image"] == "openclaw/openclaw:0.1.0"
    assert s["container_name"] == "openclaw-personal"
    assert "./personal/.openclaw:/home/node/.openclaw" in s["volumes"]
    assert "./personal/workspace:/home/node/.openclaw/workspace" in s["volumes"]
    # No Codex mount: OpenClaw bundles its own.
    for v in s["volumes"]:
        assert ".codex" not in v
    assert s["environment"]["OPENCLAW_DISABLE_BONJOUR"] == "1"


def test_openclaw_fragment_carries_firecrawl_env_interp(cfg: AgentsConfig) -> None:
    """OpenClaw's bundled Firecrawl plugin reads FIRECRAWL_API_KEY +
    FIRECRAWL_BASE_URL from container env. The fragment bridges the
    name asymmetry (Hermes uses FIRECRAWL_API_URL) via compose
    interpolation from the same project .env entry.
    """
    s = openclaw_service_fragment(name="personal", port=18789, image_tag="0.1.0")
    env = s["environment"]
    # OpenClaw-specific spelling, not the Hermes FIRECRAWL_API_URL.
    assert env["FIRECRAWL_API_KEY"] == "${FIRECRAWL_API_KEY:-}"
    assert env["FIRECRAWL_BASE_URL"] == "${FIRECRAWL_API_URL:-}"
    assert "FIRECRAWL_API_URL" not in env


def test_openclaw_named_agents_dont_collide(cfg: AgentsConfig) -> None:
    a = openclaw_service_fragment(name="personal", port=18789, image_tag="0.1.0")
    b = openclaw_service_fragment(name="home", port=18790, image_tag="0.1.0")
    assert set(a["volumes"]).isdisjoint(b["volumes"])


def test_ollama_fragment_binds_llamacpp_download_dir() -> None:
    """The ollama container doubles as the HF download host for
    `agx model pull --engine llama-cpp` — without this second bind
    the multi-GB download lands on the container overlay (invisible to
    `model list`, wiped on recreate).
    """
    s = ollama_service_fragment(image="ollama/ollama@sha256:abc")
    assert "./models/ollama:/root/.ollama" in s["volumes"]
    assert f"./models/llama-cpp:{LLAMACPP_DOWNLOAD_MOUNT}" in s["volumes"]


def test_llamacpp_fragment_reads_same_dir_ollama_downloads_into() -> None:
    """Cross-fragment invariant: the host dir the ollama fragment's
    download bind writes into (./models/llama-cpp) must be the very dir
    the per-model llama-cpp service mounts read-only at /models —
    otherwise `model pull` and `inference load` disagree on where GGUFs
    live.
    """
    s = llamacpp_service_fragment(
        model_slug="llama-3-2-3b",
        image="ghcr.io/ggml-org/llama.cpp@sha256:abc",
        gguf_relative_path="Llama-3.2-3B-Q4_K_M.gguf",
        port=18080,
    )
    assert "./models/llama-cpp:/models:ro" in s["volumes"]
    assert "/models/Llama-3.2-3B-Q4_K_M.gguf" in s["command"]


def test_hermes_socket_path_comes_from_config(tmp_path: Path) -> None:
    cfg = AgentsConfig(
        paths=PathsConfig(host_data_dir=str(tmp_path / "data")),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1234/docker.sock",
            ubuntu_uid=1234,
            ubuntu_gid=1234,
        ),
    )
    s = hermes_service_fragment(name="c", cfg=cfg, port=8642, image_tag="v1")
    assert "/run/user/1234/docker.sock:/run/user/1234/docker.sock" in s["volumes"]
    assert s["environment"]["DOCKER_HOST"] == "unix:///run/user/1234/docker.sock"


# ---------- image allowlist ----------


def test_allowlist_accepts_hermes_and_openclaw_with_any_tag() -> None:
    assert validate_image_ref("nousresearch/hermes-agent:v1.0.0") == HERMES_IMAGE_BASE
    assert validate_image_ref("openclaw/openclaw:0.2.1") == OPENCLAW_IMAGE_BASE


def test_allowlist_rejects_registry_prefixed_lookalikes() -> None:
    for evil in [
        "ghcr.io/evil/nousresearch/hermes-agent:v1",
        "evil-registry.io/nousresearch/hermes-agent",
        "hermes-agent.org/hermes-agent:latest",
    ]:
        with pytest.raises(UserError, match="allowlist"):
            validate_image_ref(evil)


def test_allowlist_rejects_unknown_repo() -> None:
    with pytest.raises(UserError, match="allowlist"):
        validate_image_ref("random/image:tag")


def test_allowlist_set_is_what_we_advertise() -> None:
    assert (
        frozenset(
            {
                HERMES_IMAGE_BASE,
                OPENCLAW_IMAGE_BASE,
                OPENVIKING_IMAGE_BASE,
                FIRECRAWL_API_IMAGE_BASE,
                FIRECRAWL_PLAYWRIGHT_IMAGE_BASE,
                FIRECRAWL_POSTGRES_IMAGE_BASE,
                FIRECRAWL_REDIS_IMAGE_BASE,
                FIRECRAWL_RABBITMQ_IMAGE_BASE,
                OLLAMA_IMAGE_BASE,
                LLAMACPP_IMAGE_BASE,
            }
        )
        == ALLOWED_IMAGE_BASES
    )


def test_validate_image_ref_handles_digest() -> None:
    """Firecrawl pins by digest (``image@sha256:…``); the allowlist
    check must not be confused by the colon inside the digest.
    """
    digest = f"{FIRECRAWL_API_IMAGE_BASE}@sha256:abc123def456"
    assert validate_image_ref(digest) == FIRECRAWL_API_IMAGE_BASE


def test_validate_image_ref_rejects_digest_on_unknown_repo() -> None:
    with pytest.raises(UserError, match="allowlist"):
        validate_image_ref("evil/typosquat@sha256:abc")


# ---------- service ops ----------


def test_add_service_inserts_into_services(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "hermes-coder", {"image": "x:y"})
    assert "hermes-coder" in doc["services"]


def test_add_service_refuses_duplicate(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "hermes-coder", {"image": "x:y"})
    with pytest.raises(UserError, match="already exists"):
        add_service(doc, "hermes-coder", {"image": "x:z"})


def test_remove_service_drops_entry(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "x", {"image": "i"})
    remove_service(doc, "x")
    assert "x" not in (doc.get("services") or {})


def test_remove_service_refuses_unknown(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    with pytest.raises(UserError, match="not in compose"):
        remove_service(doc, "nope")


def test_rename_service_atomic(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "old", {"image": "i"})
    rename_service(doc, "old", "new")
    assert "new" in doc["services"]
    assert "old" not in doc["services"]


def test_rename_service_refuses_existing_target(cfg: AgentsConfig) -> None:
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "a", {"image": "i"})
    add_service(doc, "b", {"image": "i"})
    with pytest.raises(UserError, match="already exists"):
        rename_service(doc, "a", "b")


# ---------- round-trip preserves comments ----------


def test_round_trip_preserves_comments(tmp_path: Path) -> None:
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(
        "# top-level comment\n"
        "services:\n"
        "  hermes-coder:\n"
        "    # service-level comment\n"
        "    image: nousresearch/hermes-agent:v1.0.0\n",
        encoding="utf-8",
    )
    doc = load_compose(compose_path)
    dumped = dump_compose(doc)
    assert "top-level comment" in dumped
    assert "service-level comment" in dumped


# ---------- atomic write ----------


def test_write_compose_atomically_succeeds(cfg: AgentsConfig, mocker) -> None:
    from tests.conftest import install_vm_fs_simulation

    install_vm_fs_simulation(mocker, cfg)
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "hermes-coder", {"image": "x:y"})
    write_compose_atomically(cfg, doc)
    final = (cfg.paths.host_data_dir / "docker-compose.yml").read_text()
    assert "hermes-coder" in final
    # No leftover temp files (VM staging mapped to host via mount-sim)
    assert not any(cfg.paths.host_data_dir.glob(".compose.*.new"))
    # No host temp left in /tmp
    assert not any(Path("/tmp").glob(f"agents-compose-{__import__('os').getpid()}-*.yml"))


def test_write_compose_atomically_validation_failure_keeps_old_file(
    cfg: AgentsConfig, mocker
) -> None:
    from tests.conftest import install_vm_fs_simulation

    # Seed an existing valid compose
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        "services:\n  existing: {image: i}\n", encoding="utf-8"
    )
    install_vm_fs_simulation(mocker, cfg)
    # Validation step (docker compose ... config -q) gets a failing rc. We
    # override the `in_vm_runner` for that one call by routing every exec
    # through a stub that returns failure when "config" is in the cmd.
    original = mocker.patch("agx_cli.mp.exec_in_vm")

    def selective(vm_name: str, *cmd: str, check: bool = True):
        joined = " ".join(cmd)
        if "config" in joined and "-q" in joined:
            return _fail("yaml error")
        # mv operation still needs to execute the host-side rename
        if cmd and cmd[0] == "mv" and len(cmd) >= 3:
            import os

            src = Path(cmd[1])
            dst = Path(cmd[2])
            # Map VM paths to host
            vm_root = str(cfg.paths.vm_agents_root)
            if str(src).startswith(vm_root):
                src = cfg.paths.host_data_dir / src.relative_to(vm_root)
            if str(dst).startswith(vm_root):
                dst = cfg.paths.host_data_dir / dst.relative_to(vm_root)
            os.replace(src, dst)
            return _ok()
        if cmd and cmd[0] == "rm":
            for arg in cmd[1:]:
                if arg.startswith("-"):
                    continue
                p = Path(arg)
                vm_root = str(cfg.paths.vm_agents_root)
                if str(p).startswith(vm_root):
                    p = cfg.paths.host_data_dir / p.relative_to(vm_root)
                p.unlink(missing_ok=True)
            return _ok()
        return _ok()

    original.side_effect = selective

    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "broken", {"image": "x:y"})
    with pytest.raises(UserError, match="compose file invalid"):
        write_compose_atomically(cfg, doc)
    # Old file untouched, temp cleaned up
    assert "existing" in (cfg.paths.host_data_dir / "docker-compose.yml").read_text()
    assert "broken" not in (cfg.paths.host_data_dir / "docker-compose.yml").read_text()
    assert not any(cfg.paths.host_data_dir.glob(".compose.*.new"))


def test_write_compose_atomically_validation_includes_override(cfg: AgentsConfig, mocker) -> None:
    """A broken override must be caught at write time: the ``docker
    compose config -q`` validation has to see the same merged view the
    runtime verbs use — staged main compose first, then the override.
    """
    from tests.conftest import install_vm_fs_simulation

    (cfg.paths.host_data_dir / "docker-compose.override.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    mocks = install_vm_fs_simulation(mocker, cfg)
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "hermes-coder", {"image": "x:y"})
    write_compose_atomically(cfg, doc)

    config_calls = [
        c.args for c in mocks["exec_in_vm"].call_args_list if "config" in c.args and "-q" in c.args
    ]
    assert config_calls, "expected a `compose config -q` validation call"
    argv = list(config_calls[0])
    f_idx = [i for i, a in enumerate(argv) if a == "-f"]
    assert len(f_idx) == 2
    assert argv[f_idx[0] + 1].startswith(f"{cfg.paths.vm_agents_root}/.compose.")
    assert argv[f_idx[1] + 1] == f"{cfg.paths.vm_agents_root}/docker-compose.override.yml"


# ---------- override file ----------


def test_override_file_args_present_iff_file_exists(cfg: AgentsConfig) -> None:
    assert override_file_args(cfg) == []
    (cfg.paths.host_data_dir / "docker-compose.override.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    assert override_file_args(cfg) == [
        "-f",
        f"{cfg.paths.vm_agents_root}/docker-compose.override.yml",
    ]


def test_write_override_atomically_host_mode_needs_no_transfer(cfg: AgentsConfig, mocker) -> None:
    """Host-mount mode: the host data dir IS the VM project dir (via the
    mount), so the host-side write is the whole job — no transfer."""
    transfer = mocker.patch("agx_cli.mp.transfer_to_vm")
    doc = read_override_doc(cfg)
    doc["services"]["hermes-a"] = {"volumes": ["/x:/y:ro"]}
    write_override_atomically(cfg, doc)
    transfer.assert_not_called()
    content = (cfg.paths.host_data_dir / "docker-compose.override.yml").read_text(encoding="utf-8")
    assert "hermes-a" in content
    # No staging residue.
    assert not list((cfg.paths.host_data_dir / ".staging").glob("override-*"))


def test_write_override_atomically_mirrors_into_vm_in_data_dir_in_vm_mode(
    cfg: AgentsConfig, mocker
) -> None:
    """--data-dir-in-vm: the VM project dir is a separate filesystem, so
    the override must be transferred in (stage → atomic mv) or the
    VM-side ``docker compose`` can't see it — every verb would fail on
    the ``-f`` pointing at a missing file."""
    from tests.conftest import install_vm_fs_simulation

    cfg.paths.data_dir_in_vm = True
    mocks = install_vm_fs_simulation(mocker, cfg)
    doc = read_override_doc(cfg)
    doc["services"]["hermes-a"] = {"volumes": ["/x:/y:ro"]}
    write_override_atomically(cfg, doc)

    # Host-side authoritative copy written (drives override_file_args).
    assert (cfg.paths.host_data_dir / "docker-compose.override.yml").exists()
    # Transferred to a VM-side stage, never straight to the final path...
    transfer_dsts = [c.args[2] for c in mocks["transfer_to_vm"].call_args_list]
    assert transfer_dsts, "expected a transfer into the VM"
    assert all(dst.startswith(f"{cfg.paths.vm_agents_root}/.compose.") for dst in transfer_dsts)
    # ...then atomically mv'd into place.
    mv_calls = [c.args for c in mocks["exec_in_vm"].call_args_list if c.args[1] == "mv"]
    assert mv_calls, "expected a VM-side mv"
    assert mv_calls[-1][2] == transfer_dsts[-1]
    assert mv_calls[-1][3] == f"{cfg.paths.vm_agents_root}/docker-compose.override.yml"


def test_write_supports_data_dir_in_vm_mode(cfg: AgentsConfig, mocker) -> None:
    """Under --data-dir-in-vm the mutation pattern is identical; only
    the read side differs. The simulator treats both modes the same."""
    from tests.conftest import install_vm_fs_simulation

    cfg.paths.data_dir_in_vm = True
    install_vm_fs_simulation(mocker, cfg)
    doc = load_compose(cfg.paths.host_data_dir / "docker-compose.yml")
    add_service(doc, "hermes-coder", {"image": "x:y"})
    write_compose_atomically(cfg, doc)  # no UserError


def test_write_refuses_when_vm_runtime_unpopulated(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = AgentsConfig(paths=PathsConfig(host_data_dir=str(data_dir)))
    doc = load_compose(data_dir / "docker-compose.yml")
    with pytest.raises(UserError, match="vm init"):
        write_compose_atomically(cfg, doc)


# ---------- temp cleanup ----------


def test_cleanup_stale_temps_removes_residue(cfg: AgentsConfig, mocker) -> None:
    from agx_cli.compose import host_staging_dir
    from tests.conftest import install_vm_fs_simulation

    d = cfg.paths.host_data_dir
    install_vm_fs_simulation(mocker, cfg)
    (d / ".compose.1234.abc.new").write_text("junk", encoding="utf-8")
    (d / ".compose.5678.def.new").write_text("junk", encoding="utf-8")
    (d / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    # Also leave a host-side staging-dir straggler.
    host_temp = host_staging_dir(cfg) / "stale.yml"
    host_temp.write_text("stale", encoding="utf-8")
    cleanup_stale_temps(cfg)
    assert not any(d.glob(".compose.*.new"))
    # Real compose untouched
    assert (d / "docker-compose.yml").exists()
    # Host staging-dir straggler was swept
    assert not host_temp.exists()


# ---------- port allocator ----------


def test_used_ports_parses_compose_config_json(cfg: AgentsConfig, mocker) -> None:
    payload = json.dumps(
        {
            "services": {
                "hermes-coder": {
                    "ports": [{"target": 8642, "published": "8642"}],
                },
                "openclaw": {
                    "ports": [{"target": 18789, "published": 18789}],
                },
            }
        }
    )
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=_ok(payload))
    assert used_ports(cfg) == {8642, 18789}


def test_used_ports_passes_override_flag_iff_present(cfg: AgentsConfig, mocker) -> None:
    """The rendered-config invocation must merge the override — a port
    published only via docker-compose.override.yml would otherwise be
    invisible to the allocator."""
    runner = mocker.patch("agx_cli.mp.exec_in_vm", return_value=_ok(json.dumps({"services": {}})))
    used_ports(cfg)
    argv = list(runner.call_args.args[1:])
    assert argv.count("-f") == 1

    (cfg.paths.host_data_dir / "docker-compose.override.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    used_ports(cfg)
    argv = list(runner.call_args.args[1:])
    f_idx = [i for i, a in enumerate(argv) if a == "-f"]
    assert len(f_idx) == 2
    assert argv[f_idx[0] + 1].endswith("/docker-compose.yml")
    assert argv[f_idx[1] + 1].endswith("/docker-compose.override.yml")


def test_used_ports_falls_back_to_doc_parse_on_compose_failure(cfg: AgentsConfig, mocker) -> None:
    """`docker compose config` failing (VM down, broken .env) must NOT
    read as "no ports in use" — that fail-open would let the allocator
    hand a new agent an already-published port. Fall back to parsing the
    compose docs (main + override) directly, with a warning."""
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        "services:\n  hermes-a:\n    ports:\n      - 127.0.0.1:8642:8642\n",
        encoding="utf-8",
    )
    (cfg.paths.host_data_dir / "docker-compose.override.yml").write_text(
        "services:\n  extra:\n    ports:\n      - 127.0.0.1:9100:9100\n",
        encoding="utf-8",
    )
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=_fail())
    warn = mocker.patch("agx_cli.ui.warn")
    assert used_ports(cfg) == {8642, 9100}
    warn.assert_called_once()


def test_used_ports_falls_back_to_doc_parse_on_bad_json(cfg: AgentsConfig, mocker) -> None:
    """Same fallback for unparseable stdout; also covers the long dict
    form of a ports entry in the raw doc."""
    (cfg.paths.host_data_dir / "docker-compose.yml").write_text(
        "services:\n  hermes-a:\n    ports:\n      - target: 8642\n        published: 8643\n",
        encoding="utf-8",
    )
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=_ok("not json"))
    warn = mocker.patch("agx_cli.ui.warn")
    assert used_ports(cfg) == {8643}
    warn.assert_called_once()


def test_used_ports_empty_project_still_returns_empty_on_failure(cfg: AgentsConfig, mocker) -> None:
    """A project with genuinely no ports (no compose file yet) may return
    the empty set even when the runner fails — but the degraded path is
    still surfaced via a warning."""
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=_fail())
    warn = mocker.patch("agx_cli.ui.warn")
    assert used_ports(cfg) == set()
    warn.assert_called_once()


def test_next_free_port_returns_base_when_unused(cfg: AgentsConfig) -> None:
    assert next_free_port(cfg, base=8642, used=set()) == 8642


def test_next_free_port_skips_used(cfg: AgentsConfig) -> None:
    assert next_free_port(cfg, base=8642, used={8642, 8643}) == 8644


def test_next_free_port_finds_gap(cfg: AgentsConfig) -> None:
    # Plan implies sequential allocation; gaps in the middle are not skipped
    # specially — we just pick the first free port at or above base.
    assert next_free_port(cfg, base=8642, used={8642, 8644}) == 8643


# flock tests moved to test_lock.py


def test_await_vm_view_polls_until_vm_hash_matches(cfg, mocker) -> None:
    import hashlib

    target = cfg.paths.host_data_dir / "docker-compose.override.yml"
    target.write_text("services: {}\n", encoding="utf-8")
    want = hashlib.sha256(target.read_bytes()).hexdigest()
    stale = MpResult(stdout="0" * 64 + "  x\n", stderr="", returncode=0)
    fresh = MpResult(stdout=f"{want}  x\n", stderr="", returncode=0)
    run = mocker.patch("agx_cli.mp.exec_in_vm", side_effect=[stale, stale, fresh])
    mocker.patch("agx_cli.compose.time.sleep")
    compose._await_vm_view_impl(cfg, target)
    assert run.call_count == 3
    assert run.call_args.args[1:] == (
        "sha256sum",
        f"{cfg.paths.vm_agents_root}/docker-compose.override.yml",
    )


def test_await_vm_view_gives_up_with_warning(cfg, mocker) -> None:
    target = cfg.paths.host_data_dir / ".env"
    target.write_text("A=1\n", encoding="utf-8")
    mocker.patch("agx_cli.mp.exec_in_vm", return_value=MpResult("", "gone", 1))
    mocker.patch("agx_cli.compose.time.sleep")
    clock = iter([0.0, 1.0, 99.0])
    mocker.patch("agx_cli.compose.time.monotonic", side_effect=lambda: next(clock))
    warn = mocker.patch("agx_cli.ui.warn")
    compose._await_vm_view_impl(cfg, target)  # must not raise
    assert "outdated" in warn.call_args.args[0]


def test_await_vm_view_noop_on_bare_metal_and_data_dir_in_vm(cfg, mocker) -> None:
    target = cfg.paths.host_data_dir / ".env"
    target.write_text("A=1\n", encoding="utf-8")
    run = mocker.patch("agx_cli.mp.exec_in_vm")
    cfg.vm.backend = "bare_metal"
    compose._await_vm_view_impl(cfg, target)
    cfg.vm.backend = "multipass"
    cfg.paths.data_dir_in_vm = True
    compose._await_vm_view_impl(cfg, target)
    run.assert_not_called()
