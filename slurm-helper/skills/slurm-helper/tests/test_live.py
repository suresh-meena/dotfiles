import json

from _path import SRC  # noqa: F401

from helpers import SITE, fixture
from kiac_slurm.cli import main, run_check
from kiac_slurm.live import (
    ClusterState,
    accounts,
    accounts_qos,
    discover,
    parse_scontrol_nodes,
    parse_scontrol_partitions,
    parse_sinfo_json,
)

SPART = "\n".join(
    [
        "PartitionName=long AllocNodes=ALL Default=no MaxTime=48:00:00 DefaultTime=none Nodes=cn[1,4] State=UP",
        "PartitionName=short AllocNodes=ALL Default=no MaxTime=12:00:00 DefaultTime=none Nodes=cn[2,5] State=UP",
        "PartitionName=medium MaxTime=24:00:00 Nodes=cn3 State=UP",
        "PartitionName=a100 MaxTime=1-00:00:00 Nodes=cn6 State=UP",
        "PartitionName=ada MaxTime=48:00:00 Nodes=cn[7-9] State=UP",
        "PartitionName=h200 MaxTime=24:00:00 Nodes=cn10 AllowAccounts=h200grp State=UP",
    ]
)

SNODES = "\n".join(
    [
        "NodeName=cn1 State=IDLE Gres=(null) RealMemory=257000 Partitions=long",
        "NodeName=cn4 State=MIXED Gres=(null) RealMemory=257000 Partitions=long",
        "NodeName=cn2 State=IDLE Gres=(null) RealMemory=257000 Partitions=short",
        "NodeName=cn5 State=ALLOCATED Gres=(null) RealMemory=257000 Partitions=short",
        "NodeName=cn3 State=IDLE Gres=(null) RealMemory=257000 Partitions=medium",
        "NodeName=cn6 State=IDLE Gres=gpu:a100:8(S:0-1) RealMemory=515000 Partitions=a100",
        "NodeName=cn7 State=IDLE Gres=gpu:ADA6000:4(S:0-1) RealMemory=128000 Partitions=ada",
        "NodeName=cn8 State=IDLE Gres=gpu:ADA6000:4(S:0-1) RealMemory=128000 Partitions=ada",
        "NodeName=cn9 State=DRAIN Gres=gpu:ADA6000:4(S:0-1) RealMemory=128000 Partitions=ada",
        "NodeName=cn10 State=MIXED Gres=gpu:h200:4(S:0-1) RealMemory=1410000 Partitions=h200",
    ]
)

SINFO_JSON = json.dumps(
    {
        "partitions": [
            {
                "name": "long",
                "time": {
                    "maximum": {"seconds": 172800, "infinite": False},
                    "default": {"seconds": 3600, "infinite": False},
                },
                "nodes": {"list": "cn[1,4]"},
            },
            {"name": "a100", "time": {"maximum": {"seconds": 86400, "infinite": False}}, "nodes": {"list": "cn6"}},
        ],
        "nodes": [
            {
                "name": "cn6",
                "state": ["IDLE"],
                "tres": "cpu=64,mem=500G,gres/gpu=8,gres/gpu:a100=8",
                "real_memory": 515000,
                "partitions": ["a100"],
            }
        ],
    }
)


class FakeRunner:
    def __init__(self, responses=None):
        self.responses = {tuple(k): v for k, v in (responses or {}).items()}
        self.calls = []

    def run(self, cmd):
        self.calls.append(list(cmd))
        for key, value in self.responses.items():
            if tuple(cmd[: len(key)]) == key:
                return value
        return 0, ""

    def add(self, key, rc, out):
        self.responses[tuple(key)] = (rc, out)


SINFO_JSON_NUMBER = json.dumps(
    {
        # the shape SchedMD's data_parser actually emits (rest_api.html):
        # numbers wrapped as {"set":..,"infinite":..,"number":..}, and
        # partition nodes as a plain hostlist string
        "partitions": [
            {
                "name": "a100",
                "time": {
                    "maximum": {"set": True, "infinite": False, "number": 86400},
                    "default": {"set": True, "infinite": False, "number": 3600},
                },
                "nodes": "cn6",
            }
        ],
        "nodes": [
            {
                "name": "cn6",
                "state": ["IDLE"],
                "tres": "cpu=64,mem=500G,gres/gpu:a100=8",
                "real_memory": {"set": True, "infinite": False, "number": 515000},
                "partitions": ["a100"],
            }
        ],
    }
)

