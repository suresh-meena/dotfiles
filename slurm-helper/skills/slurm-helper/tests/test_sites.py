"""Multi-site support: the AMD MI210 cluster alongside KIAC."""

from _path import SRC  # noqa: F401

from helpers import SITE
from kiac_slurm import learn
from kiac_slurm.cli import main
from kiac_slurm.config import load_site_config
from kiac_slurm.diagnostics import Report
from kiac_slurm.parser import parse_text
from kiac_slurm.rules_kiac import check_kiac

AMD = load_site_config(site="amd")


def ids(rep):
    return rep.rule_ids()


def run_amd(text):
    script = parse_text(text)
    rep = Report()
    check_kiac(script, rep, AMD)
    return rep


def test_amd_config_loads():
    assert AMD.cluster_name == "amd"
    assert AMD.rule_prefix == "AMD"
    assert AMD.gpu_catalog == ["MI210"]
    assert AMD.gpu_vendor == "amd"
    assert AMD.gpu_jobs_only_partitions == ["GPU", "jobgn01"]
    assert AMD.partitions["GPU"].status == "example-only"
    assert AMD.partitions["GPU"].nodes == ["gn01", "gn02", "gn03"]
    assert AMD.verified_partition("GPU") is None
    assert AMD.home_path_prefix == "/rhome"
    assert AMD.preferred_storage == "/scratch"
    assert "WEEKLY" in (AMD.preferred_storage_caveat or "")


def test_kiac_prefix_unchanged():
    assert SITE.rule_prefix == "KIAC"


def test_amd_unknown_partition_uses_amd_prefix():
    rep = run_amd("#!/bin/bash\n#SBATCH --partition=cpuq\n#SBATCH --mem=4G\n")
    assert "AMD011" in ids(rep)
    assert rep.exit_code() == 1


def test_amd_example_only_partition_gets_info_not_silence():
    rep = run_amd("#!/bin/bash\n#SBATCH --partition=GPU\n#SBATCH --gres=gpu:1\n")
    a10 = [d for d in rep.items if d.rule_id == "AMD010"]
    assert any(d.level == "PASS" for d in a10)
    assert any(d.level == "INFO" and "example" in d.message.lower() for d in a10)


def test_amd_gpu_catalog_check():
    ok = run_amd("#!/bin/bash\n#SBATCH --partition=GPU\n#SBATCH --gres=gpu:mi210:2\n")
    a50 = [d for d in ok.items if d.rule_id == "AMD050"]
    assert a50 and a50[0].level == "PASS"
    bad = run_amd("#!/bin/bash\n#SBATCH --partition=GPU\n#SBATCH --gres=gpu:h200:1\n")
    a50 = [d for d in bad.items if d.rule_id == "AMD050"]
    assert a50 and a50[0].level == "WARN"


def test_amd_gpu_only_queue_policy():
    cpu_job = run_amd(
        "#!/bin/bash\n#SBATCH --partition=GPU\n#SBATCH --ntasks=1\n#SBATCH --mem=4G\n"
        "python3 cpu_only.py\n"
    )
    a70 = [d for d in cpu_job.items if d.rule_id == "AMD070"]
    assert a70 and a70[0].level == "ERROR"
    assert "monitored" in a70[0].message

    gpu_job = run_amd(
        "#!/bin/bash\n#SBATCH --partition=GPU\n#SBATCH --gres=gpu:1\n"
        "python3 train.py\n"
    )
    assert "AMD070" not in ids(gpu_job)


def test_amd_nvidia_smi_rejected():
    """The manual's own example runs nvidia-smi on MI210 hardware."""
    rep = run_amd(
        "#!/bin/bash\n#SBATCH --partition=GPU\n#SBATCH --gres=gpu:1\n"
        "pwd; hostname; date | tee result\nnvidia-smi\n"
    )
    a71 = [d for d in rep.items if d.rule_id == "AMD071"]
    assert a71 and a71[0].level == "ERROR"
    assert "rocm-smi" in a71[0].suggestion
    assert a71[0].line is not None


