from _path import SRC  # noqa: F401

from helpers import check, fixture


def ids(rep):
    return rep.rule_ids()


def test_manual_example_is_not_blessed():
    """The manual's own example must fail: 16GB unit and 'general' partition."""
    rep = check(fixture("manual_basic.sbatch"))
    assert "SLURM021" in ids(rep)
    assert "KIAC011" in ids(rep)
    assert rep.exit_code() == 1
    mem = [d for d in rep.items if d.rule_id == "SLURM021"][0]
    assert mem.suggestion == "--mem=16G"
    assert mem.excerpt and "--mem=16GB" in mem.excerpt


def test_valid_gpu_script_passes_offline():
    rep = check(fixture("valid_gpu.sbatch"))
    assert rep.exit_code() == 0
    assert "SH001" in ids(rep)
    assert "KIAC010" in ids(rep)
    assert not rep.errors


def test_shell_syntax_error():
    rep = check(fixture("syntax_error.sbatch"))
    assert "SH001" in ids(rep)
    assert rep.exit_code() == 1


def test_directive_after_executable_line():
    rep = check(fixture("directive_after_code.sbatch"))
    assert "SLURM011" in ids(rep)
    assert rep.exit_code() == 1


def test_memory_options_conflict():
    rep = check(fixture("mem_conflict.sbatch"))
    assert "SLURM023" in ids(rep)


def test_shell_variable_in_directive():
    rep = check(fixture("var_directive.sbatch"))
    assert "SLURM010" in ids(rep)
    assert rep.exit_code() == 1


def test_invalid_array_spec():
    rep = check(fixture("bad_array.sbatch"))
    assert "SLURM050" in ids(rep)


def test_array_output_collision():
    rep = check(fixture("array_collision.sbatch"))
    assert "SLURM041" in ids(rep)
    # logs/ does not exist relative to the fixture dir; sbatch opens output
    # files at submit time, so this must be flagged even with %A/%a patterns
    fs = [d for d in rep.items if d.rule_id == "FS003"]
    assert fs and "mkdir -p logs" in fs[0].suggestion
    assert rep.exit_code() == 0  # both findings are warnings


def test_strict_escalates_warnings():
    rep = check(fixture("array_collision.sbatch"), strict=True)
    assert rep.exit_code() == 1
    escalated = [d for d in rep.items if d.rule_id == "SLURM041"]
    assert escalated and escalated[0].level == "ERROR" and escalated[0].escalated


def test_bad_directives_catch_multiple_rules():
    rep = check(fixture("bad_directives.sbatch"))
    found = set(ids(rep))
    assert {"SLURM004", "SLURM001", "SLURM020", "SLURM060", "SLURM070"} <= found


def test_unknown_percent_substitution():
    from kiac_slurm.diagnostics import Report
    from kiac_slurm.parser import parse_text
    from kiac_slurm.rules_generic import check_generic

    script = parse_text(
        "#!/bin/bash\n#SBATCH --output=logs/%q.out\n#SBATCH --partition=medium\n"
    )
    rep = Report()
    check_generic(script, rep)
    assert "SLURM040" in rep.rule_ids()


def test_json_report_shape():
    import json

    from kiac_slurm.report import render_json

    rep = check(fixture("manual_basic.sbatch"))
    payload = json.loads(render_json(rep, "manual_basic.sbatch"))
    # SLURM021 (16GB) and KIAC011 (general rejected offline) are both errors
    assert payload["summary"]["error"] >= 2
    assert payload["exit_code"] == 1
    rule_ids = {d["rule_id"] for d in payload["diagnostics"]}
    assert {"SLURM021", "KIAC011"} <= rule_ids
