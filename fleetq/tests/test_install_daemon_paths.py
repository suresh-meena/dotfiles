from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_install_ignores_preexisting_unit_tmp_symlink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "unrelated"
    outside.write_text("keep me\n")
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "fleetq.service.tmp").symlink_to(outside)
    current_tmp = home / ".local/share/fleetq/.current.new"
    current_tmp.parent.mkdir(parents=True)
    current_tmp.write_text("keep me too\n")

    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        "if [[ $1 == -c ]]; then echo '3 11'; exit 0; fi\n"
        "if [[ $2 == build.py ]]; then exit 0; fi\n"
        "if [[ $1 == -m && $2 == venv ]]; then\n"
        "  mkdir -p \"$3/bin\"\n"
        "  printf '#!/bin/sh\\nexit 0\\n' > \"$3/bin/python\"\n"
        "  printf '#!/bin/sh\\nexit 0\\n' > \"$3/bin/fleetqd\"\n"
        "  chmod +x \"$3/bin/python\" \"$3/bin/fleetqd\"\n"
        "fi\n"
    )
    fake_python.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    loginctl = fake_bin / "loginctl"
    loginctl.write_text("#!/bin/sh\necho Linger=no\n")
    loginctl.chmod(0o755)

    env = os.environ | {
        "HOME": str(home),
        "USER": "test-user",
        "FLEETQ_PYTHON": str(fake_python),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
    }
    result = subprocess.run(
        [str(ROOT / "scripts/install-daemon")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert outside.read_text() == "keep me\n"
    assert (unit_dir / "fleetq.service.tmp").is_symlink()
    assert current_tmp.read_text() == "keep me too\n"
    assert (unit_dir / "fleetq.service").read_text() == (ROOT / "packaging/fleetq.service").read_text()
