"""Observation cadence (§3.5): one session per cluster per cycle, never a busy poller."""

from __future__ import annotations

from fleetq.engine.controller import ControllerConfig

from harness import Harness


def controller(tmp_path):
    h = Harness(tmp_path, config=ControllerConfig())
    return h, h.start()


def test_clusters_poll_fast_only_right_after_a_change(tmp_path):
    h, ctl = controller(tmp_path)
    cfg = ctl.config
    assert ctl._observe_interval("site", "slurm", None, now=1000.0) == cfg.slurm_observe_slow_s == 120.0
    ctl._last_transition["site"] = 1000.0
    assert ctl._observe_interval("site", "slurm", None, now=1010.0) == cfg.slurm_observe_fast_s == 30.0
    assert ctl._observe_interval("site", "slurm", None, now=1000.0 + cfg.slurm_fast_window_s + 1) == 120.0
    # Workstations keep their own, quicker default; an explicit per-target setting wins.
    assert ctl._observe_interval("ws", "bare", None, now=1000.0) == cfg.observe_interval_s
    assert ctl._observe_interval("site", "slurm", 600.0, now=1010.0) == 600.0
    h.close()


def test_an_unreachable_login_node_backs_off_to_the_cap(tmp_path):
    h, ctl = controller(tmp_path)
    ctl._observe_failures["site"] = 1
    assert ctl._observe_interval("site", "slurm", None, now=0.0) == 240.0
    ctl._observe_failures["site"] = 10
    assert ctl._observe_interval("site", "slurm", None, now=0.0) == ctl.config.slurm_unreachable_max_s == 1800.0
    ctl._observe_failures["ws"] = 10
    assert ctl._observe_interval("ws", "bare", None, now=0.0) == ctl.config.observe_interval_s
    h.close()
