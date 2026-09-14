"""Self-learning: observe live evidence, log it, and distill it into config.

The loop (always with a human in charge of the final step):

  1. Every live run diffs the scheduler against config/kiac.yaml and appends
     structured observations to var/observations.jsonl (deduplicated).
  2. `kiac-slurm inspect` turns real pending-job reasons — the strongest
     signal, e.g. "Job's account not permitted to use this partition (a100
     allows research,freerun_not_chiru)" — into account/QOS evidence.
  3. `kiac-slurm learn log` shows the evidence; `learn apply --yes` rewrites
     ONLY the marker-bounded verified_live section of kiac.yaml (printed
     diff + backup first). Learned facts then drive offline checks.

Recording never raises into the caller: a failing log write must not break
a preflight.
"""

from __future__ import annotations

import datetime
import difflib
import getpass
import json
import os
import re
import socket
from pathlib import Path
from typing import Dict, List, Optional

from .config import SiteConfig, repo_root
from .live import ClusterState, accounts_qos
from .parser import fmt_time, parse_time

BEGIN_MARKER = "# --- verified-live:begin (managed by `kiac-slurm learn apply`) ---"
END_MARKER = "# --- verified-live:end ---"

MAX_LOG_ENTRIES = 2000
DEDUP_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# Observation store
# ---------------------------------------------------------------------------

def observations_path() -> Path:
    env = os.environ.get("KIAC_SLURM_OBSERVATIONS")
    if env:
        return Path(env)
    return repo_root() / "var" / "observations.jsonl"


def learning_enabled() -> bool:
    return os.environ.get("KIAC_SLURM_LEARN", "1").strip().lower() not in ("off", "0", "no", "false")


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def record(entry: dict) -> bool:
    """Append one observation (deduped); returns False when skipped/failed."""
    try:
        if not learning_enabled():
            return False
        path = observations_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = dict(entry)
        entry.setdefault("ts", _now_iso())
        entry.setdefault("host", socket.gethostname())
        entry.setdefault("user", getpass.getuser())

        existing = read_observations(path)
        cutoff = datetime.datetime.now() - datetime.timedelta(days=DEDUP_WINDOW_DAYS)
        for old in existing:
            if (
                old.get("kind") == entry.get("kind")
                and old.get("subject") == entry.get("subject")
                and old.get("observed") == entry.get("observed")
                and _parse_ts(old.get("ts")) >= cutoff
            ):
                return False

        existing.append(entry)
        _write_observations(path, existing[-MAX_LOG_ENTRIES:])
        return True
    except Exception:
        return False


def _parse_ts(value) -> datetime.datetime:
    try:
        return datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.datetime.min


def read_observations(path=None) -> List[dict]:
    path = Path(path) if path else observations_path()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("kind"):
            out.append(entry)
    return out


def _write_observations(path: Path, entries: List[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in entries))
    tmp.replace(path)


def summarize(entries: List[dict]) -> List[dict]:
    """Latest entry per (kind, subject, observed), with an occurrence count."""
    groups: Dict[tuple, dict] = {}
    for entry in entries:
        key = (entry.get("kind"), entry.get("subject"), entry.get("observed"))
        if key not in groups:
            groups[key] = dict(entry, count=0)
        groups[key]["count"] += 1
        if _parse_ts(entry.get("ts")) >= _parse_ts(groups[key].get("ts")):
            groups[key].update({k: entry.get(k) for k in ("ts", "detail", "source")})
    return sorted(groups.values(), key=lambda e: str(e.get("ts")), reverse=True)


# ---------------------------------------------------------------------------
# Evidence extraction
# ---------------------------------------------------------------------------

