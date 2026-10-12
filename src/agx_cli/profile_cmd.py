"""``agx profile list <agent>`` — enumerate a Hermes agent's profiles.

Read-only discovery anchor for the GH #6 tier-2 surface: every other
command that grew a ``--profile`` flag (secret, conversations, model
use, memory) targets a profile by name; this is where those names come
from, together with each profile's gateway posture.

Profiles are Hermes-owned (created with ``hermes profile create`` inside
the container); the toolkit deliberately does not create or delete them
— it only reads. One probe per invocation, exec'd in the container:
for the default home plus every ``/opt/data/profiles/<name>/``, emit
config presence, the gateway's desired state (``gateway_state.json``,
the record upstream's boot reconciler replays), and the live s6 service
state for ``gateway-<name>``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agx_cli import config as cfg_mod
from agx_cli import mp, ui
from agx_cli.agents_cmd import _detect_type_from_compose
from agx_cli.agents_cmd import service_name as _service_name_for
from agx_cli.errors import RuntimeFailure, UserError

_LIST_PROBE = """\
import glob, json, os, subprocess
rows = []
homes = [("default", "/opt/data")] + sorted(
    (os.path.basename(os.path.dirname(p)), os.path.dirname(p))
    for p in glob.glob("/opt/data/profiles/*/")
)
for name, home in homes:
    row = {
        "profile": name,
        "config": os.path.isfile(home + "/config.yaml"),
        "state_db": os.path.isfile(home + "/state.db"),
        "gateway_desired": None,
        "gateway_s6": None,
    }
    try:
        state = json.load(open(home + "/gateway_state.json"))
        if isinstance(state, dict):
            row["gateway_desired"] = state.get("desired_state")
    except Exception:
        pass
    svc = "/run/service/gateway-" + name
    if os.path.isdir(svc):
        # Absolute path: the exec env PATH lacks /command (same pitfall
        # as the cont-init hook), and a bare name FileNotFoundErrors.
        svstat = "/command/s6-svstat" if os.path.exists("/command/s6-svstat") else "s6-svstat"
        try:
            out = subprocess.run(
                [svstat, "-o", "up", svc], capture_output=True, text=True, timeout=10
            )
            row["gateway_s6"] = "up" if out.stdout.strip() == "true" else "down"
        except Exception:
            row["gateway_s6"] = "unknown"
    rows.append(row)
print(json.dumps({"profiles": rows}))
"""


def list_profiles(
    name: str,
    *,
    config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    cfg = cfg_mod.load(config_path)
    agent_type = _detect_type_from_compose(cfg, name)
    if agent_type is None:
        raise UserError(f"agent {name!r} not in compose")
    if agent_type != "hermes":
        raise UserError(
            f"profiles are Hermes-only — {name!r} is type {agent_type!r} "
            f"(OpenClaw has no profile concept)"
        )
    svc = _service_name_for(name, agent_type)

    from agx_cli.ops import _compose_exec_in_vm_argv

    result = mp.exec_in_vm(
        cfg.vm.name,
        *_compose_exec_in_vm_argv(
            cfg, svc, "python3", "-c", _LIST_PROBE, no_tty=True, as_root=True
        ),
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or "").lower()
        if "is not running" in err or "no such" in err:
            raise UserError(f"container for {svc!r} is not running")
        raise RuntimeFailure(
            f"profile probe failed (rc={result.returncode}): "
            f"{(result.stderr or '').strip() or 'no stderr'}"
        )
    try:
        data: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeFailure(f"could not parse profile probe output: {e}") from e

    payload = {"agent": name, "profiles": data.get("profiles", [])}
    ui.emit(
        payload,
        human="\n".join(
            f"{r['profile']}: config={'yes' if r.get('config') else 'no'} "
            f"sessions={'yes' if r.get('state_db') else 'no'} "
            f"gateway={r.get('gateway_desired') or '-'}"
            + (f" (s6 {r['gateway_s6']})" if r.get("gateway_s6") else "")
            for r in payload["profiles"]
        )
        or "(no profiles)",
    )
    return payload
