from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "install-helper"
HUB = ROOT / "scripts" / "install-hub"
BUILD = ROOT / "scripts" / "build-helper-wheel"
HELPER_LOCK = ROOT / "requirements-helper.lock"
PYPROJECT = ROOT / "pyproject.toml"
UNIT_SOURCE = ROOT / "packaging" / "fleetmon-hub.service"


def fake_executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o700)
    return path


def project_version() -> str:
    match = re.search(r'(?m)^version = "([^"]+)"$', PYPROJECT.read_text())
    assert match is not None
    return match.group(1)


def helper_lock_pins() -> list[str]:
    pins: list[str] = []
    for raw in HELPER_LOCK.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "==" in line:
            pins.append(line)
    return sorted(pins)


def build_helper_wheel(tmp_path: Path, out: str = "wheel-out") -> tuple[Path, str]:
    out_dir = tmp_path / out
    result = subprocess.run(
        [sys.executable, str(BUILD), "--out", str(out_dir)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    wheel = out_dir / f"fleetmon-{project_version()}-py3-none-any.whl"
    assert wheel.is_file()
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert f"sha256: {digest}" in result.stdout
    sidecar = Path(f"{wheel}.sha256")
    assert sidecar.read_text().split()[0] == digest
    return wheel, digest


# Fake fleetctl emulating the verified local contract: `show --json NAME`,
# `protocol show --json NAME`, `sync push --target NAME <local> <remote>`,
# and `exec --target NAME -- argv...` (plain argv vectors, no shell). Every
# executed vector is logged verbatim so tests can assert the exact flow.
AUTHORIZED_FLEETCTL_BODY = r"""
log() { printf '%s' "$*" | tr '\n' ' ' >> "$FLEETCTL_FAKE_LOG"; printf '\n' >> "$FLEETCTL_FAKE_LOG"; }
log "$*"
case "$1" in
  show)
    if [ "$2" != "--json" ]; then exit 1; fi
    case "$3" in
      ada1) printf '%s\n' '{"enabled":true,"name":"ada1","protocol":"direct","role":"compute"}' ;;
      loginhost) printf '%s\n' '{"enabled":true,"name":"loginhost","protocol":"slurm","role":"login"}' ;;
      disabledhost) printf '%s\n' '{"enabled":false,"name":"disabledhost","protocol":"direct","role":"compute"}' ;;
      bridgehost) printf '%s\n' '{"enabled":true,"name":"bridgehost","protocol":"route-proto","role":"workstation"}' ;;
      *) exit 1 ;;
    esac
    ;;
  protocol)
    if [ "$2" != "show" ] || [ "$3" != "--json" ]; then exit 1; fi
    case "$4" in
      direct) printf '%s\n' '{"kind":"direct"}' ;;
      route-proto) printf '%s\n' '{"kind":"bridge"}' ;;
      *) exit 1 ;;
    esac
    ;;
  sync)
    if [ "$2" != "push" ] || [ "$3" != "--target" ]; then exit 1; fi
    if [ ! -f "$5" ]; then exit 1; fi
    ;;
  exec)
    if [ "$2" != "--target" ] || [ "$4" != "--" ]; then exit 1; fi
    case "$5" in
      sh)
        if [ "$6" != "-c" ] || [[ "$7" != *"fleetmon: find-python"* ]]; then exit 1; fi
        if [ -n "${FLEETMON_FAKE_NO_PYTHON:-}" ]; then exit 4; fi
        printf '%s\n' "$REMOTE_HOME/.local/bin/python3.12"
        ;;
      "$REMOTE_HOME"/.local/bin/python3.12 | */bin/python3.12)
        if [ "$6" = "-m" ]; then
          if [ "$7" != "venv" ]; then exit 1; fi
          exit 0
        fi
        if [ "$6" != "-c" ]; then exit 1; fi
        case "$7" in
          *"fleetmon: probe"*)
            printf '%s\n' '{"major":3,"minor":11,"uid":1000,"home":"'"$REMOTE_HOME"'"}'
            ;;
          *"fleetmon: makedirs"*)
            mkdir -p "$8" "$9"
            ;;
          *"fleetmon: clear-version-dir"*)
            mkdir -p "$(dirname "$9")"
            ;;
          *"fleetmon: stage-hash"*)
            printf '%s\n' '{"sha256":"'"$FLEETMON_FAKE_REMOTE_HASH"'"}'
            ;;
          *"fleetmon: previous"*)
            printf '%s\n' '{"previous": null}'
            ;;
          *"fleetmon: switch"*)
            mkdir -p "$(dirname "$8")"
            ln -sfn "$9" "$8"
            ;;
          *"fleetmon: verify"*)
            if [ "${FLEETMON_FAKE_VERIFY:-ok}" != "ok" ]; then printf '%s\n' 'bad'; exit 1; fi
            printf '%s\n' 'ok'
            ;;
          *"rmtree"* | *"import shutil, sys"*)
            rm -rf -- "$8"
            ;;
          *"fleetmon: rollback"*)
            rm -f -- "$8.fleetmon-tmp"
            if [ -z "$9" ]; then
              rm -f -- "$8"
            else
              ln -sfn "$9" "$8"
            fi
            printf '%s\n' 'rolled back'
            ;;
          *) exit 1 ;;
        esac
        ;;
      */bin/python)
        if [ "$6" != "-m" ] || [ "$7" != "pip" ] || [ "$8" != "install" ]; then exit 1; fi
        ;;
      */fleetmon-snapshot)
        printf '%s\n' '{"schema_version":1,"status":"ok","helper_version":"1","captured_at":"2026-09-04T00:00:00Z","limits":{"truncated":false}}'
        ;;
      *) exit 1 ;;
    esac
    ;;
  *) exit 1 ;;
esac
"""


def authorized_env(tmp_path: Path, remote_home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "FLEETCTL": str(tmp_path / "fleetctl"),
        "FLEETCTL_FAKE_LOG": str(tmp_path / "fake.log"),
        "REMOTE_HOME": str(remote_home),
        "FLEETMON_FAKE_REMOTE_HASH": "",
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }


def fake_log_lines(env: dict[str, str]) -> list[str]:
    return [
        line.rstrip()
        for line in Path(env["FLEETCTL_FAKE_LOG"]).read_text().splitlines()
    ]


def fake_hub_python(path: Path) -> Path:
    return fake_executable(
        path,
        r"""
if [ "$1" = "-c" ]; then
  if [ "$2" = 'import sys; print(sys.version_info[0], sys.version_info[1])' ]; then
    printf '%s\n' '3 11'
  else
    exec python3 -c "$2"
  fi
elif [ "$1" = "--version" ]; then
  printf '%s\n' 'Python 3.11.0'
elif [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
  printf 'venv %s\n' "$3" >> "$FLEETMON_FAKE_LOG"
  cat >"$3/bin/fleetmon" <<'EOF'
#!/bin/sh
if [ "$1" = maintain ]; then
  printf '%s\n' "$1 $2 $3 $4" >> "$FLEETMON_FAKE_LOG"
  case "${FLEETMON_FAKE_BACKUP:-ok}" in
    ok) printf '%s\n' '{"ok": true, "path": "/backup/fleet.db.bak"}' ;;
    notok) printf '%s\n' '{"ok": false}' ;;
    *) exit 1 ;;
  esac
  exit 0
fi
exit 0
EOF
  chmod 700 "$3/bin/fleetmon"
else
  exit 0
fi
""",
    )


def fake_loginctl(path: Path, linger: str) -> Path:
    return fake_executable(
        path,
        'if [ "$1" = show-user ]; then\n'
        f"  printf '%s\\n' 'Linger={linger}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )


def make_home(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    home.mkdir()
    fake_hub_python(tmp_path / "python")
    fake_executable(tmp_path / "systemctl", "exit 0\n")
    env = {
        **os.environ,
        "HOME": str(home),
        "FLEETMON_PYTHON": str(tmp_path / "python"),
        "FLEETMON_FAKE_LOG": str(tmp_path / "fake.log"),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }
    return home, env


def run(
    script: Path, *args: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


# --- fleetmon helper wheel builder -----------------------------------------


def test_build_wheel_is_reproducible_byte_identical(tmp_path: Path) -> None:
    first, first_digest = build_helper_wheel(tmp_path, "out-1")
    second, second_digest = build_helper_wheel(tmp_path, "out-2")

    assert first.read_bytes() == second.read_bytes()
    assert first_digest == second_digest


def test_build_wheel_version_tracks_pyproject_single_source_of_truth(
    tmp_path: Path,
) -> None:
    version = project_version()
    wheel, digest = build_helper_wheel(tmp_path)

    assert wheel.name == f"fleetmon-{version}-py3-none-any.whl"
    sidecar = Path(f"{wheel}.sha256")
    assert sidecar.read_text() == f"{digest}  {wheel.name}\n"


def test_build_wheel_members_are_bounded_and_self_consistent(tmp_path: Path) -> None:
    wheel, _ = build_helper_wheel(tmp_path)
    version = project_version()
    dist = f"fleetmon-{version}.dist-info"
    expected = [
        "fleetmon/__init__.py",
        "fleetmon/protocol.py",
        "fleetmon/snapshot.py",
        f"{dist}/METADATA",
        f"{dist}/WHEEL",
        f"{dist}/entry_points.txt",
        f"{dist}/RECORD",
    ]

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert names == expected
        for name in names:
            assert archive.getinfo(name).date_time == (2000, 1, 1, 0, 0, 0)
        record = archive.read(f"{dist}/RECORD").decode("utf-8").splitlines()
        assert record[-1] == f"{dist}/RECORD,,"
        assert len(record) == len(names)
        for line, name in zip(record, names, strict=True):
            file_name, entry, size = line.split(",")
            assert file_name == name
            if entry:
                assert int(size) == len(archive.read(name))
                encoded = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(archive.read(name)).digest()
                    )
                    .rstrip(b"=")
                    .decode("ascii")
                )
                assert entry == f"sha256={encoded}"
        metadata = archive.read(f"{dist}/METADATA").decode("utf-8")
        assert "Name: fleetmon" in metadata
        assert f"Version: {version}" in metadata
        assert "Requires-Python: >=3.10" in metadata
        assert metadata.splitlines()[-len(helper_lock_pins()) :][0] == (
            f"Requires-Dist: {helper_lock_pins()[0]}"
        )
        wheel_meta = archive.read(f"{dist}/WHEEL").decode("utf-8")
        assert "Wheel-Version: 1.0" in wheel_meta
        assert "Tag: py3-none-any" in wheel_meta
        entry_points = archive.read(f"{dist}/entry_points.txt").decode("utf-8")
        assert "fleetmon-snapshot = fleetmon.snapshot:main" in entry_points


def test_build_wheel_requires_dist_tracks_helper_lock(tmp_path: Path) -> None:
    wheel, _ = build_helper_wheel(tmp_path)
    version = project_version()
    with zipfile.ZipFile(wheel) as archive:
        metadata = archive.read(f"fleetmon-{version}.dist-info/METADATA").decode(
            "utf-8"
        )
    assert [
        line for line in metadata.splitlines() if line.startswith("Requires-Dist: ")
    ] == [f"Requires-Dist: {pin}" for pin in helper_lock_pins()]


def test_build_wheel_check_mode_ok_and_failure_paths(tmp_path: Path) -> None:
    wheel, digest = build_helper_wheel(tmp_path)
    wrong = "0" * 64

    sidecar_ok = subprocess.run(
        [sys.executable, str(BUILD), "--check", str(wheel)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert sidecar_ok.returncode == 0, sidecar_ok.stderr
    assert "check ok" in sidecar_ok.stdout

    expect_ok = subprocess.run(
        [sys.executable, str(BUILD), "--check", str(wheel), "--expect-sha256", digest],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert expect_ok.returncode == 0, expect_ok.stderr

    expect_bad = subprocess.run(
        [sys.executable, str(BUILD), "--check", str(wheel), "--expect-sha256", wrong],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert expect_bad.returncode == 1
    assert "check failed" in expect_bad.stderr
    assert digest in expect_bad.stderr

    sidecar_bad = subprocess.run(
        [sys.executable, str(BUILD), "--check", str(wheel)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ},
    )
    assert sidecar_bad.returncode == 0  # sidecar still holds the true digest
    sidecar = Path(f"{wheel}.sha256")
    sidecar.write_text(f"{wrong}  {wheel.name}\n")
    sidecar_bad = subprocess.run(
        [sys.executable, str(BUILD), "--check", str(wheel)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert sidecar_bad.returncode == 1
    assert "check failed" in sidecar_bad.stderr


def test_build_wheel_rejects_version_drift(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "src" / "fleetmon").mkdir(parents=True)
    shutil.copy2(PYPROJECT, source / "pyproject.toml")
    shutil.copy2(HELPER_LOCK, source / "requirements-helper.lock")
    for module in ("__init__.py", "protocol.py", "snapshot.py"):
        shutil.copy2(
            ROOT / "src" / "fleetmon" / module, source / "src" / "fleetmon" / module
        )
    init = source / "src" / "fleetmon" / "__init__.py"
    version = project_version()
    drifted = "9.9.9"
    assert version != drifted
    init.write_text(init.read_text().replace(version, drifted))

    result = subprocess.run(
        [
            sys.executable,
            str(BUILD),
            "--source",
            str(source),
            "--out",
            str(tmp_path / "out"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "disagree" in result.stderr


# --- fleetmon helper installer ----------------------------------------------


def test_helper_dry_run_executes_nothing_on_hostile_path_fleetctl(
    tmp_path: Path,
) -> None:
    canary = tmp_path / "canary-fired"
    fake_executable(tmp_path / "fleetctl", f"touch {canary}\nexit 1\n")
    # No FLEETCTL override: the canary on PATH must not be executed at all.
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = run(HELPER, "--dry-run", "--target", "ada1", env=env)

    assert result.returncode == 0, result.stderr
    assert "performs no fleetctl invocation" in result.stdout
    assert "dry-run: target=ada1" in result.stdout
    assert "would resolve admission in real mode" in result.stdout
    assert not canary.exists()


def test_helper_dry_run_validates_local_wheel_read_only(tmp_path: Path) -> None:
    canary = tmp_path / "canary-fired"
    fake_executable(tmp_path / "fleetctl", f"touch {canary}\nexit 1\n")
    wheel, digest = build_helper_wheel(tmp_path)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = run(
        HELPER, "--dry-run", "--target", "ada1", "--wheel", str(wheel), env=env
    )

    assert result.returncode == 0, result.stderr
    assert f"dry-run: wheel={wheel}" in result.stdout
    assert f"dry-run: local sha256={digest} matches the recorded hash" in result.stdout
    assert not canary.exists()


def test_helper_dry_run_refuses_bad_target_shapes(tmp_path: Path) -> None:
    canary = tmp_path / "canary-fired"
    fake_executable(tmp_path / "fleetctl", f"touch {canary}\nexit 1\n")
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    bad_targets = (
        "all",
        "ada1 ada2",
        "ada1\tb",
        "ada1\x01b",
        "-x",
        "",
        "ada1\x7f",
        "ada1\xa0",
    )

    for bad in bad_targets:
        result = run(HELPER, "--dry-run", "--target", bad, env=env)

        assert result.returncode == 2, (bad, result.stderr)
        expected = (
            "refused: all_selector_refused"
            if bad == "all"
            else "refused: target_shape_invalid"
        )
        assert expected in result.stderr, (bad, result.stderr)
        assert not canary.exists()


def test_helper_refuses_selector_flags_and_malformed_invocations(
    tmp_path: Path,
) -> None:
    canary = tmp_path / "canary-fired"
    fake_executable(tmp_path / "fleetctl", f"touch {canary}\nexit 1\n")
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    bad_invocations = (
        (),
        ("ada1",),
        ("--dry-run",),
        ("--target",),
        ("--target", "ada1", "--target", "bob"),
        ("--dry-run", "--target", "ada1", "--tag", "x"),
        ("--dry-run", "--target", "ada1", "--all"),
        ("--dry-run", "ada1"),
    )

    for invocation in bad_invocations:
        result = run(HELPER, *invocation, env=env)

        assert result.returncode == 2, (invocation, result.stderr)
        assert not canary.exists()


def test_helper_dry_run_refuses_hash_mismatch_read_only(tmp_path: Path) -> None:
    canary = tmp_path / "canary-fired"
    fake_executable(tmp_path / "fleetctl", f"touch {canary}\nexit 1\n")
    wheel, _ = build_helper_wheel(tmp_path)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = run(
        HELPER,
        "--dry-run",
        "--target",
        "ada1",
        "--wheel",
        str(wheel),
        "--expect-sha256",
        "0" * 64,
        env=env,
    )

    assert result.returncode == 4
    assert "refused: artifact_hash_mismatch" in result.stderr
    assert not canary.exists()


def test_helper_real_install_refused_without_authorization_flag(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "fleetctl-called"
    fake_executable(tmp_path / "fleetctl", f"touch {marker}\nexit 0\n")
    env = {
        **os.environ,
        "FLEETCTL": str(tmp_path / "fleetctl"),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    result = run(HELPER, "--target", "ada1", env=env)

    assert result.returncode == 3
    assert "refused: install_requires_explicit_authorization_flag" in result.stderr
    assert "not enabled" in result.stderr
    assert "--i-authorize-target-ada1" in result.stderr
    assert not marker.exists()


def test_helper_authorization_flag_target_mismatch_refused(tmp_path: Path) -> None:
    marker = tmp_path / "fleetctl-called"
    fake_executable(tmp_path / "fleetctl", f"touch {marker}\nexit 0\n")
    env = {
        **os.environ,
        "FLEETCTL": str(tmp_path / "fleetctl"),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    result = run(HELPER, "--target", "bob", "--i-authorize-target-ada1", env=env)

    assert result.returncode == 3
    assert "refused: authorization_flag_target_mismatch" in result.stderr
    assert not marker.exists()


def test_helper_authorized_refuses_missing_wheel_before_any_fleetctl(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "fleetctl-called"
    fake_executable(tmp_path / "fleetctl", f"touch {marker}\nexit 0\n")
    env = {
        **os.environ,
        "FLEETCTL": str(tmp_path / "fleetctl"),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    result = run(
        HELPER,
        "--target",
        "ada1",
        "--wheel",
        str(tmp_path / "missing.whl"),
        "--i-authorize-target-ada1",
        env=env,
    )

    assert result.returncode == 2
    assert "refused: artifact_not_found" in result.stderr
    assert not marker.exists()


def test_helper_refuses_hash_mismatch_before_any_fleetctl(tmp_path: Path) -> None:
    marker = tmp_path / "fleetctl-called"
    fake_executable(tmp_path / "fleetctl", f"touch {marker}\nexit 0\n")
    wheel, _ = build_helper_wheel(tmp_path)
    env = {
        **os.environ,
        "FLEETCTL": str(tmp_path / "fleetctl"),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    result = run(
        HELPER,
        "--target",
        "ada1",
        "--wheel",
        str(wheel),
        "--expect-sha256",
        "0" * 64,
        "--i-authorize-target-ada1",
        env=env,
    )

    assert result.returncode == 4
    assert "refused: artifact_hash_mismatch" in result.stderr
    assert not marker.exists()


def test_helper_authorized_refuses_wheel_without_recorded_hash(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "fleetctl-called"
    fake_executable(tmp_path / "fleetctl", f"touch {marker}\nexit 0\n")
    wheel, _ = build_helper_wheel(tmp_path)
    Path(f"{wheel}.sha256").unlink()
    env = {
        **os.environ,
        "FLEETCTL": str(tmp_path / "fleetctl"),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    result = run(
        HELPER,
        "--target",
        "ada1",
        "--wheel",
        str(wheel),
        "--i-authorize-target-ada1",
        env=env,
    )

    assert result.returncode == 2
    assert "refused: artifact_hash_record_missing" in result.stderr
    assert not marker.exists()


def test_helper_authorized_refuses_unexpected_wheel_name(tmp_path: Path) -> None:
    marker = tmp_path / "fleetctl-called"
    fake_executable(tmp_path / "fleetctl", f"touch {marker}\nexit 0\n")
    wheel, _ = build_helper_wheel(tmp_path)
    renamed = tmp_path / "evil-payload.whl"
    renamed.write_bytes(wheel.read_bytes())
    env = {
        **os.environ,
        "FLEETCTL": str(tmp_path / "fleetctl"),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    result = run(
        HELPER,
        "--target",
        "ada1",
        "--wheel",
        str(renamed),
        "--i-authorize-target-ada1",
        env=env,
    )

    assert result.returncode == 2
    assert "refused: artifact_unexpected_name" in result.stderr
    assert not marker.exists()


def test_helper_authorized_refuses_login_role_before_any_transfer(
    tmp_path: Path,
) -> None:
    fake_executable(tmp_path / "fleetctl", AUTHORIZED_FLEETCTL_BODY)
    wheel, digest = build_helper_wheel(tmp_path)
    env = authorized_env(tmp_path, tmp_path / "remotehome")
    env["FLEETMON_FAKE_REMOTE_HASH"] = digest

    result = run(
        HELPER,
        "--target",
        "loginhost",
        "--wheel",
        str(wheel),
        "--i-authorize-target-loginhost",
        env=env,
    )

    assert result.returncode == 3
    assert "refused: target_not_admitted" in result.stderr
    assert "not an enabled workstation/compute target" in result.stderr
    assert fake_log_lines(env) == ["show --json loginhost"]


def test_helper_authorized_refuses_disabled_target_before_any_transfer(
    tmp_path: Path,
) -> None:
    fake_executable(tmp_path / "fleetctl", AUTHORIZED_FLEETCTL_BODY)
    wheel, digest = build_helper_wheel(tmp_path)
    env = authorized_env(tmp_path, tmp_path / "remotehome")
    env["FLEETMON_FAKE_REMOTE_HASH"] = digest

    result = run(
        HELPER,
        "--target",
        "disabledhost",
        "--wheel",
        str(wheel),
        "--i-authorize-target-disabledhost",
        env=env,
    )

    assert result.returncode == 3
    assert "refused: target_not_admitted" in result.stderr
    assert fake_log_lines(env) == ["show --json disabledhost"]


def test_helper_authorized_refuses_non_direct_protocol_before_any_transfer(
    tmp_path: Path,
) -> None:
    fake_executable(tmp_path / "fleetctl", AUTHORIZED_FLEETCTL_BODY)
    wheel, digest = build_helper_wheel(tmp_path)
    env = authorized_env(tmp_path, tmp_path / "remotehome")
    env["FLEETMON_FAKE_REMOTE_HASH"] = digest

    result = run(
        HELPER,
        "--target",
        "bridgehost",
        "--wheel",
        str(wheel),
        "--i-authorize-target-bridgehost",
        env=env,
    )

    assert result.returncode == 3
    assert "refused: target_not_admitted" in result.stderr
    assert "protocol is not direct" in result.stderr
    assert fake_log_lines(env) == [
        "show --json bridgehost",
        "protocol show --json route-proto",
    ]


def test_helper_refuses_when_remote_has_no_python_310(tmp_path: Path) -> None:
    fake_executable(tmp_path / "fleetctl", AUTHORIZED_FLEETCTL_BODY)
    wheel, digest = build_helper_wheel(tmp_path)
    env = authorized_env(tmp_path, tmp_path / "remotehome")
    env["FLEETMON_FAKE_REMOTE_HASH"] = digest
    env["FLEETMON_FAKE_NO_PYTHON"] = "1"

    result = run(
        HELPER,
        "--target",
        "ada1",
        "--wheel",
        str(wheel),
        "--i-authorize-target-ada1",
        env=env,
    )

    assert result.returncode == 3
    assert "refused: remote_python_unsupported" in result.stderr
    assert "never installs or replaces system python" in result.stderr
    log = fake_log_lines(env)
    assert log[-1].startswith("exec --target ada1 -- sh -c")
    assert not any("sync push" in line for line in log)


def test_helper_authorized_install_success_full_flow(tmp_path: Path) -> None:
    fake_executable(tmp_path / "fleetctl", AUTHORIZED_FLEETCTL_BODY)
    remote_home = tmp_path / "remotehome"
    wheel, digest = build_helper_wheel(tmp_path)
    env = authorized_env(tmp_path, remote_home)
    env["FLEETMON_FAKE_REMOTE_HASH"] = digest
    version = project_version()

    result = run(
        HELPER,
        "--target",
        "ada1",
        "--wheel",
        str(wheel),
        "--i-authorize-target-ada1",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    log = fake_log_lines(env)
    stage = f"{remote_home}/.cache/fleetmon/helper-stage-{version}"
    helpers = f"{remote_home}/.local/share/fleetmon/helpers"
    venv = f"{helpers}/{version}/venv"
    current = f"{helpers}/current"
    stage_wheel = f"{stage}/fleetmon-{version}-py3-none-any.whl"
    remote_python = f"{remote_home}/.local/bin/python3.12"
    expected = [
        "show --json ada1",
        "protocol show --json direct",
        "exec --target ada1 -- sh -c",  # interpreter discovery
        f"exec --target ada1 -- {remote_python} -c",  # probe
        f"exec --target ada1 -- {remote_python} -c",  # makedirs
        f"sync push --target ada1 {wheel} {stage_wheel}",
        f"sync push --target ada1 {HELPER_LOCK} {stage}/requirements-helper.lock",
        f"exec --target ada1 -- {remote_python} -c",  # staged wheel hash
        f"exec --target ada1 -- {remote_python} -c",  # clear previous version dir
        f"exec --target ada1 -- {remote_python} -m venv {venv}",
        f"exec --target ada1 -- {venv}/bin/python -m pip install --disable-pip-version-check -r {stage}/requirements-helper.lock {stage_wheel}",
        f"exec --target ada1 -- {venv}/bin/fleetmon-snapshot",
        f"exec --target ada1 -- {remote_python} -c",  # previous link
        f"exec --target ada1 -- {remote_python} -c",  # atomic switch
        f"exec --target ada1 -- {remote_python} -c",  # switched helper verification
        f"exec --target ada1 -- {remote_python} -c",  # staged artifacts cleanup
    ]
    assert len(log) == len(expected)
    markers = [
        "show --json",
        "protocol show",
        "fleetmon: find-python",
        "fleetmon: probe",
        "fleetmon: makedirs",
        "sync push --target",
        "sync push --target",
        "fleetmon: stage-hash",
        "fleetmon: clear-version-dir",
        f"python3.12 -m venv {venv}",
        "-m pip install",
        "bin/fleetmon-snapshot",
        "fleetmon: previous",
        "fleetmon: switch",
        "fleetmon: verify",
        "shutil.rmtree",
    ]
    for line, prefix in zip(log, expected, strict=True):
        assert line.startswith(prefix), (line, prefix)
    for marker, line in zip(markers, log, strict=True):
        assert marker in line, (marker, line)
    for line in log:
        assert "--all" not in line
        assert "--tag" not in line
        assert "--delete" not in line
    version_dir = f"{helpers}/{version}"
    assert os.readlink(current) == version_dir
    helper_path = f"{current}/venv/bin/fleetmon-snapshot"
    assert (
        f"helper path (record this verified absolute path in hub-local operational state): {helper_path}"
        in result.stdout
    )
    assert f"installed: target=ada1 helper_version={version}" in result.stdout


def test_helper_refuses_remote_stage_hash_mismatch_before_install(
    tmp_path: Path,
) -> None:
    fake_executable(tmp_path / "fleetctl", AUTHORIZED_FLEETCTL_BODY)
    wheel, _ = build_helper_wheel(tmp_path)
    env = authorized_env(tmp_path, tmp_path / "remotehome")
    env["FLEETMON_FAKE_REMOTE_HASH"] = "0" * 64

    result = run(
        HELPER,
        "--target",
        "ada1",
        "--wheel",
        str(wheel),
        "--i-authorize-target-ada1",
        env=env,
    )

    assert result.returncode == 4
    assert "refused: remote_stage_hash_mismatch" in result.stderr
    log = fake_log_lines(env)
    assert any("sync push" in line for line in log)
    assert not any("-m pip" in line for line in log)
    assert not any("fleetmon: switch" in line for line in log)


def test_helper_rolls_back_to_previous_on_verify_failure(tmp_path: Path) -> None:
    fake_executable(tmp_path / "fleetctl", AUTHORIZED_FLEETCTL_BODY)
    remote_home = tmp_path / "remotehome"
    wheel, digest = build_helper_wheel(tmp_path)
    env = authorized_env(tmp_path, remote_home)
    env["FLEETMON_FAKE_REMOTE_HASH"] = digest
    env["FLEETMON_FAKE_VERIFY"] = "bad"

    result = run(
        HELPER,
        "--target",
        "ada1",
        "--wheel",
        str(wheel),
        "--i-authorize-target-ada1",
        env=env,
    )

    assert result.returncode == 3
    assert "refused: switched_helper_failed" in result.stderr
    assert "rolled back to the previous activation" in result.stderr
    log = fake_log_lines(env)
    verify = [line for line in log if "fleetmon: verify" in line]
    rollback = [line for line in log if "fleetmon: rollback" in line]
    assert len(verify) == 1
    assert len(rollback) == 1
    assert log.index(verify[0]) < log.index(rollback[0])
    current = remote_home / ".local" / "share" / "fleetmon" / "helpers" / "current"
    assert not current.exists()


def test_hub_dry_run_has_no_filesystem_side_effects(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}

    result = run(HUB, "--dry-run", env=env)

    assert result.returncode == 0, result.stderr
    assert "requirements-hub.lock" in result.stdout
    assert "--no-deps" in result.stdout
    assert "lingering" in result.stdout
    assert not any(tmp_path.iterdir())


def test_hub_requires_systemd_before_creating_install_paths(tmp_path: Path) -> None:
    fake_systemctl = fake_executable(tmp_path / "systemctl", "exit 1\n")
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    result = run(HUB, env=env)

    assert result.returncode == 3
    assert "systemd user manager" in result.stderr
    assert fake_systemctl.exists()
    assert not (tmp_path / ".local" / "share" / "fleetmon").exists()


def test_hub_dry_run_rejects_symlinked_destination_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".config").symlink_to(outside, target_is_directory=True)
    env = {**os.environ, "HOME": str(tmp_path)}

    result = run(HUB, "--dry-run", env=env)

    assert result.returncode == 2
    assert "contains a symlink" in result.stderr
    assert not (outside / "systemd").exists()


def test_hub_dry_run_rejects_symlinked_state_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "state").mkdir()
    (tmp_path / ".local" / "state" / "fleetmon").symlink_to(
        outside, target_is_directory=True
    )
    env = {**os.environ, "HOME": str(tmp_path)}

    result = run(HUB, "--dry-run", env=env)

    assert result.returncode == 2
    assert "hub state directory" in result.stderr
    assert not any(outside.iterdir())


def test_hub_dry_run_rejects_current_target_outside_releases(tmp_path: Path) -> None:
    install_root = tmp_path / ".local" / "share" / "fleetmon"
    (install_root / "releases").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (install_root / "current").symlink_to(outside)
    env = {**os.environ, "HOME": str(tmp_path)}

    result = run(HUB, "--dry-run", env=env)

    assert result.returncode == 2
    assert "does not point into releases directory" in result.stderr


def test_hub_dry_run_rejects_group_writable_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o770)
    env = {**os.environ, "HOME": str(home)}

    result = run(HUB, "--dry-run", env=env)

    assert result.returncode == 2
    assert "writable by another user" in result.stderr


def test_hub_dry_run_rejects_world_writable_install_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_root = home / ".local" / "share" / "fleetmon"
    install_root.mkdir(parents=True)
    install_root.chmod(0o777)
    env = {**os.environ, "HOME": str(home)}

    result = run(HUB, "--dry-run", env=env)

    assert result.returncode == 2
    assert "writable by another user" in result.stderr


def test_hub_install_success_creates_final_path_venv_and_atomic_current(
    tmp_path: Path,
) -> None:
    home, env = make_home(tmp_path)
    fake_loginctl(tmp_path / "loginctl", "yes")

    result = run(HUB, env=env)

    assert result.returncode == 0, result.stderr
    install_root = home / ".local" / "share" / "fleetmon"
    releases = install_root / "releases"
    current = install_root / "current"
    assert current.is_symlink()
    release_names = sorted(path.name for path in releases.iterdir())
    assert len(release_names) == 1
    release = releases / release_names[0]
    assert Path(os.readlink(current)) == release
    assert (current / "venv" / "bin" / "python").exists()
    assert (current / "venv" / "bin" / "fleetmon").exists()
    log = Path(env["FLEETMON_FAKE_LOG"]).read_text().splitlines()
    assert log == [f"venv {release / 'venv'}"]
    unit_dir = home / ".config" / "systemd" / "user"
    assert sorted(path.name for path in unit_dir.iterdir()) == ["fleetmon-hub.service"]
    unit_path = unit_dir / "fleetmon-hub.service"
    assert unit_path.read_text() == UNIT_SOURCE.read_text()
    assert (unit_path.stat().st_mode & 0o777) == 0o644
    for path in (
        install_root,
        releases,
        home / ".local" / "state" / "fleetmon",
        home / ".local" / "state" / "fleet",
        home / ".cache" / "fleet",
    ):
        assert (path.stat().st_mode & 0o777) == 0o700
    assert "daemon-reload" in result.stdout
    assert "enable --now fleetmon-hub.service" in result.stdout
    assert "User lingering is enabled" in result.stdout


def test_hub_preserves_pre_existing_unit_without_replace(tmp_path: Path) -> None:
    home, env = make_home(tmp_path)
    fake_loginctl(tmp_path / "loginctl", "yes")
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit_path = unit_dir / "fleetmon-hub.service"
    unit_path.write_text("old unit\n")

    result = run(HUB, env=env)

    assert result.returncode == 3
    assert "unit exists" in result.stderr
    assert "--replace" in result.stderr
    assert unit_path.read_text() == "old unit\n"
    assert not (home / ".local" / "share" / "fleetmon").exists()


def test_hub_fails_closed_without_lingering_and_prints_admin_step(
    tmp_path: Path,
) -> None:
    home, env = make_home(tmp_path)
    fake_loginctl(tmp_path / "loginctl", "no")

    result = run(HUB, env=env)

    assert result.returncode == 3
    assert "systemd user lingering is not enabled" in result.stderr
    assert "sudo loginctl enable-linger" in result.stderr
    assert "NOT survive logout" in result.stderr
    assert "--yes-i-know-lingering-is-off" in result.stderr
    assert not (home / ".local").exists()
    assert not (home / ".config").exists()
    assert not (home / ".cache").exists()


def test_hub_lingering_opt_out_flag_installs_with_explicit_warning(
    tmp_path: Path,
) -> None:
    home, env = make_home(tmp_path)
    fake_loginctl(tmp_path / "loginctl", "no")

    result = run(HUB, "--yes-i-know-lingering-is-off", env=env)

    assert result.returncode == 0, result.stderr
    assert "will NOT survive logout" in result.stdout
    assert "sudo loginctl enable-linger" in result.stdout
    current = home / ".local" / "share" / "fleetmon" / "current"
    assert current.is_symlink()
    assert (current / "venv" / "bin" / "python").exists()
    log = Path(env["FLEETMON_FAKE_LOG"]).read_text().splitlines()
    assert all(line.startswith("venv ") for line in log)


def test_hub_missing_loginctl_requires_explicit_opt_out(tmp_path: Path) -> None:
    bin_dir = tmp_path / "coreutils"
    bin_dir.mkdir()
    for directory in ("/usr/bin", "/bin"):
        if not Path(directory).is_dir():
            continue
        for source in Path(directory).iterdir():
            if source.name == "loginctl":
                continue
            link = bin_dir / source.name
            if link.exists():
                continue
            with contextlib.suppress(FileExistsError):
                link.symlink_to(source)
    home, env = make_home(tmp_path)
    env["PATH"] = f"{tmp_path}:{bin_dir}"

    result = run(HUB, env=env)

    assert result.returncode == 3
    assert "loginctl was not found" in result.stderr
    assert "sudo loginctl enable-linger" in result.stderr
    assert not (home / ".local").exists()

    result = run(HUB, "--yes-i-know-lingering-is-off", env=env)

    assert result.returncode == 0, result.stderr
    assert "will NOT survive logout" in result.stdout
    current = home / ".local" / "share" / "fleetmon" / "current"
    assert current.is_symlink()


def _upgrade_setup(
    tmp_path: Path, backup_mode: str
) -> tuple[Path, dict[str, str], Path, Path, Path]:
    home, env = make_home(tmp_path)
    fake_loginctl(tmp_path / "loginctl", "yes")
    install_root = home / ".local" / "share" / "fleetmon"
    old_release = install_root / "releases" / "old"
    (old_release / "venv").mkdir(parents=True)
    current = install_root / "current"
    current.symlink_to(old_release)
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit_path = unit_dir / "fleetmon-hub.service"
    unit_path.write_text("old unit\n")
    state_dir = home / ".local" / "state" / "fleetmon"
    state_dir.mkdir(parents=True)
    (state_dir / "fleet.db").write_text("db\n")
    env["FLEETMON_FAKE_BACKUP"] = backup_mode
    return home, env, install_root, old_release, unit_path


def test_hub_upgrade_runs_schema_backup_before_switching_current(
    tmp_path: Path,
) -> None:
    home, env, install_root, old_release, unit_path = _upgrade_setup(tmp_path, "ok")

    result = run(HUB, "--replace", env=env)

    assert result.returncode == 0, result.stderr
    current = install_root / "current"
    new_release = Path(os.readlink(current))
    assert new_release != old_release
    assert (current / "venv" / "bin" / "fleetmon").exists()
    state_dir = home / ".local" / "state" / "fleetmon"
    log = Path(env["FLEETMON_FAKE_LOG"]).read_text().splitlines()
    assert log == [
        f"venv {new_release / 'venv'}",
        f"maintain backup --state-dir {state_dir}",
    ]
    release_names = sorted(path.name for path in (install_root / "releases").iterdir())
    assert release_names == sorted(["old", new_release.name])
    assert "schema backup created before upgrade: /backup/fleet.db.bak" in result.stdout
    assert unit_path.read_text() == UNIT_SOURCE.read_text()
    assert "User lingering is enabled" in result.stdout


def test_hub_upgrade_backup_command_failure_aborts_and_rolls_back(
    tmp_path: Path,
) -> None:
    home, env, install_root, old_release, unit_path = _upgrade_setup(tmp_path, "fail")

    result = run(HUB, "--replace", env=env)

    assert result.returncode != 0
    assert "maintain backup failed" in result.stderr
    current = install_root / "current"
    assert Path(os.readlink(current)) == old_release
    assert sorted(path.name for path in (install_root / "releases").iterdir()) == [
        "old"
    ]
    assert unit_path.read_text() == "old unit\n"


def test_hub_upgrade_backup_not_ok_json_aborts(tmp_path: Path) -> None:
    home, env, install_root, old_release, unit_path = _upgrade_setup(tmp_path, "notok")

    result = run(HUB, "--replace", env=env)

    assert result.returncode != 0
    assert "did not report ok" in result.stderr
    current = install_root / "current"
    assert Path(os.readlink(current)) == old_release
    assert sorted(path.name for path in (install_root / "releases").iterdir()) == [
        "old"
    ]
    assert unit_path.read_text() == "old unit\n"


def test_hub_rolls_back_current_when_unit_install_fails(tmp_path: Path) -> None:
    fake_python = fake_executable(
        tmp_path / "python",
        r"""
if [ "$1" = "-c" ]; then
  printf '%s\n' '3 11'
elif [ "$1" = "--version" ]; then
  printf '%s\n' 'Python 3.11.0'
elif [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
  cat >"$3/bin/fleetmon" <<'EOF'
#!/bin/sh
exit 0
EOF
  chmod 700 "$3/bin/fleetmon"
else
  exit 0
fi
""",
    )
    fake_systemctl = fake_executable(tmp_path / "systemctl", "exit 0\n")
    fake_loginctl(tmp_path / "loginctl", "yes")
    fake_executable(tmp_path / "install", "exit 1\n")
    install_root = tmp_path / ".local" / "share" / "fleetmon"
    releases = install_root / "releases"
    old_release = releases / "old"
    old_release.mkdir(parents=True)
    (old_release / "venv").mkdir()
    current = install_root / "current"
    current.symlink_to(old_release)
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit_path = unit_dir / "fleetmon-hub.service"
    unit_path.write_text("old unit\n")
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "FLEETMON_PYTHON": str(fake_python),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    result = run(HUB, "--replace", env=env)

    assert result.returncode != 0
    assert current.is_symlink()
    assert current.resolve() == old_release.resolve()
    assert unit_path.read_text() == "old unit\n"
    assert sorted(path.name for path in releases.iterdir()) == ["old"]
    assert fake_systemctl.exists()


def test_hub_uses_final_path_current_release_contract() -> None:
    installer = HUB.read_text()
    unit = UNIT_SOURCE.read_text()
    lock = (ROOT / "requirements-hub.lock").read_text()

    assert '"$release_dir/venv"' in installer
    assert 'mv -Tf -- "$current_tmp" "$current_path"' in installer
    assert '--no-deps "$source_dir"' in installer
    assert 'loginctl show-user "$UID" --property=Linger' in installer
    assert "--yes-i-know-lingering-is-off" in installer
    assert 'maintain backup --state-dir "$state_dir"' in installer
    assert "nvidia-ml-py" not in lock
    assert "current/venv/bin/fleetmon hub" in unit
    assert "current/venv/bin" in unit
    assert "Type=simple" in unit
    assert "/readyz" in unit