def discovery_observations(site: SiteConfig, state: ClusterState) -> List[dict]:
    """Where the live cluster disagrees with (or resolves) the config."""
    out: List[dict] = []
    for name, live in sorted(state.partitions.items()):
        doc = site.partitions.get(name)
        if doc is None:
            out.append({
                "kind": "partition-new",
                "subject": name,
                "observed": f"nodes={','.join(live.nodes) or '?'}",
                "detail": "exists live but is absent from the documented table",
                "source": state.source,
            })
        if live.max_time is not None and live.max_time >= 0:
            learned = (site.verified.get("max_times") or {}).get(name)
            if fmt_time(live.max_time) != learned:
                resolution = ""
                if doc is not None:
                    matching = [
                        i + 1 for i, t in enumerate(doc.max_times_raw)
                        if parse_time(t) == live.max_time
                    ]
                    if doc.status == "disputed":
                        resolution = (
                            f"resolves documented dispute ({' vs '.join(doc.max_times_raw)})"
                            if matching else "contradicts every documented candidate"
                        )
                out.append({
                    "kind": "maxtime",
                    "subject": name,
                    "observed": fmt_time(live.max_time),
                    "detail": resolution or "differs from the recorded value",
                    "source": state.source,
                })
        types = sorted(state.partition_gres_types(name))
        known = {g.casefold() for g in site.verified_gres_types(name)}
        if types and known != {t.casefold() for t in types}:
            out.append({
                "kind": "gres-types",
                "subject": name,
                "observed": ",".join(types),
                "detail": "live GRES type set differs from the verified list",
                "source": state.source,
            })
    for name in site.partition_names:
        if name not in state.partitions:
            out.append({
                "kind": "partition-missing-live",
                "subject": name,
                "observed": "absent",
                "detail": "documented partition not returned by the live scheduler",
                "source": state.source,
            })
    return out


def record_discovery(site: SiteConfig, state: ClusterState) -> int:
    if not learning_enabled():
        return 0
    written = 0
    for entry in discovery_observations(site, state):
        if record(entry):
            written += 1
    return written


_REASON_ALLOWS_RE = re.compile(r"\((\S+)\s+allows\s+([^)]+)\)")
_REASON_INVALID_ACCOUNT_RE = re.compile(r"[Ii]nvalid account or account/partition combination")


def parse_reason_evidence(reason: str, job_account: Optional[str] = None) -> List[dict]:
    """Turn scheduler pending/rejection reasons into structured evidence."""
    out: List[dict] = []
    if not reason:
        return out
    for match in _REASON_ALLOWS_RE.finditer(reason):
        partition, raw = match.group(1), match.group(2)
        # drop the "not <value>" clause: "(h200 allows h200_qos not normal)"
        raw = re.sub(r"\bnot\s+\S+", "", raw, flags=re.IGNORECASE)
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if any("qos" in t.lower() for t in tokens):
            required = tokens[0]
            out.append({
                "kind": "qos-required",
                "subject": partition,
                "observed": required,
                "detail": f"scheduler reason: {match.group(0)!r}"
                          + (f" (job account: {job_account})" if job_account else ""),
                "source": "job reason",
            })
        else:
            detail = f"scheduler reason: {match.group(0)!r}"
            if job_account:
                detail += f" (job account '{job_account}' was rejected)"
            out.append({
                "kind": "account-policy",
                "subject": partition,
                "observed": ",".join(tokens),
                "detail": detail,
                "source": "job reason",
                "job_account": job_account,
            })
    if _REASON_INVALID_ACCOUNT_RE.search(reason):
        out.append({
            "kind": "account-invalid",
            "subject": job_account or "unknown",
            "observed": "not associated with this user",
            "detail": f"scheduler reason: {reason.strip()[:200]!r}",
            "source": "job reason",
        })
    return out


def record_job_reason(reason: str, job_account: Optional[str] = None) -> int:
    written = 0
    for entry in parse_reason_evidence(reason, job_account):
        if record(entry):
            written += 1
    return written


# ---------------------------------------------------------------------------
# Distilling evidence into the config's verified_live section
# ---------------------------------------------------------------------------

