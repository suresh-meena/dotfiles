from _path import SRC  # noqa: F401

from helpers import SITE, check, fixture
from kiac_slurm.config import _mini_yaml, load_site_config
from kiac_slurm.diagnostics import Report
from kiac_slurm.parser import parse_text
from kiac_slurm.rules_kiac import check_kiac


def ids(rep):
    return rep.rule_ids()


def test_site_config_loads_documented_uncertainty():
    assert set(SITE.partitions) == {"long", "short", "medium", "a100", "ada", "h200"}
    long_doc = SITE.partitions["long"]
    assert long_doc.status == "disputed"
    assert long_doc.max_times == [48 * 3600, 24 * 3600]
    assert long_doc.max_times_raw == ["48:00:00", "24:00:00"]
    ada = SITE.partitions["ada"]
    assert ada.nodes_raw == ["cn[7-9]"]
    assert ada.nodes == ["cn7", "cn8", "cn9"]
    h200 = SITE.partitions["h200"]
    assert h200.account_required is True
    assert h200.interactive_qos == "h200_qos"
    assert h200.batch_qos == "h200_qos"  # resolved by live verification
    assert SITE.gpu_catalog == ["A5000", "A6000", "A100", "ADA6000", "H200"]
    assert SITE.node_partition_map["cn10"] == "h200"
    assert SITE.preferred_storage == "/storage"
    assert SITE.assume_storage_writable is False


def test_site_config_verified_matrix():
    assert SITE.verified_as_of == "2026-09-14"
    a100 = SITE.verified_partition("a100")
    assert a100["allowed_accounts"] == ["research"]
    assert a100["denied_accounts"] == ["chiru"]
    h200 = SITE.verified_partition("h200")
    assert h200["required_qos"] == "h200_qos"
    assert SITE.verified_gres_types("medium") == ["a5000", "a6000", "ada6000"]
    assert SITE.verified_gres_types("a100") == ["a100"]
    assert SITE.verified.get("test_only_enforces_account_policy") is False
    assert SITE.verified_partition("general") is None


def test_mini_yaml_matches_shipped_config_shape():
    text = (SRC.parent / "config" / "kiac.yaml").read_text()
    data = _mini_yaml(text)
    assert data["cluster"] == "kiac"
    assert data["policy"]["preferred_storage"] == "/storage"
    assert data["partitions"]["long"]["status"] == "disputed"
    assert data["partitions"]["long"]["documented_nodes"] == ["cn1", "cn4"]
    assert data["partitions"]["ada"]["documented_nodes"] == ["cn[7-9]"]
    assert data["partitions"]["h200"]["account_required"] is True
    assert data["gpu_catalog_documented"] == ["A5000", "A6000", "A100", "ADA6000", "H200"]
    assert data["discover_gres_types_live"] is True
    verified = data["verified_live"]
    assert verified["as_of"] == "2026-09-14"
    assert verified["partitions"]["a100"]["allowed_accounts"] == ["research"]
    assert verified["partitions"]["h200"]["required_qos"] == "h200_qos"
    assert verified["accounts"] == ["research", "chiru"]


def test_mini_yaml_parity_with_pyyaml_if_present():
    text = (SRC.parent / "config" / "kiac.yaml").read_text()
    try:
        import yaml
    except ImportError:
        return
    assert _mini_yaml(text) == yaml.safe_load(text)


def test_h200_requires_account():
    rep = check(fixture("h200_no_account.sbatch"))
    assert "KIAC020" in ids(rep)
    diag = [d for d in rep.items if d.rule_id == "KIAC020"][0]
    assert "H200 jobs require an account." in diag.message
    assert diag.suggestion and "--account=" in diag.suggestion
    assert rep.exit_code() == 1