# clean JSON, wrong field names: parses fine but carries no usable facts
SINFO_JSON_WRONGSHAPE = json.dumps(
    {
        "partitions": [
            {"name": "a100", "time": {"maximum": {"set": True, "unheard_of": 86400}}, "nodes": "cn6"}
        ],
        "nodes": [],
    }
)


def live_runner(test_only_rc=0, test_only_out="sbatch: job 4242 would be submitted"):
    return FakeRunner(
        {
            ("sinfo", "--json"): (1, "sinfo: error: unrecognized option"),
            ("scontrol", "-o", "show", "partitions"): (0, SPART),
            ("scontrol", "-o", "show", "nodes"): (0, SNODES),
            ("sacctmgr",): (0, "research|normal\nchiru|normal,h200_qos\n"),
            ("sbatch", "--test-only"): (test_only_rc, test_only_out),
        }
    )


def ids(rep):
    return rep.rule_ids()


def test_scontrol_parsing():
    parts = parse_scontrol_partitions(SPART)
    assert set(parts) == {"long", "short", "medium", "a100", "ada", "h200"}
    assert parts["short"].max_time == 12 * 3600  # live resolves the disputed table
    assert parts["long"].nodes == ["cn1", "cn4"]
    assert parts["h200"].max_time == 24 * 3600

    nodes = parse_scontrol_nodes(SNODES)
    cn6 = nodes["cn6"]
    assert cn6.gres == [("gpu", "a100", 8)]
    assert cn6.real_memory == 515000
    assert nodes["cn1"].gres == []
    assert nodes["cn9"].is_down()
    assert not nodes["cn7"].is_down()


def test_discover_prefers_json_then_falls_back(tmp_path):
    runner = FakeRunner({("sinfo", "--json"): (0, SINFO_JSON)})
    state = discover(runner=runner, cache_dir=str(tmp_path), force=True)
    assert state.source == "sinfo --json"
    assert state.partitions["long"].max_time == 172800
    assert state.partitions["long"].nodes == ["cn1", "cn4"]
    assert state.nodes["cn6"].gres == [("gpu", None, 8), ("gpu", "a100", 8)]

    runner = live_runner()
    state = discover(runner=runner, cache_dir=str(tmp_path), force=True)
    assert state.source == "scontrol show"
    assert state.partition_gres_types("a100") == {"a100"}
    assert state.partition_gpu_max("a100", "a100") == 8
    assert state.partition_gpu_max("a100", None) == 8
    assert state.partition_has_gpu("long") is False


def test_discover_caches(tmp_path):
    runner = live_runner()
    discover(runner=runner, cache_dir=str(tmp_path), force=True)
    calls_before = len(runner.calls)
    cached = discover(runner=runner, cache_dir=str(tmp_path))
    assert cached is not None and cached.source.endswith("(cached)")
    assert len(runner.calls) == calls_before  # served entirely from cache

    fresh = discover(runner=runner, cache_dir=str(tmp_path), ttl=0)
    assert fresh is not None and not fresh.source.endswith("(cached)")


def test_accounts_parsing():
    assert accounts(live_runner()) == ["chiru", "research"]
    acc_qos = accounts_qos(live_runner())
    assert acc_qos == {"research": {"normal"}, "chiru": {"normal", "h200_qos"}}
    assert accounts(FakeRunner({("sacctmgr",): (1, "error")})) is None


def test_sinfo_json_number_shape(tmp_path):
    runner = FakeRunner({("sinfo", "--json"): (0, SINFO_JSON_NUMBER)})
    state = discover(runner=runner, cache_dir=str(tmp_path), force=True)
    assert state.source == "sinfo --json"
    assert state.partitions["a100"].max_time == 86400
    assert state.partitions["a100"].default_time == 3600
    assert state.partitions["a100"].nodes == ["cn6"]
    assert state.nodes["cn6"].real_memory == 515000
    assert state.nodes["cn6"].gres == [("gpu", "a100", 8)]