def test_amd_storage_semantics():
    home_script = run_amd(
        "#!/bin/bash\n#SBATCH --partition=GPU\n#SBATCH --gres=gpu:1\n"
        "cd /rhome/alice/wanda\npython3 main.py\n"
    )
    a40 = [d for d in home_script.items if d.rule_id == "AMD040"]
    assert a40 and a40[0].level == "WARN" and "20 GB" in a40[0].message

    scratch = run_amd(
        "#!/bin/bash\n#SBATCH --partition=GPU\n#SBATCH --gres=gpu:1\n"
        "cd /scratch/alice/data\npython3 main.py\n"
    )
    a40 = [d for d in scratch.items if d.rule_id == "AMD040"]
    assert a40 and a40[0].level == "PASS" and "WEEKLY" in a40[0].message


def test_site_selection_cli(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("KIAC_SLURM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("KIAC_SLURM_OBSERVATIONS", str(tmp_path / "obs.jsonl"))
    script = tmp_path / "cpuq.sbatch"
    script.write_text("#!/bin/bash\n#SBATCH --partition=cpuq\n#SBATCH --mem=4G\n")
    rc = main(["--site", "amd", "check", str(script)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "AMD011" in out

    # env var works too
    monkeypatch.setenv("KIAC_SLURM_SITE", "amd")
    rc = main(["check", str(script)])
    assert "AMD011" in capsys.readouterr().out

    # kiac default still uses the KIAC prefix for the same file
    monkeypatch.delenv("KIAC_SLURM_SITE")
    rc = main(["check", str(script)])
    assert "KIAC011" in capsys.readouterr().out


def test_h200_template_guard_on_amd(capsys):
    rc = main(["--site", "amd", "new", "-t", "h200"])
    assert rc == 2
    assert "KIAC-specific" in capsys.readouterr().err


def test_observations_and_cache_are_per_site(tmp_path, monkeypatch):
    monkeypatch.delenv("KIAC_SLURM_OBSERVATIONS", raising=False)
    monkeypatch.delenv("KIAC_SLURM_CACHE_DIR", raising=False)
    monkeypatch.delenv("KIAC_SLURM_SITE", raising=False)
    assert learn.observations_path("amd").name == "observations.amd.jsonl"
    assert learn.observations_path().name == "observations.kiac.jsonl"
    monkeypatch.setenv("KIAC_SLURM_SITE", "amd")
    assert learn.observations_path().name == "observations.amd.jsonl"

    from kiac_slurm.live import default_cache_dir

    assert default_cache_dir("amd").name == "amd"
    monkeypatch.delenv("KIAC_SLURM_SITE")
    assert default_cache_dir().name == "kiac"


def test_amd_learn_apply_roundtrip(tmp_path, monkeypatch, capsys):
    """Policy evidence recorded under site=amd lands in amd.yaml only."""
    monkeypatch.setenv("KIAC_SLURM_LEARN", "1")
    monkeypatch.setenv("KIAC_SLURM_SITE", "amd")
    monkeypatch.setenv("KIAC_SLURM_OBSERVATIONS", str(tmp_path / "obs.jsonl"))
    monkeypatch.setenv("KIAC_SLURM_CACHE_DIR", str(tmp_path / "cache"))
    config_copy = tmp_path / "amd.yaml"
    config_copy.write_text((SRC.parent / "config" / "amd.yaml").read_text())
    monkeypatch.setenv("KIAC_SLURM_CONFIG", str(config_copy))

    assert learn.record({
        "kind": "qos-required", "subject": "GPU", "observed": "debug_qos",
        "detail": "from a pending job reason",
    }, site_name="amd")

    from test_live import FakeRunner

    # FakeRunner with no reachable scheduler: apply falls back to log evidence
    rc = main(["learn", "apply", "--yes"], runner=FakeRunner())
    assert rc == 0
    site = load_site_config(str(config_copy))
    assert site.verified_partition("GPU").get("required_qos") == "debug_qos"
    assert site.verified_as_of  # bumped
    assert learn.BEGIN_MARKER in config_copy.read_text()
    # a stray KIAC_SLURM_CONFIG pointing at another site's file must be a
    # loud error when --site kiac is explicit, never a silent site swap
    import pytest

    from kiac_slurm.config import ConfigError

    with pytest.raises(ConfigError, match="--site kiac"):
        load_site_config(site="kiac")
    monkeypatch.delenv("KIAC_SLURM_CONFIG")
    kiac = load_site_config(site="kiac")
    assert kiac.verified_partition("GPU") is None  # untouched by amd learning