def test_account_partition_policy_from_verified_matrix():
    def run(part, account=None, qos=None):
        lines = [f"#SBATCH --partition={part}"]
        if account:
            lines.append(f"#SBATCH --account={account}")
        if qos:
            lines.append(f"#SBATCH --qos={qos}")
        script = parse_text("#!/bin/bash\n" + "\n".join(lines) + "\n")
        rep = Report()
        check_kiac(script, rep, SITE)
        return rep

    # the exact trap verified on the cluster: passes --test-only, pends forever
    bad = run("a100", "chiru")
    k23 = [d for d in bad.items if d.rule_id == "KIAC023"]
    assert k23 and k23[0].level == "ERROR"
    assert "pending" in k23[0].message.lower() or "test-only" in k23[0].message.lower()
    assert k23[0].suggestion == "use one of: research"

    ok = run("a100", "research")
    k23 = [d for d in ok.items if d.rule_id == "KIAC023"]
    assert k23 and k23[0].level == "PASS"

    # h200: account alone is not enough — the QOS is mandatory
    no_qos = run("h200", "chiru")
    k24 = [d for d in no_qos.items if d.rule_id == "KIAC024"]
    assert k24 and k24[0].level == "ERROR"
    assert k24[0].suggestion == "Add: #SBATCH --qos=h200_qos"

    wrong_qos = run("h200", "chiru", "normal")
    k24 = [d for d in wrong_qos.items if d.rule_id == "KIAC024"]
    assert k24 and k24[0].level == "ERROR" and "h200_qos" in k24[0].message

    good_h200 = run("h200", "chiru", "h200_qos")
    assert not [d for d in good_h200.items if d.level == "ERROR"]


def test_gpu_types_per_partition_verified():
    def run(part, gres):
        script = parse_text(
            "#!/bin/bash\n"
            f"#SBATCH --partition={part}\n"
            f"#SBATCH --gres=gpu:{gres}:1\n"
        )
        rep = Report()
        check_kiac(script, rep, SITE)
        return rep

    ok = run("a100", "a100")
    k51 = [d for d in ok.items if d.rule_id == "KIAC051"]
    assert k51 and k51[0].level == "PASS"

    wrong = run("a100", "h200")
    k51 = [d for d in wrong.items if d.rule_id == "KIAC051"]
    assert k51 and k51[0].level == "ERROR"

    medium_ok = run("medium", "ada6000")
    assert [d for d in medium_ok.items if d.rule_id == "KIAC051"][0].level == "PASS"
    medium_bad = run("medium", "h200")
    assert [d for d in medium_bad.items if d.rule_id == "KIAC051"][0].level == "ERROR"


def test_gpu_catalog_fallback_without_verified_matrix():
    from copy import deepcopy

    plain = deepcopy(SITE)
    plain.verified = {}
    script = parse_text(
        "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --gres=gpu:a100:1\n"
    )
    rep = Report()
    check_kiac(script, rep, plain)
    assert "KIAC050" in rep.rule_ids()  # catalog fallback still works


def test_time_exceeding_all_documented_limits():
    rep = check(fixture("bad_time.sbatch"))
    assert "KIAC030" in ids(rep)
    assert rep.exit_code() == 1


def test_disputed_time_window_warns_not_errors():
    rep = check(fixture("time_disputed.sbatch"))
    assert "KIAC031" in ids(rep)
    conflict = [d for d in rep.items if d.rule_id == "KIAC031"][0]
    assert conflict.confidence == "document-conflict"
    assert rep.exit_code() == 0


def test_storage_recommendation(tmp_path):
    import os

    home = os.path.expanduser("~")
    script_path = tmp_path / "home_job.sbatch"
    script_path.write_text(
        "#!/bin/bash\n"
        "#SBATCH --partition=medium\n"
        f"#SBATCH --chdir={home}\n"
    )
    rep = check(script_path)
    storage = [d for d in rep.items if d.rule_id == "KIAC040"]
    assert storage and storage[0].level == "WARN"
    assert "/storage" in storage[0].message


def test_nodelist_partition_mismatch():
    script = parse_text(
        "#!/bin/bash\n#SBATCH --partition=long\n#SBATCH --nodelist=cn10\n"
    )
    rep = Report()
    check_kiac(script, rep, SITE)
    assert "KIAC060" in rep.rule_ids()


def test_missing_partition_is_an_error():
    script = parse_text("#!/bin/bash\n#SBATCH --mem=4G\n")
    rep = Report()
    check_kiac(script, rep, SITE)
    assert "KIAC001" in rep.rule_ids()
    assert rep.exit_code() == 1


def test_unknown_partition_rejected_offline():
    """The manual's `general` must not pass an offline check (spec section 1)."""
    rep = check(fixture("manual_basic.sbatch"))
    k11 = [d for d in rep.items if d.rule_id == "KIAC011"]
    assert k11 and k11[0].level == "ERROR"


def test_load_site_config_explicit_path():
    site = load_site_config(str(SRC.parent / "config" / "kiac.yaml"))
    assert site.cluster_name == "kiac"
