"""The fq client as agents drive it: parsing, exit codes, log cleaning, dry runs."""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fleetq import client
from fleetq.feeds.usage import UsageCache, usage_schema_json

ROOT = Path(__file__).resolve().parents[1]


# ---- parsing ----------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["--json", "q"], ["q", "--json"], ["--json", "q", "--json"],
    ["submit", "--gpus", "1", "--json", "--wrap", "x"], ["usage", "--idle", "--json"],
])
def test_json_is_accepted_before_or_after_the_subcommand(argv):
    assert client.build_parser().parse_args(argv).json is True


def test_json_defaults_off_and_every_command_documents_itself():
    parser = client.build_parser()
    assert parser.parse_args(["q"]).json is False
    commands = next(a for a in parser._actions if a.dest == "cmd")
    for name, sub in commands.choices.items():
        for action in sub._actions:
            if action.help is None:
                pytest.fail(f"fq {name} {action.option_strings or action.dest} has no help")


def test_job_ids_must_be_integers(capsys):
    with pytest.raises(SystemExit) as exc:
        client.build_parser().parse_args(["wait", "abc"])
    assert exc.value.code == 2


def test_job_flags_without_a_separator_get_a_hint(capsys):
    with pytest.raises(SystemExit) as exc:
        client.build_parser().parse_args(["submit", "--gpus", "1", "python", "x.py", "--lr", "1"])
    assert exc.value.code == 2
    assert "go after `--`" in capsys.readouterr().err


# ---- exit codes ---------------------------------------------------------------------------

@pytest.mark.parametrize(("status", "code"), [(401, 6), (403, 2), (404, 3), (409, 2), (429, 8), (503, 7)])
def test_unknown_error_codes_fall_back_to_the_http_status(status, code):
    api = client.Api("https://fleetq.example", "token")
    api.call = lambda *a, **kw: (status, {"ok": False, "error": {"code": "something_new", "message": "x"}})
    with pytest.raises(client.ClientError) as exc:
        api.ok("GET", "/api/v1/queue")
    assert exc.value.exit_code == code