def test_sinfo_json_wrong_shape_falls_back_to_scontrol(tmp_path):
    """P0 regression: clean JSON with unknown keys must not masquerade as an
    authoritative cluster with unknown limits / no GPUs."""
    runner = FakeRunner(
        {
            ("sinfo", "--json"): (0, SINFO_JSON_WRONGSHAPE),
            ("scontrol", "-o", "show", "partitions"): (0, SPART),
            ("scontrol", "-o", "show", "nodes"): (0, SNODES),
        }
    )
    state = discover(runner=runner, cache_dir=str(tmp_path), force=True)
    assert state.source == "scontrol show"  # fell back
    assert state.partitions["short"].max_time == 12 * 3600
    assert state.partition_has_gpu("a100")


def test_live_partition_not_found():
    rep = run_check(
        str(fixture("manual_basic.sbatch")),
        live=True,
        site=SITE,
        runner=live_runner(),
        cache_dir=None,  # not used; force via no_cache
        no_cache=True,
        shellcheck="off",
    )
    assert "KIAC011" in ids(rep)
    diag = [d for d in rep.items if d.rule_id == "KIAC011"][0]
    assert diag.level == "ERROR"
    assert diag.confidence == "verified-live"
    assert "Live lookup: partition not found" in diag.message
    assert "LIVE001" in ids(rep)  # test-only accepted
    assert rep.exit_code() == 1


def test_live_maxtime_authoritative():
    runner = live_runner()
    rep = run_check(
        str(fixture("bad_time.sbatch")), live=True, site=SITE,
        runner=runner, no_cache=True, shellcheck="off",
    )
    assert "LIVE011" in ids(rep)
    diag = [d for d in rep.items if d.rule_id == "LIVE011"][0]
    assert diag.level == "ERROR"
    assert "72:00:00" in diag.message and "2-00:00:00" in diag.message

    # disputed short: live says 12h; an 18h request is now a hard error
    rep = run_check(
        str(fixture("time_disputed.sbatch")), live=True, site=SITE,
        runner=live_runner(), no_cache=True, shellcheck="off",
    )
    assert rep.exit_code() == 1
    live_11 = [d for d in rep.items if d.rule_id == "LIVE011"]
    assert any(d.level == "ERROR" and "12:00:00" in d.message for d in live_11)

    # a fitting request resolves the documented dispute with PASS + INFO
    from kiac_slurm.diagnostics import Report
    from kiac_slurm.parser import parse_text
    from kiac_slurm.rules_kiac import check_kiac

    fitting = parse_text("#!/bin/bash\n#SBATCH --partition=short\n#SBATCH --time=06:00:00\n")
    rep = Report()
    check_kiac(fitting, rep, SITE, state=discover(runner=live_runner(), force=True))
    live_11 = [d for d in rep.items if d.rule_id == "LIVE011"]
    assert any(d.level == "PASS" for d in live_11)
    resolved = [d for d in live_11 if d.level == "INFO"]
    assert resolved and "resolved" in resolved[0].message


def test_live_gpu_type_and_count():
    from kiac_slurm.parser import parse_text

    runner = live_runner()
    state = discover(runner=runner, force=True)
    from kiac_slurm.diagnostics import Report
    from kiac_slurm.rules_kiac import check_kiac

    wrong_type = parse_text(
        "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --gres=gpu:h200:1\n"
    )
    rep = Report()
    check_kiac(wrong_type, rep, SITE, state=state, accounts=None)
    l20 = [d for d in rep.items if d.rule_id == "LIVE020"]
    assert l20 and l20[0].level == "ERROR" and "h200" in l20[0].message

    too_many = parse_text(
        "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --gres=gpu:a100:16\n"
    )
    rep = Report()
    check_kiac(too_many, rep, SITE, state=state, accounts=None)
    assert any(d.level == "ERROR" and "at most 8" in d.message for d in rep.items)

    cpu_partition = parse_text(
        "#!/bin/bash\n#SBATCH --partition=long\n#SBATCH --gres=gpu:1\n"
    )
    rep = Report()
    check_kiac(cpu_partition, rep, SITE, state=state, accounts=None)
    assert "LIVE021" in rep.rule_ids()