def build_verified_update(
    site: SiteConfig,
    state: Optional[ClusterState],
    entries: List[dict],
    runner=None,
    today: Optional[str] = None,
) -> Dict:
    """Merge live state + logged evidence into a new verified_live mapping."""
    merged = json.loads(json.dumps(site.verified or {}))  # deep copy of plain data
    if state is not None:
        for name, live in state.partitions.items():
            part = merged.setdefault("partitions", {}).setdefault(name, {})
            types = sorted(state.partition_gres_types(name))
            if types:
                part["gres_types"] = types
            if live.max_time is not None:
                merged.setdefault("max_times", {})[name] = fmt_time(live.max_time)
    if runner is not None:
        acc_qos = accounts_qos(runner)
        if acc_qos:
            merged["accounts"] = sorted(acc_qos)
    for entry in entries:  # chronological, so newer evidence wins
        kind = entry.get("kind")
        subject = entry.get("subject")
        # only structured policy evidence touches the config; manual notes,
        # maxtime/gres observations (already handled via `state`), submit
        # errors, etc. are for `learn log` review, not for partition entries
        if kind not in ("account-policy", "qos-required") or not subject:
            continue
        part = merged.setdefault("partitions", {}).setdefault(subject, {})
        if kind == "account-policy" and entry.get("observed"):
            allowed = [a for a in str(entry["observed"]).split(",") if a]
            part["allowed_accounts"] = allowed
            job_account = entry.get("job_account")
            denied = [a for a in part.get("denied_accounts") or []]
            if job_account and job_account not in allowed and job_account not in denied:
                denied.append(job_account)
                part["denied_accounts"] = denied
        elif kind == "qos-required" and entry.get("observed"):
            part["required_qos"] = str(entry["observed"])
    if merged:
        merged["as_of"] = today or datetime.date.today().isoformat()
    return merged


def _yaml_scalar(value) -> str:
    text = str(value)
    if re.search(r"[:#\[\]{},]|^\s|\s$|^$", text) or not re.match(r"^[A-Za-z0-9_.\-/]+$", text or " "):
        return "'" + text.replace("'", "''") + "'"
    return text


def _dump_mapping(data: dict, indent: int = 0) -> List[str]:
    lines = []
    pad = " " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.extend(_dump_mapping(value, indent + 2))
        elif isinstance(value, (list, tuple)) and value:
            items = ", ".join(_yaml_scalar(v) for v in value)
            lines.append(f"{pad}{key}: [{items}]")
        elif isinstance(value, (list, tuple)):
            lines.append(f"{pad}{key}: []")
        elif isinstance(value, bool):
            lines.append(f"{pad}{key}: {'true' if value else 'false'}")
        elif value is None:
            lines.append(f"{pad}{key}: null")
        else:
            lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
    return lines


def dump_verified_section(data: Dict) -> str:
    lines = [BEGIN_MARKER, "verified_live:"]
    order = ["as_of", "user", "accounts", "test_only_enforces_account_policy"]
    ordered = {k: data[k] for k in order if k in data}
    for key in sorted(data):
        if key not in ordered and key not in ("partitions", "max_times"):
            ordered[key] = data[key]
    lines.extend(_dump_mapping(ordered, 2))
    if "partitions" in data and data["partitions"]:
        lines.append("  partitions:")
        lines.extend(_dump_mapping(data["partitions"], 4))
    if "max_times" in data and data["max_times"]:
        lines.append("  max_times:")
        lines.extend(_dump_mapping(data["max_times"], 4))
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def _strip_as_of(data: Dict) -> Dict:
    out = json.loads(json.dumps(data or {}))
    out.pop("as_of", None)
    return out


def plan_apply(config_path: Path, new_verified: Dict, current_verified: Dict):
    """Returns (changed, new_text, diff_text). Does not write."""
    old_text = config_path.read_text()
    section = dump_verified_section(new_verified) if new_verified else ""
    if BEGIN_MARKER in old_text and END_MARKER in old_text:
        before, rest = old_text.split(BEGIN_MARKER, 1)
        _, after = rest.split(END_MARKER, 1)
        new_text = before + section.rstrip("\n") + after if section else before.rstrip("\n") + "\n" + after.lstrip("\n")
    elif section:
        new_text = old_text.rstrip("\n") + "\n\n" + section
    else:
        return False, old_text, ""
    if _strip_as_of(new_verified) == _strip_as_of(current_verified):
        return False, old_text, ""
    diff = "\n".join(difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile=str(config_path), tofile=str(config_path), lineterm="",
    ))
    return True, new_text, diff


def apply_verified(config_path: Path, new_verified: Dict, current_verified: Dict) -> Optional[str]:
    """Write the updated config (with .bak backup); returns the diff, or None
    when there was nothing to change."""
    changed, new_text, diff = plan_apply(config_path, new_verified, current_verified)
    if not changed:
        return None
    backup = config_path.with_suffix(config_path.suffix + ".bak")
    backup.write_text(config_path.read_text())
    config_path.write_text(new_text)
    return diff
