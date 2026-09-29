"""The transport contract against the *real* fleetctl (§8), not the fake.

The fake fleetctl in tests/fakes mirrors the real envelope, and this is what
keeps it honest: every call here goes through fleetq's own Fleetctl class into
shared/fleet-dotfiles/bin/fleetctl, and is refused before any connection opens
(unknown target, empty budget, bad permit) or aimed at an unroutable RFC 5737
address inside the test network namespace. The permit round trip runs the
real fleetq API on loopback.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from fleetq import auth, bundles
from fleetq.api.app import Runtime, create_app
from fleetq.executors.fake import FakeNode
from fleetq.transport.fleetctl import Fleetctl, _classify
from fleetq.util import utcnow

from harness import Harness

REAL_FLEETCTL = Path(__file__).resolve().parents[2] / "shared" / "fleet-dotfiles" / "bin" / "fleetctl"
pytestmark = pytest.mark.skipif(not REAL_FLEETCTL.exists(), reason="fleet-dotfiles submodule not checked out")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def fleet(tmp_path):
    """A fleetctl config with a workstation and a budgeted managed site, both at 192.0.2.1."""
    sockets = Path(tempfile.mkdtemp(prefix="fc-", dir="/tmp"))
    config = tmp_path / "fleet"
    for sub in ("targets.d", "protocols.d", "profiles.d", "secrets"):
        (config / sub).mkdir(parents=True)
    (config / "config.toml").write_text(f'version = 1\n\n[ssh]\ncontrol_path = "{sockets}/%C"\n')
    (config / "projects.toml").write_text("version = 1\n")
    secret = config / "secrets" / "host.toml"
    secret.write_text('host = "192.0.2.1"\nuser = "nobody"\nport = 9\n')
    secret.chmod(0o600)
    (config / "targets.d" / "box.toml").write_text(
        f'version = 1\nname = "box"\nrole = "workstation"\nprotocol = "direct"\n'
        f'secret_backend = "file"\nsecret_ref = "{secret}"\n')
    (config / "targets.d" / "gate.toml").write_text(
        f'version = 1\nname = "gate"\nrole = "storage"\nprotocol = "managed"\n'
        f'secret_backend = "file"\nsecret_ref = "{secret}"\n')

    def protocol(extra: str = "") -> None:
        (config / "protocols.d" / "managed.toml").write_text(
            'version = 1\nname = "managed"\nkind = "direct"\n\n[control_budget]\n'
            f'action_per_minute = 60.0\nburst = 1\ncanonical_cluster = "site"\n{extra}')

    protocol()
    (config / "protocols.d" / "sched.toml").write_text(
        'version = 1\nname = "sched"\nkind = "slurm"\nrule_prefix = "S"\n\n[[queue]]\nname = "short"\n'
        'partition = "short"\ndefault = true\naccount = "research"\ntime_limit = "00:10:00"\n'
        'max_time = "24:00:00"\nevidence = "verified-live"\n')
    transport = Fleetctl(REAL_FLEETCTL, config_home=config, state_home=tmp_path / "state",
                         cache_home=tmp_path / "cache")
    transport.path = sys.executable                     # _patch_argv0 adds the script path
    yield transport, protocol, sockets
    shutil.rmtree(sockets, ignore_errors=True)


def _patch_argv0(transport: Fleetctl) -> None:
    base = transport._base

    def with_script():
        argv = base()
        return [argv[0], str(REAL_FLEETCTL), *argv[1:]]
    transport._base = with_script


def test_the_real_envelope_is_one_pretty_printed_document():
    doc = _complete_envelope()
    result = _classify(0, json.dumps(doc, indent=2).encode(), b"", mutation=True)
    assert result.ok and result.stdout == "hi\n" and result.envelope == doc


def _complete_envelope():
    return {"schema": "fleetctl.result/v1", "ok": True, "verb": "exec", "target": "box", "route": "direct",
            "outcome": "ok", "may_have_executed": False, "exit_code": 0, "remote_exit_code": 0,
            "stdout": "hi\n", "stderr": "", "stdout_truncated": False, "stderr_truncated": False,
            "duration_s": 0.01, "error": None, "details": {}}


@pytest.mark.parametrize("stdout", [
    b"",
    b"Warning: something\n" + json.dumps({"schema": "fleetctl.result/v1", "outcome": "ok"}).encode(),
    json.dumps({"schema": "fleetctl.result/v1", "outcome": "ok"}).encode() * 2,
    json.dumps({"schema": "fleetctl.result/v2", "outcome": "ok"}).encode(),
    b'{"schema": "fleetctl.result/v1", "outcome": "ok"',
])
def test_anything_but_exactly_one_v1_envelope_is_malformed(stdout):
    assert _classify(0, stdout, b"", mutation=True).outcome == "malformed_response"
    assert _classify(0, stdout, b"", mutation=True).may_have_executed is True
    assert _classify(0, stdout, b"", mutation=False).may_have_executed is False


@pytest.mark.parametrize("changes", [
    {"may_have_executed": "false"},
    {"outcome": "future_outcome"},
    {"outcome": ["ok"]},
    {"ok": False},
    {"exit_code": 1},
    {"details": None},
    {"details": {"retry_after": "now"}},
    {"stdout": None},
    {"outcome": "remote_failed", "ok": False, "exit_code": 0, "may_have_executed": False},
])
def test_incomplete_or_contradictory_envelope_is_uncertain_for_mutation(changes):
    doc = {**_complete_envelope(), **changes}
    result = _classify(0, json.dumps(doc).encode(), b"", mutation=True)
    assert result.outcome == "malformed_response"
    assert result.may_have_executed is True
    assert result.envelope is None


@pytest.mark.parametrize("missing", ["target", "remote_exit_code", "error", "details", "may_have_executed"])
def test_missing_envelope_field_is_uncertain_for_mutation(missing):
    doc = _complete_envelope()
    del doc[missing]
    result = _classify(0, json.dumps(doc).encode(), b"", mutation=True)
    assert result.outcome == "malformed_response"
    assert result.may_have_executed is True


def test_result_for_another_target_or_verb_is_uncertain():
    doc = _complete_envelope()
    payload = json.dumps(doc).encode()
    for verb, target in (("sync", "box"), ("exec", "other")):
        result = _classify(0, payload, b"", mutation=True,
                           expected_verb=verb, expected_target=target)
        assert result.outcome == "malformed_response"
        assert result.may_have_executed is True


def test_unknown_target_is_refused_and_never_ran(fleet):
    transport, _, sockets = fleet
    _patch_argv0(transport)
    result = run(transport.exec("nowhere", ["true"], timeout=20, mutation=True))
    assert result.outcome == "refused", (result.stdout, result.stderr)
    assert result.may_have_executed is False and result.exit_code == 2
    assert not any(sockets.iterdir())


def test_expected_role_blocks_inventory_drift_before_connection(fleet, tmp_path):
    transport, _, sockets = fleet
    _patch_argv0(transport)
    changed = transport.config_home / "targets.d" / "box.toml"
    changed.write_text(changed.read_text().replace('role = "workstation"', 'role = "compute"'))
    src = tmp_path / "stage"
    src.mkdir()
    operations = (
        transport.exec("box", ["true"], timeout=20, mutation=True, expected_role="workstation"),
        transport.sync_push("box", src, "/tmp/x", timeout=20, expected_role="workstation"),
        transport.sync_pull("box", "/tmp/x", tmp_path / "pull", timeout=20, expected_role="workstation"),
    )
    for operation in operations:
        result = run(operation)
        assert result.outcome == "refused" and result.may_have_executed is False, result.stderr
    assert not any(sockets.iterdir())


def test_an_empty_local_budget_is_budget_refused_with_retry_after(fleet):
    transport, _, sockets = fleet
    _patch_argv0(transport)
    result = run(transport.exec("gate", ["squeue"], timeout=20, mutation=False, admin=True, op_class="action"))
    assert result.outcome == "budget_refused", (result.stdout, result.stderr)
    assert result.may_have_executed is False and result.exit_code == 75
    assert result.retry_after and result.retry_after > 0
    assert not any(sockets.iterdir())


def test_sync_push_argv_is_what_the_real_parser_accepts(fleet, tmp_path):
    transport, _, _ = fleet
    _patch_argv0(transport)
    src = tmp_path / "inbox"
    src.mkdir()
    result = run(transport.sync_push("nowhere", src, "/tmp/x", timeout=20))
    # An argv the parser rejected would print usage and no envelope at all.
    assert result.envelope is not None, result.stderr
    assert result.outcome == "refused"


def test_sync_pull_argv_is_what_the_real_parser_accepts(fleet, tmp_path):
    transport, _, _ = fleet
    _patch_argv0(transport)
    result = run(transport.sync_pull("nowhere", "/tmp/x/outbox", tmp_path / "into", timeout=20, admin=True))
    assert result.envelope is not None, result.stderr
    assert result.outcome == "refused" and result.may_have_executed is False


def test_preflight_report_is_parsed_with_its_error_rule_ids(fleet, tmp_path):
    transport, _, _ = fleet
    _patch_argv0(transport)
    header = "#!/bin/bash\n#SBATCH --partition=short\n#SBATCH --account=research\n"
    good = tmp_path / "good.sh"
    good.write_text(header + "#SBATCH --time=00:10:00\necho hi\n")
    result = run(transport.preflight(good, "sched"))
    assert result.ok and result.details["errors"] == [], (result.stdout, result.stderr)
    over = tmp_path / "over.sh"
    over.write_text(header + "#SBATCH --time=48:00:00\necho hi\n")
    refused = run(transport.preflight(over, "sched"))
    assert refused.ok and refused.details["errors"] == ["S033"], refused.stdout
    # No report at all (a non-scheduler protocol, a missing file) is a failure, not a refusal.
    assert not run(transport.preflight(good, "gate")).ok
    assert not run(transport.preflight(tmp_path / "absent.sh", "sched")).ok


# ---- the central budget, end to end: real fleetctl redeeming against the real API ----------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def authority(tmp_path):
    import uvicorn

    (tmp_path / "fq").mkdir()
    h = Harness(tmp_path / "fq", [FakeNode("n1", gpus=["GPU-a"])])
    (h.tmp / "bundles").mkdir()

    def seed(conn):
        conn.execute("INSERT INTO nodes (id, backend, enabled, config_json, updated_at) VALUES (?,?,?,?,?)",
                     ("site", "slurm", 1, json.dumps({"fleetctl_target": "gate",
                                                      "budget": {"action_per_minute": 600, "burst": 32,
                                                                 "sessions_per_minute": 60, "sessions_burst": 8,
                                                                 "bytes_per_minute": 1024, "bytes_burst": 1024,
                                                                 "max_sessions_per_operation": 2,
                                                                 "max_bytes_per_transfer": 1024,
                                                                 "action_session_reserve": 2}}), utcnow()))
    h.store.run_sync(seed)
    _, token = h.store.run_sync(lambda c: auth.create_token(c, owner="suresh", kind="service", label="fleetctl",
                                                             scopes=("permits",)))
    token_file = tmp_path / "authority.token"
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)
    ctl = h.start()
    app = create_app(Runtime(store=h.store, controller=ctl, bundle_dir=h.tmp / "bundles",
                             bundle_limits=bundles.BundleLimits(), clock_ok=lambda: True))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    yield h, f"http://127.0.0.1:{port}", token_file
    server.should_exit = True
    thread.join(timeout=10)
    h.close()


def test_fleetctl_redeems_a_fleetq_permit_exactly_once_and_only_for_its_bucket(fleet, authority):
    from fleetq import budget

    transport, protocol, sockets = fleet
    _patch_argv0(transport)
    h, url, token_file = authority
    protocol(f'authority_url = "{url}"\nauthority_token_file = "{token_file}"\n')

    def grant(op_class):
        return h.store.run_sync(lambda c: budget.grant_permit(
            c, cluster="gate", op_class=op_class,
            cost={"rpc": 8, "sessions": 2, "bytes": 0}, caller="fleetqd"))

    permit = grant("action")
    assert permit["granted"] and permit["cluster"] == "site"
    first = run(transport.exec("gate", ["true"], timeout=20, mutation=True, admin=True, op_class="action",
                               permit=permit["permit_id"]))
    # Redeemed, so fleetctl went on to connect -- and the unroutable host failed it.
    assert first.outcome == "transport_failed" and first.may_have_executed is False, (first.stdout, first.stderr)
    again = run(transport.exec("gate", ["true"], timeout=20, mutation=True, admin=True, op_class="action",
                               permit=permit["permit_id"]))
    assert again.outcome == "budget_refused" and again.may_have_executed is False
    assert "not valid" in again.envelope["error"]["message"]
    other = grant("action")
    wrong = run(transport.exec("gate", ["true"], timeout=20, mutation=True, admin=True, op_class="monitor",
                               permit=other["permit_id"]))
    assert wrong.outcome == "budget_refused"
    forged = run(transport.exec("gate", ["true"], timeout=20, mutation=True, admin=True, op_class="action",
                                permit="pmt_forged"))
    assert forged.outcome == "budget_refused"
