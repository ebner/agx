"""Unit tests for `agx doctor-cron` — host cron that runs doctor and
mails on state change."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agx_cli import config as cfg_mod
from agx_cli import doctor_cron
from agx_cli.config import AgentsConfig, HostConfig, PathsConfig, VmRuntimeConfig
from agx_cli.errors import RuntimeFailure, UserError
from agx_cli.ops import DoctorReport


@pytest.fixture
def cfg(tmp_path: Path) -> AgentsConfig:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return AgentsConfig(
        paths=PathsConfig(host_data_dir=str(data_dir)),
        host=HostConfig(uid=1000, gid=1000),
        vm_runtime=VmRuntimeConfig(
            docker_socket_path="/run/user/1000/docker.sock", ubuntu_uid=1000, ubuntu_gid=1000
        ),
    )


@pytest.fixture
def saved_config(cfg: AgentsConfig, tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    cfg_mod.save(cfg, p)
    return p


class FakeCrontab:
    def __init__(self) -> None:
        self.body = ""


@pytest.fixture
def fake_crontab(mocker) -> FakeCrontab:
    ct = FakeCrontab()
    mocker.patch("agx_cli.export_cmd._read_crontab", side_effect=lambda: ct.body)

    def _write(content: str) -> None:
        ct.body = content

    mocker.patch("agx_cli.export_cmd._write_crontab", side_effect=_write)
    return ct


@pytest.fixture
def sendmail(mocker):
    mocker.patch("agx_cli.doctor_cron._sendmail_path", return_value="/usr/sbin/sendmail")
    proc = mocker.MagicMock(returncode=0, stderr=b"")
    return mocker.patch("agx_cli.doctor_cron.subprocess.run", return_value=proc)


def _report(**checks: str) -> DoctorReport:
    r = DoctorReport()
    for name, status in checks.items():
        r.add(name, status, f"detail of {name}")
    return r


# ---------- install / uninstall / status ----------


def test_install_writes_marker_line_and_config(
    saved_config: Path, fake_crontab: FakeCrontab, sendmail, mocker
) -> None:
    mocker.patch("agx_cli.ui.emit")
    doctor_cron.install(
        every_minutes=10, mail_to=["ops@example.org"], binary="/x/agx", config_path=saved_config
    )
    assert doctor_cron.CRON_MARKER in fake_crontab.body
    assert "*/10 * * * * /x/agx --yes doctor-cron run" in fake_crontab.body
    reloaded = cfg_mod.load(saved_config)
    assert reloaded.doctor_cron.enabled is True
    assert reloaded.doctor_cron.every_minutes == 10
    assert reloaded.doctor_cron.mail_to == ["ops@example.org"]


def test_install_is_idempotent_and_keeps_foreign_lines(
    saved_config: Path, fake_crontab: FakeCrontab, sendmail, mocker
) -> None:
    mocker.patch("agx_cli.ui.emit")
    fake_crontab.body = "0 0 * * * /home/user/other-job\n"
    doctor_cron.install(mail_to=["a@b.c"], binary="/x/agx", config_path=saved_config)
    doctor_cron.install(every_minutes=5, binary="/x/agx", config_path=saved_config)
    assert fake_crontab.body.count(doctor_cron.CRON_MARKER) == 1
    assert "other-job" in fake_crontab.body
    assert "*/5 * * * *" in fake_crontab.body
    # Second install without --mail reused the configured recipients.
    assert cfg_mod.load(saved_config).doctor_cron.mail_to == ["a@b.c"]


def test_install_requires_recipient_and_sendmail(
    saved_config: Path, fake_crontab: FakeCrontab, mocker
) -> None:
    with pytest.raises(UserError, match="--mail"):
        doctor_cron.install(binary="/x/agx", config_path=saved_config)
    mocker.patch("agx_cli.doctor_cron._sendmail_path", return_value=None)
    with pytest.raises(UserError, match="sendmail"):
        doctor_cron.install(mail_to=["a@b.c"], binary="/x/agx", config_path=saved_config)
    assert fake_crontab.body == ""


def test_install_rejects_bad_address(
    saved_config: Path, fake_crontab: FakeCrontab, sendmail
) -> None:
    with pytest.raises(ValueError, match="mail address"):
        doctor_cron.install(mail_to=["not-an-address"], binary="/x", config_path=saved_config)


def test_uninstall_and_status(
    saved_config: Path, fake_crontab: FakeCrontab, sendmail, mocker
) -> None:
    mocker.patch("agx_cli.ui.emit")
    doctor_cron.install(mail_to=["a@b.c"], binary="/x/agx", config_path=saved_config)
    assert doctor_cron.status(config_path=saved_config)["installed"] is True
    doctor_cron.uninstall(config_path=saved_config)
    assert doctor_cron.CRON_MARKER not in fake_crontab.body
    info = doctor_cron.status(config_path=saved_config)
    assert info["installed"] is False
    assert cfg_mod.load(saved_config).doctor_cron.enabled is False


# ---------- run ----------


def test_run_mails_only_on_state_change(
    cfg: AgentsConfig, saved_config: Path, sendmail, mocker
) -> None:
    mocker.patch("agx_cli.ui.emit")
    cfg.doctor_cron.mail_to = ["ops@example.org"]
    cfg_mod.save(cfg, saved_config)
    doctor = mocker.patch("agx_cli.ops.doctor")

    # 1. Green → no state before, nothing failing → no mail.
    doctor.return_value = _report(vm_reachable="ok")
    r = doctor_cron.run(config_path=saved_config)
    assert r == {"status": "ok", "failing": [], "warning": [], "changed": False, "notified": False}
    sendmail.assert_not_called()
    assert doctor.call_args.kwargs["raise_on_fail"] is False

    # 2. A check goes red → one mail listing it, with the detail.
    doctor.return_value = _report(vm_reachable="ok", **{"coder.credential_pool": "fail"})
    r = doctor_cron.run(config_path=saved_config)
    assert r["status"] == "fail" and r["notified"] is True
    sendmail.assert_called_once()
    argv = sendmail.call_args.args[0]
    assert argv[:2] == ["/usr/sbin/sendmail", "-t"]
    msg = sendmail.call_args.kwargs["input"].decode()
    assert "To: ops@example.org" in msg
    assert "Subject: [agents] doctor: 1 check(s) failing" in msg
    assert "coder.credential_pool: detail of coder.credential_pool" in msg

    # 3. Still red, same set → no second mail.
    r = doctor_cron.run(config_path=saved_config)
    assert r["notified"] is False and r["changed"] is False
    assert sendmail.call_count == 1

    # 4. Warnings alone never trigger a mail.
    doctor.return_value = _report(
        vm_reachable="ok", **{"coder.credential_pool": "fail", "vm_disk": "warn"}
    )
    r = doctor_cron.run(config_path=saved_config)
    assert r["warning"] == ["vm_disk"] and r["notified"] is False

    # 5. Recovery → one "recovered" mail naming what cleared.
    doctor.return_value = _report(vm_reachable="ok", vm_disk="warn")
    r = doctor_cron.run(config_path=saved_config)
    assert r["status"] == "ok" and r["notified"] is True
    msg = sendmail.call_args.kwargs["input"].decode()
    assert "Subject: [agents] doctor: recovered" in msg
    assert "Recovered since last notice:\n  - coder.credential_pool" in msg
    assert "vm_disk: detail of vm_disk" in msg  # warning listed for context

    state = json.loads((cfg.paths.host_data_dir / ".doctor-cron" / "state.json").read_text())
    assert state["failing"] == []
    assert state["notified_at"] is not None


def test_run_without_recipients_records_state_but_never_mails(
    saved_config: Path, sendmail, mocker
) -> None:
    mocker.patch("agx_cli.ui.emit")
    mocker.patch("agx_cli.ops.doctor", return_value=_report(x="fail"))
    r = doctor_cron.run(config_path=saved_config)
    assert r["changed"] is True and r["notified"] is False
    sendmail.assert_not_called()


def test_run_surfaces_sendmail_failure(cfg: AgentsConfig, saved_config: Path, mocker) -> None:
    mocker.patch("agx_cli.ui.emit")
    cfg.doctor_cron.mail_to = ["a@b.c"]
    cfg_mod.save(cfg, saved_config)
    mocker.patch("agx_cli.ops.doctor", return_value=_report(x="fail"))
    mocker.patch("agx_cli.doctor_cron._sendmail_path", return_value="/usr/sbin/sendmail")
    mocker.patch(
        "agx_cli.doctor_cron.subprocess.run",
        return_value=mocker.MagicMock(returncode=75, stderr=b"deferred"),
    )
    with pytest.raises(RuntimeFailure, match="sendmail failed"):
        doctor_cron.run(config_path=saved_config)
