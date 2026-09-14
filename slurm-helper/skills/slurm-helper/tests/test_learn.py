"""Self-learning loop: record evidence, distill it into the site config."""

import json
from copy import deepcopy

from _path import SRC  # noqa: F401

from helpers import SITE, fixture
from kiac_slurm import learn
from kiac_slurm.cli import main, run_check
from kiac_slurm.config import load_site_config
from kiac_slurm.diagnostics import Report
from kiac_slurm.parser import parse_text
from kiac_slurm.rules_kiac import check_kiac
from test_live import FakeRunner, live_runner


def enable_learning(tmp_path, monkeypatch):
    obs = tmp_path / "obs.jsonl"
    monkeypatch.setenv("KIAC_SLURM_LEARN", "1")
    monkeypatch.setenv("KIAC_SLURM_OBSERVATIONS", str(obs))
    return obs


def test_record_and_dedup(tmp_path, monkeypatch):
    obs = enable_learning(tmp_path, monkeypatch)
    assert learn.record({"kind": "maxtime", "subject": "short", "observed": "12:00:00"})
    assert not learn.record({"kind": "maxtime", "subject": "short", "observed": "12:00:00"})
    assert learn.record({"kind": "maxtime", "subject": "short", "observed": "24:00:00"})
    entries = learn.read_observations(obs)
    assert len(entries) == 2
    assert all(e.get("ts") and e.get("host") for e in entries)


def test_record_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("KIAC_SLURM_LEARN", "off")
    obs = tmp_path / "obs.jsonl"
    monkeypatch.setenv("KIAC_SLURM_OBSERVATIONS", str(obs))
    assert not learn.record({"kind": "x", "subject": "y", "observed": "z"})
    assert learn.read_observations(obs) == []


def test_live_check_records_discovery_diffs(tmp_path, monkeypatch):
    obs = enable_learning(tmp_path, monkeypatch)
    monkeypatch.setenv("KIAC_SLURM_CACHE_DIR", str(tmp_path / "cache"))
    rep = run_check(
        str(fixture("valid_gpu.sbatch")), live=True, site=SITE,
        runner=live_runner(), no_cache=True, shellcheck="off",
    )
    assert rep.exit_code() == 0
    entries = learn.read_observations(obs)
    kinds = {(e["kind"], e["subject"], e["observed"]) for e in entries}
    # the fake live sweep resolves both disputed MaxTimes and records the rest
    # (times render in Slurm's days format: 48h -> 2-00:00:00)
    assert ("maxtime", "long", "2-00:00:00") in kinds
    assert ("maxtime", "short", "12:00:00") in kinds
    assert ("maxtime", "a100", "1-00:00:00") in kinds
    short_entry = next(
        e for e in entries
        if e["kind"] == "maxtime" and e["subject"] == "short"
    )
    assert "resolves documented dispute" in short_entry["detail"]


def test_reason_parsing_variants():
    account = learn.parse_reason_evidence(
        "Job's account not permitted to use this partition "
        "(a100 allows research,freerun_not_chiru)", job_account="chiru"
    )
    assert account and account[0]["kind"] == "account-policy"
    assert account[0]["subject"] == "a100"
    assert account[0]["observed"] == "research,freerun_not_chiru"
    assert account[0]["job_account"] == "chiru"

    qos = learn.parse_reason_evidence(
        "Job's QOS not permitted to use this partition (h200 allows h200_qos not normal)"
    )
    assert qos and qos[0]["kind"] == "qos-required"
    assert qos[0]["subject"] == "h200" and qos[0]["observed"] == "h200_qos"

    invalid = learn.parse_reason_evidence(
        "allocation failure: Invalid account or account/partition combination specified",
        job_account="freerun",
    )
    assert invalid and invalid[0]["kind"] == "account-invalid"

    assert learn.parse_reason_evidence("Resources") == []


def test_inspect_records_pending_reason(tmp_path, monkeypatch, capsys):
    obs = enable_learning(tmp_path, monkeypatch)
    runner = FakeRunner(
        {
            ("squeue",): (
                0,
                "PENDING|Job's account not permitted to use this partition "
                "(a100 allows research,freerun_not_chiru)|a100|chiru|4:00:00|0:12|cn6\n",
            )
        }
    )
    rc = main(["inspect", "4242"], runner=runner)
    assert rc == 0
    entries = learn.read_observations(obs)
    assert any(
        e["kind"] == "account-policy" and e["subject"] == "a100"
        and e["observed"] == "research,freerun_not_chiru" and e.get("job_account") == "chiru"
        for e in entries
    )


