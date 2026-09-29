import json
import shutil

import pytest

from fleetq.shim import fq_node


def enrolled_root(path):
    path.mkdir(mode=0o700)
    (path / "enrollment.json").write_text(json.dumps({"control_root": str(path)}))
    attempts = path / "attempts"
    attempts.mkdir(mode=0o700)
    path.chmod(0o700)
    attempts.chmod(0o700)
    return fq_node.ControlRoot(path)


def test_safe_rmtree_parent_swap_after_pin_cannot_delete_outside(tmp_path, monkeypatch):
    root_path = tmp_path / "control"
    root = enrolled_root(root_path)
    attempts = root_path / "attempts"
    victim = attempts / "victim"
    victim.mkdir(mode=0o700)
    (victim / "data").write_text("inside")

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    outside_victim = outside / "victim"
    outside_victim.mkdir(mode=0o700)
    marker = outside_victim / "keep"
    marker.write_text("safe")

    real_rmtree = shutil.rmtree
    swapped = False

    def swap_then_rmtree(path, *args, **kwargs):
        nonlocal swapped
        if path == "victim" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            attempts.rename(root_path / "attempts-pinned")
            attempts.symlink_to(outside, target_is_directory=True)
        return real_rmtree(path, *args, **kwargs)

    swap_then_rmtree.avoids_symlink_attacks = real_rmtree.avoids_symlink_attacks
    monkeypatch.setattr(fq_node.shutil, "rmtree", swap_then_rmtree)
    fq_node._safe_rmtree(root, victim)

    assert swapped
    assert marker.read_text() == "safe"
    assert not (root_path / "attempts-pinned" / "victim").exists()


def test_safe_rmtree_is_missing_leaf_noop_and_rejects_symlink(tmp_path):
    root_path = tmp_path / "control"
    root = enrolled_root(root_path)
    missing = root_path / "attempts" / "missing"
    fq_node._safe_rmtree(root, missing)

    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("safe")
    link = root_path / "attempts" / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(fq_node.ShimError) as exc:
        fq_node._safe_rmtree(root, link)
    assert exc.value.code == "unsafe_cleanup"
    assert marker.read_text() == "safe"