def test_live_account_association():
    from kiac_slurm.diagnostics import Report
    from kiac_slurm.parser import parse_text
    from kiac_slurm.rules_kiac import check_kiac

    runner = live_runner()
    state = discover(runner=runner, force=True)
    accs = accounts(runner)

    good = parse_text(
        "#!/bin/bash\n#SBATCH --partition=h200\n#SBATCH --account=chiru\n"
        "#SBATCH --qos=h200_qos\n#SBATCH --gres=gpu:1\n"
    )
    rep = Report()
    check_kiac(good, rep, SITE, state=state, accounts=accs)
    assert any(d.rule_id == "LIVE030" and d.level == "PASS" for d in rep.items)
    assert not [d for d in rep.items if d.level == "ERROR"]

    bogus = parse_text(
        "#!/bin/bash\n#SBATCH --partition=h200\n#SBATCH --account=bogus\n#SBATCH --gres=gpu:1\n"
    )
    rep = Report()
    check_kiac(bogus, rep, SITE, state=state, accounts=accs)
    l30 = [d for d in rep.items if d.rule_id == "LIVE030"]
    assert l30 and l30[0].level == "ERROR"


def test_test_only_failure_is_error():
    runner = live_runner(test_only_rc=1,
                         test_only_out="sbatch: error: Batch job submission failed: Invalid partition name specified")
    rep = run_check(
        str(fixture("manual_basic.sbatch")), live=True, site=SITE,
        runner=runner, no_cache=True, shellcheck="off",
    )
    l1 = [d for d in rep.items if d.rule_id == "LIVE001"][0]
    assert l1.level == "ERROR"
    assert rep.exit_code() == 1


def test_check_never_submits():
    runner = live_runner()
    run_check(
        str(fixture("valid_gpu.sbatch")), live=True, site=SITE,
        runner=runner, no_cache=True, shellcheck="off",
    )
    assert not any(c[:1] == ["sbatch"] and "--test-only" not in c for c in runner.calls)


def test_live_memory_exceeds_node_cap():
    from kiac_slurm.diagnostics import Report
    from kiac_slurm.parser import parse_text
    from kiac_slurm.rules_kiac import check_kiac

    state = discover(runner=live_runner(), force=True)
    greedy = parse_text(
        "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --mem=600G\n"
    )
    rep = Report()
    check_kiac(greedy, rep, SITE, state=state)
    l12 = [d for d in rep.items if d.rule_id == "LIVE012"]
    assert l12 and l12[0].level == "ERROR" and "515000" in l12[0].message


def test_cli_main_exit_codes(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("KIAC_SLURM_CACHE_DIR", str(tmp_path / "cache"))
    rc = main(
        ["check", str(fixture("valid_gpu.sbatch")), "--live", "--json"],
        runner=live_runner(),
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 0
    assert payload["live"] is True

    rc = main(
        ["check", str(fixture("manual_basic.sbatch"))],
        runner=live_runner(),
    )
    assert rc == 1


def test_submit_gating(tmp_path, capsys, monkeypatch):
    """submit must run the checker first; errors block sbatch, --yes is required."""
    monkeypatch.setenv("KIAC_SLURM_CACHE_DIR", str(tmp_path / "cache"))

    runner = live_runner()
    rc = main(["submit", str(fixture("manual_basic.sbatch")), "--yes"], runner=runner)
    assert rc == 1
    assert not any(c[:1] == ["sbatch"] and "--test-only" not in c for c in runner.calls)

    runner = live_runner()
    rc = main(["submit", str(fixture("valid_gpu.sbatch"))], runner=runner)
    assert rc == 0
    assert not any(c[:1] == ["sbatch"] and "--test-only" not in c for c in runner.calls)

    runner = live_runner()
    path = str(fixture("valid_gpu.sbatch"))
    rc = main(["submit", path, "--yes"], runner=runner)
    assert rc == 0
    assert ["sbatch", path] in [c for c in runner.calls]


def test_test_only_caveat_diagnostic(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KIAC_SLURM_CACHE_DIR", str(tmp_path / "cache"))
    script = tmp_path / "h200.sbatch"
    script.write_text(
        "#!/bin/bash\n"
        "#SBATCH --partition=h200\n"
        "#SBATCH --account=chiru\n"
        "#SBATCH --qos=h200_qos\n"
        "#SBATCH --gres=gpu:1\n"
    )
    rc = main(["check", str(script), "--live"], runner=live_runner())
    out = capsys.readouterr().out
    assert rc == 0
    assert "LIVE002" in out
    assert "does NOT enforce" in out