def test_learn_apply_flow(tmp_path, monkeypatch, capsys):
    obs = enable_learning(tmp_path, monkeypatch)
    monkeypatch.setenv("KIAC_SLURM_CACHE_DIR", str(tmp_path / "cache"))
    config_copy = tmp_path / "kiac.yaml"
    config_copy.write_text((SRC.parent / "config" / "kiac.yaml").read_text())
    monkeypatch.setenv("KIAC_SLURM_CONFIG", str(config_copy))

    # evidence: the exact rejection observed on the real cluster
    assert learn.record({
        "kind": "account-policy",
        "subject": "a100",
        "observed": "research,freerun",
        "job_account": "chiru",
        "detail": "from a pending job reason",
    })

    # dry run: diff shown, file untouched
    rc = main(["learn", "apply"], runner=live_runner())
    assert rc == 0
    captured = capsys.readouterr()
    assert "verified_live" in captured.out + captured.err
    original = config_copy.read_text()

    rc = main(["learn", "apply", "--yes"], runner=live_runner())
    assert rc == 0
    assert config_copy.read_text() != original
    assert (tmp_path / "kiac.yaml.bak").read_text() == original

    site = load_site_config(str(config_copy))
    a100 = site.verified_partition("a100")
    assert a100["allowed_accounts"] == ["research", "freerun"]
    assert "chiru" in a100["denied_accounts"]
    # disputed short MaxTime resolved by the live sweep and persisted
    assert site.verified_max_time("short") == 12 * 3600
    assert learn.BEGIN_MARKER in config_copy.read_text()
    # round-trips through the config loader (dumped YAML is parseable)
    assert site.verified_partition("h200")["required_qos"] == "h200_qos"


def test_learn_apply_noop(tmp_path, monkeypatch, capsys):
    enable_learning(tmp_path, monkeypatch)
    monkeypatch.setenv("KIAC_SLURM_CACHE_DIR", str(tmp_path / "cache"))
    config_copy = tmp_path / "kiac.yaml"
    config_copy.write_text((SRC.parent / "config" / "kiac.yaml").read_text())
    monkeypatch.setenv("KIAC_SLURM_CONFIG", str(config_copy))

    # no observations, and the runner has no reachable scheduler or accounting
    rc = main(["learn", "apply", "--yes"], runner=FakeRunner())
    assert rc == 0
    out = capsys.readouterr().out
    # off-cluster no-op must say there was no live sweep, not claim a match
    assert "no live scheduler reachable" in out
    assert config_copy.read_text() == (SRC.parent / "config" / "kiac.yaml").read_text()


def test_manual_note_never_becomes_config(tmp_path, monkeypatch):
    """Regression: a manual-note entry must not leak a bogus partition entry
    or make a no-op apply rewrite the config."""
    enable_learning(tmp_path, monkeypatch)
    learn.record({
        "kind": "manual-note", "subject": "note",
        "observed": "some observation", "detail": "some observation",
    })
    new_verified = learn.build_verified_update(SITE, None, learn.read_observations())
    assert "note" not in (new_verified.get("partitions") or {})
    assert learn._strip_as_of(new_verified) == learn._strip_as_of(SITE.verified)


def test_learned_maxtime_drives_offline_check():
    learned = deepcopy(SITE)
    learned.verified = dict(SITE.verified)
    learned.verified["max_times"] = {"short": "12:00:00"}

    over = parse_text("#!/bin/bash\n#SBATCH --partition=short\n#SBATCH --time=18:00:00\n")
    rep = Report()
    check_kiac(over, rep, learned)
    k333 = [d for d in rep.items if d.rule_id == "KIAC033"]
    assert k333 and k333[0].level == "ERROR"
    assert k333[0].confidence == "verified-live"

    under = parse_text("#!/bin/bash\n#SBATCH --partition=short\n#SBATCH --time=06:00:00\n")
    rep = Report()
    check_kiac(under, rep, learned)
    k333 = [d for d in rep.items if d.rule_id == "KIAC033"]
    assert any(d.level == "PASS" for d in k333)
    assert any(d.level == "INFO" and "resolved" in d.message for d in rep.items)


def test_learn_note_and_log_cli(tmp_path, monkeypatch, capsys):
    obs = enable_learning(tmp_path, monkeypatch)
    rc = main(["learn", "note", "FS001 false positive for /storage paths off-cluster"])
    assert rc == 0
    entries = learn.read_observations(obs)
    assert entries and entries[0]["kind"] == "manual-note"
    rc = main(["learn", "log"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "manual-note" in out and "FS001 false positive" in out