def _configured(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("t" * 32)
    token.chmod(0o600)
    monkeypatch.setenv("FQ_URL", "https://fleetq.example")
    monkeypatch.setenv("FQ_TOKEN_FILE", str(token))
    monkeypatch.setenv("FQ_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def test_a_local_failure_is_a_usage_error_not_a_failed_job(monkeypatch, tmp_path, capsys):
    _configured(monkeypatch, tmp_path)
    code = client.main(["--json", "submit", "--gpus", "0", "--env-file", str(tmp_path / "missing.env"),
                        "--in-place", "/srv/x", "--on", "n1", "--dry-run", "--", "true"])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "local_error"


def test_localhost_is_refused_with_the_loopback_spelling(monkeypatch, tmp_path, capsys):
    _configured(monkeypatch, tmp_path)
    monkeypatch.setenv("FQ_URL", "http://localhost:8089")
    assert client.main(["whoami"]) == 2
    assert "127.0.0.1" in capsys.readouterr().err


# ---- submit -----------------------------------------------------------------------------

def test_dry_run_with_in_place_submits_nothing(monkeypatch, tmp_path, capsys):
    _configured(monkeypatch, tmp_path)
    monkeypatch.setattr(client.Api, "call", lambda *a, **kw: pytest.fail("a dry run contacted fleetqd"))
    code = client.main(["--json", "submit", "--gpus", "1", "--in-place", "/srv/code", "--on", "n1",
                        "--dry-run", "--", "python", "train.py"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == "fq.dryrun/v1" and out["spec"]["workdir"] == {"in_place": "/srv/code"}


def test_idempotency_keys_never_choose_a_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    seen = []

    class Api:
        def ok(self, method, path, **kw):
            seen.append(sorted(p.name for p in (tmp_path / "state" / "fleetq" / "pending").iterdir()))
            return {"jobs": [7]}

    key = "../../escape"
    assert client._post_job(Api(), SimpleNamespace(idempotency_key=key), {}) == {"jobs": [7]}
    assert seen == [[hashlib.sha256(key.encode()).hexdigest() + ".json"]]
    assert not (tmp_path / "escape.json").exists()
    for bad in ("x" * 201, "line\nbreak", "tab\there"):
        with pytest.raises(client.ClientError) as exc:
            client._post_job(Api(), SimpleNamespace(idempotency_key=bad), {})
        assert exc.value.exit_code == 2


def test_a_refused_submission_leaves_no_pending_record(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    class Api:
        def ok(self, method, path, **kw):
            raise client._err("unsatisfiable", "no such GPU", 2)

    with pytest.raises(client.ClientError):
        client._post_job(Api(), SimpleNamespace(idempotency_key="k1"), {})
    assert list((tmp_path / "state" / "fleetq" / "pending").iterdir()) == []


def test_the_pending_record_and_its_directory_are_owner_only(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    pending = state / "fleetq" / "pending"
    pending.mkdir(parents=True, mode=0o755)
    seen = []

    class Api:
        def ok(self, method, path, **kw):
            seen.append((pending.stat().st_mode & 0o777, [p.stat().st_mode & 0o777 for p in pending.iterdir()]))
            return {"jobs": [7]}

    old = os.umask(0o002)
    try:
        client._post_job(Api(), SimpleNamespace(idempotency_key="k1"), {"env": {"TOKEN": "secret"}})
    finally:
        os.umask(old)
    assert seen == [(0o700, [0o600])]


def test_submit_wait_names_the_job_before_blocking(monkeypatch, capsys):
    monkeypatch.setattr(client, "_spec_from_args", lambda args: {})
    monkeypatch.setattr(client, "_upload_bundle", lambda *a: "sha256:x")
    monkeypatch.setattr(client, "_post_job", lambda api, args, spec: {"jobs": [41]})

    def interrupted(*a):
        raise KeyboardInterrupt
    monkeypatch.setattr(client, "_wait_and_report", interrupted)
    args = SimpleNamespace(in_place=None, dir=".", dry_run=False, exclude=None, wait=True, json=True)
    with pytest.raises(KeyboardInterrupt):
        client.cmd_submit(object(), args)
    assert "submitted job 41" in capsys.readouterr().err


def test_queue_watch_refuses_json():
    args = client.build_parser().parse_args(["q", "--watch", "--json"])
    with pytest.raises(client.ClientError) as exc:
        client.cmd_queue(object(), args)
    assert exc.value.exit_code == 2


# ---- log cleaning --------------------------------------------------------------------------

BAR = b"".join(b"\rtrain %3d%%|##  | %d/10 [00:0%d]" % (i * 10, i, i) for i in range(1, 11))


@pytest.mark.parametrize(("raw", "clean"), [
    (b"hello\r\nworld\r\n", b"hello\nworld\n"),
    (b"a\n", b"a\n"),
    (b"no newline", b"no newline"),
    (b"\x1b[32mgreen\x1b[0m\n", b"green\n"),
    (b"abc\b\bX\n", b"aX\n"),
    (b"epoch 1\n" + BAR + b"\nepoch 2\n", b"epoch 1\ntrain 100% (10/10) [00:010]\nepoch 2\n"),
    (BAR, b"train 100% (10/10) [00:010]\n"),
])
def test_cleaning(raw, clean):
    assert client._clean_log_output(raw) == clean


@pytest.mark.parametrize("raw", [
    b"hello\r\nworld\r\n", "café ✓\n".encode(), b"\x1b[1;31merr\x1b[0m\n" + BAR + b"\ndone\n",
])
def test_cleaning_is_the_same_however_the_log_is_chunked(raw):
    cleaner = client._LogCleaner()
    streamed = b"".join(cleaner.feed(raw[i:i + 1]) for i in range(len(raw))) + cleaner.finish()
    assert streamed == client._clean_log_output(raw)


def test_cleaning_streams_finished_lines_and_bounds_an_endless_bar():
    cleaner = client._LogCleaner()
    assert cleaner.feed(b"step 1\nstep 2\npartial") == b"step 1\nstep 2\n"
    for i in range(10_000):
        cleaner.feed(b"\rtrain %d%%|#| %d/10000 [00:00]" % (i // 100, i))
    assert len(cleaner._partial) < 100
    # The redraws overwrote "partial", as they would have on a terminal.
    assert cleaner.finish() == b"train 99% (9999/10000) [00:00]\n"


def test_follow_streams_through_a_pipe(monkeypatch):
    pages = [{"data_b64": "c3RlcCAxCg==", "next_offset": 7, "end": 7, "complete": False, "generation": 0},
             {"data_b64": "c3RlcCAyCg==", "next_offset": 14, "end": 14, "complete": True, "generation": 0}]
    written = []

    class Out(io.BytesIO):
        def isatty(self):
            return False

        def flush(self):
            written.append(self.getvalue())

    class Api:
        def ok(self, method, path, **kw):
            return pages.pop(0)

    out = Out()
    monkeypatch.setattr(client.sys, "stdout", SimpleNamespace(buffer=out))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    args = SimpleNamespace(err=False, attempt=None, offset=None, tail=None, follow=True, json=False, raw=False, job=3)
    assert client.cmd_logs(Api(), args) == 0
    assert written[0] == b"step 1\n", "the first line must reach the pipe before the log completes"
    assert out.getvalue() == b"step 1\nstep 2\n"


# ---- usage ----------------------------------------------------------------------------------

def _usage(availability, *, stale=False, unreadable=False):
    now = time.time()
    sample = {"sample_time": now - (600 if stale else 5), "freshness_seconds": 90.0,
              "cpu_busy_fraction": 0.1, "ram_used_bytes": 1, "ram_total_bytes": 2}
    hosts = [{"target": "n1", "state": "live", **sample,
              "gpus": [{"uuid": f"GPU-{i}", "index": i, "model": "A100", "utilization_fraction": 0.0,
                        "vram_used_bytes": 0, "vram_total_bytes": 2**30, "supported": True,
                        "availability": verdict, "availability_reason": "r"}
                       for i, verdict in enumerate(availability)]}]
    if unreadable:
        hosts.append({"target": "r1", "state": "partial", **sample,
                      "gpu_error": "nvmlerror_librmversionmismatch", "gpus": []})
    cache = UsageCache(configured=True)
    cache.update({"schema": "fleetmon.usage/v1", "generated_at": now, "hosts": hosts})
    return cache.view()


def test_usage_idle_lists_only_gpus_fleetmon_proves_idle(capsys):
    envelope = _usage(["idle", "busy", "unknown"])

    class Api:
        def ok(self, method, path):
            return envelope
    args = client.build_parser().parse_args(["--json", "usage", "--idle"])
    assert client.cmd_usage(Api(), args) == 0
    hosts = json.loads(capsys.readouterr().out)["hosts"]
    assert [g["uuid"] for g in hosts[0]["gpus"]] == ["GPU-0"]
    table = client.fmt_usage(envelope)
    assert "1 idle, 1 busy, 1 unknown GPUs" in table and "FREE" in table


def test_usage_names_a_host_whose_gpus_are_unreadable(capsys):
    envelope = _usage(["idle"], unreadable=True)
    table = client.fmt_usage(envelope)
    row = next(line for line in table.splitlines() if line.startswith("r1"))
    assert "unreadable" in row and row.split()[-2] == "?", "FREE is unknown, not '-'"
    note = "r1: GPUs unreadable (NVIDIA driver and library versions differ; reboot the host)"
    assert note in table

    class Api:
        def ok(self, method, path):
            return envelope
    assert client.cmd_usage(Api(), client.build_parser().parse_args(["usage", "--idle"])) == 0
    idle = capsys.readouterr().out
    assert "n1" in idle and note in idle, "--idle still says which machine it could not read"
    stale = _usage(["idle"], unreadable=True, stale=True)
    assert "unreadable" not in client.fmt_usage(stale), "an old sample's error is not current"


def test_a_stale_host_has_no_idle_gpus():
    gpus = _usage(["idle"], stale=True)["hosts"][0]["gpus"]
    assert gpus[0]["availability"] == "unknown" and gpus[0]["availability_reason"] == "stale"


def test_both_committed_usage_contracts_match_the_model():
    expected = usage_schema_json()
    for path in (ROOT / "contracts" / "fleetmon-usage-v1.schema.json",
                 ROOT.parent / "fleetmon" / "contracts" / "fleetmon-usage-v1.schema.json"):
        if path.exists():
            assert path.read_text() == expected, (
                f"{path} is stale; regenerate it with "
                "python -c 'from fleetq.feeds.usage import usage_schema_json as s; print(s(), end=\"\")'")


def test_usage_idle_refuses_a_feed_without_verdicts():
    envelope = _usage([None, None])

    class Api:
        def ok(self, method, path):
            return envelope
    args = client.build_parser().parse_args(["usage", "--idle"])
    with pytest.raises(client.ClientError) as exc:
        client.cmd_usage(Api(), args)
    assert exc.value.exit_code == 7
    assert "no idle verdicts" in client.fmt_usage(envelope)
