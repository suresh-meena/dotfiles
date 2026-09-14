from _path import SRC  # noqa: F401

from kiac_slurm.parser import (
    expand_hostlist,
    fmt_time,
    has_shell_var,
    parse_memory,
    parse_text,
    parse_time,
)


def test_shebang_and_directives():
    script = parse_text(
        "#!/bin/bash\n"
        "#SBATCH --job-name=a\n"
        "#SBATCH --partition=long\n"
        "\n"
        "echo hi\n",
        path="mem",
    )
    assert script.shebang == "#!/bin/bash"
    assert script.get("--job-name") == "a"
    assert script.get("--partition") == "long"
    assert script.body_lines and script.body_lines[0][1] == "echo hi"
    assert not script.ignored


def test_directive_after_code_is_ignored():
    script = parse_text(
        "#!/bin/bash\n"
        "#SBATCH --mem=4G\n"
        "echo start\n"
        "#SBATCH --mem=8G\n",
    )
    assert script.get("--mem") == "4G"
    assert len(script.ignored) == 1
    assert script.ignored[0].value == "8G"


def test_comments_do_not_stop_header():
    script = parse_text(
        "#!/bin/bash\n"
        "# a plain comment\n"
        "#SBATCH --mem=4G\n"
        "\n"
        "true\n",
    )
    assert script.get("--mem") == "4G"
    assert not script.ignored


def test_short_options_and_space_form():
    script = parse_text(
        "#!/bin/bash\n"
        "#SBATCH -p long\n"
        "#SBATCH --time 01:00:00\n"
        "#SBATCH -J myjob\n",
    )
    assert script.get("--partition") == "long"
    assert script.get("--time") == "01:00:00"
    assert script.get("--job-name") == "myjob"


def test_attached_short_value():
    script = parse_text("#!/bin/bash\n#SBATCH -p2\n#SBATCH -Jtrain\n")
    assert script.get("--partition") == "2"
    assert script.get("--job-name") == "train"


def test_valueless_option_does_not_absorb_next_token():
    script = parse_text("#!/bin/bash\n#SBATCH --hold\n#SBATCH --requeue foo\n")
    assert script.get("--hold") is None
    assert script.get("--requeue") is None
    # 'foo' must survive as a bare token (flagged SLURM002), not be eaten
    assert "foo" in [d.option for d in script.directives]


def test_canonical_memory_case_insensitive():
    from kiac_slurm.parser import canonical_memory

    assert canonical_memory("16GB") == "16G"
    assert canonical_memory("16gb") == "16G"
    assert canonical_memory("512Mb") == "512M"
    assert canonical_memory("16G") is None
    assert canonical_memory("junk") is None


def test_duplicates_are_grouped_last_wins():
    script = parse_text(
        "#!/bin/bash\n"
        "#SBATCH --partition=long\n"
        "#SBATCH --partition=short\n",
    )
    assert script.get("--partition") == "short"
    assert len(script.get_all("--partition")) == 2


def test_shell_var_detection():
    assert has_shell_var("${MEM}")
    assert has_shell_var("$MEM")
    assert has_shell_var("$(x)")
    assert not has_shell_var("16G")
    assert not has_shell_var("logs/%x_%j.out")


def test_parse_time():
    assert parse_time("30") == 30 * 60
    assert parse_time("30:00") == 30 * 60
    assert parse_time("01:00:00") == 3600
    assert parse_time("48:00:00") == 48 * 3600
    assert parse_time("1-12:00:00") == 36 * 3600
    assert parse_time("-1") == -1
    assert parse_time("infinite") == -1
    assert parse_time("junk") is None
    assert parse_time("1:2:3:4") is None
    assert parse_time("") is None


def test_fmt_time_roundtrip():
    # days>0 renders D-HH:MM:SS, matching Slurm's own secs2time_str
    assert fmt_time(172800) == "2-00:00:00"
    assert fmt_time(3600) == "01:00:00"
    assert fmt_time(-1) == "infinite"
    assert fmt_time(None) == "unknown"
    assert fmt_time(90000) == "1-01:00:00"


def test_parse_memory():
    assert parse_memory("16G") == (16 * 1024, None)
    assert parse_memory("16GB")[1] == "B-suffix"
    assert parse_memory("16B")[1] == "B-suffix"
    assert parse_memory("512") == (512, None)
    assert parse_memory("0") == (0, None)
    assert parse_memory("16X")[1] == "invalid"
    assert parse_memory("-4G")[1] == "invalid"


def test_expand_hostlist():
    assert expand_hostlist("cn[7-9]") == ["cn7", "cn8", "cn9"]
    assert expand_hostlist("cn[1,4]") == ["cn1", "cn4"]
    assert expand_hostlist("cn10") == ["cn10"]
    assert expand_hostlist("") == []
    assert expand_hostlist("cn[01-03]") == ["cn01", "cn02", "cn03"]
