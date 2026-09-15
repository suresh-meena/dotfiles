"""Site policy rules (stage 5) resolved against live scheduler state (7-8).

Multi-site: every rule ID is prefixed from the site config (`KIAC`* on KIAC,
`AMD`* on the AMD MI210 cluster); rule numbers are shared. Source-of-truth
model per site:

  * Slurm syntax comes from SchedMD semantics (rules_generic).
  * Current partitions/nodes/GRES/times/accounts come from the live controller.
  * Site-only operational rules come from the site manual (config/<site>.yaml).
  * A dated `verified_live` section records facts confirmed on the real
    cluster; it outranks the manual but is re-verified after cluster changes
    and can be superseded by a fresh live query in the same run.

Conflicts are surfaced, never silently reconciled: disputed documented values
stay labeled document-conflict until a live query resolves them.

Account/QOS policy is enforced from the verified matrix because
`sbatch --test-only` does NOT enforce it (verified 2026-09-14 on KIAC:
chiru/a100 passed --test-only but the real job stayed pending with "Job's
account not permitted to use this partition").
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

from .config import SiteConfig
from .diagnostics import (
    CONF_CONFLICT,
    CONF_DOCUMENTED,
    CONF_VERIFIED_LIVE,
    Report,
)
from .live import ClusterState
from .parser import ParsedScript, expand_hostlist, fmt_time, parse_memory, parse_time
from .rules_generic import iter_gpu_requests


def _rid(site: SiteConfig, code: str) -> str:
    return f"{site.rule_prefix}{code}"


def check_kiac(
    script: ParsedScript,
    rep: Report,
    site: SiteConfig,
    state: Optional[ClusterState] = None,
    accounts: Optional[List[str]] = None,
) -> None:
    part = script.get("--partition")
    part_d = script.directive_for("--partition")
    doc = site.partitions.get(part) if part else None
    live_p = state.partitions.get(part) if (state is not None and part) else None
    live_names = sorted(state.partitions) if state is not None else None

    _check_partition(script, rep, site, part, part_d, doc, live_p, live_names)
    _check_account(script, rep, site, part, part_d, doc, accounts)
    _check_time(script, rep, site, part, doc, live_p)
    _check_storage(script, rep, site)
    _check_gpu(script, rep, site, part, state, live_p)
    _check_gpu_jobs_only(script, rep, site, part)
    _check_gpu_vendor_tools(script, rep, site)
    _check_nodelist(script, rep, site, part)
    _check_node_states(script, rep, site, part, state, live_p)
    _check_memory_vs_nodes(script, rep, site, part, state, live_p)


def _verified_tag(site: SiteConfig) -> str:
    as_of = site.verified_as_of
    return f"verified live {as_of}" if as_of else "verified live"


def _check_partition(script, rep, site, part, part_d, doc, live_p, live_names) -> None:
    if part is None:
        rep.error(
            _rid(site, "001"),
            f"no --partition directive; {site.cluster_name} usage requires an explicit partition",
            suggestion="pick one from `kiac-slurm resources`; do not assume a cluster default",
        )
        return
    if doc is None:
        if live_names is not None:
            if live_p is None:
                rep.error(
                    _rid(site, "011"),
                    f'"--partition={part}" is absent from the documented {site.cluster_name} '
                    "partition table.\nLive lookup: partition not found",
                    line=part_d.line_no,
                    excerpt=part_d.raw,
                    suggestion="use one of: " + ", ".join(live_names),
                    confidence=CONF_VERIFIED_LIVE,
                )
            else:
                rep.pass_(
                    "LIVE010",
                    f"partition '{part}' exists live (nodes: {', '.join(live_p.nodes) or '?'}) "
                    "but is not in the manual's table",
                    confidence=CONF_VERIFIED_LIVE,
                )
        else:
            # spec: unknown partitions are rejected offline unless explicitly
            # configured or found live (the KIAC manual's `general` must not pass)
            rep.error(
                _rid(site, "011"),
                f'"--partition={part}" is not in the documented {site.cluster_name} partition '
                f"table ({', '.join(site.partition_names)}) and cannot be verified offline",
                line=part_d.line_no,
                excerpt=part_d.raw,
                suggestion="use a documented partition, or verify with "
                           "`kiac-slurm check FILE --live`",
                confidence=CONF_DOCUMENTED,
            )
    else:
        rep.pass_(
            _rid(site, "010"),
            f"partition '{part}' is documented (nodes: {', '.join(doc.nodes_raw) or '?'})",
            line=part_d.line_no,
            confidence=CONF_DOCUMENTED,
        )
        if doc.status == "example-only":
            rep.info(
                _rid(site, "010"),
                f"partition '{part}' is named only in the manual's example scripts — "
                "confirm it (and its limits) live before relying on it",
                line=part_d.line_no,
                confidence=CONF_DOCUMENTED,
            )
        if live_names is not None and live_p is None:
            rep.error(
                _rid(site, "011"),
                f"partition '{part}' is documented but absent from the live cluster",
                line=part_d.line_no,
                excerpt=part_d.raw,
                suggestion="live partitions: " + ", ".join(live_names),
                confidence=CONF_VERIFIED_LIVE,
            )


def _check_account(script, rep, site, part, part_d, doc, accounts) -> None:
    if doc is None or not doc.account_required:
        _check_account_policy(script, rep, site, part, accounts)
        _check_qos_policy(script, rep, site, part)
        return
    acc = script.directive_for("--account")
    if acc is None or not acc.value:
        rep.error(
            _rid(site, "020"),
            f"{part.upper()} jobs require an account.",
            line=part_d.line_no,
            excerpt=part_d.raw,
            suggestion="Add: #SBATCH --account=<verified_account>   (see `kiac-slurm account`)",
        )
        _check_qos_policy(script, rep, site, part)
        return
    if accounts is not None:
        if acc.value not in accounts:
            rep.error(
                "LIVE030",
                f"account '{acc.value}' is not among your associations: {', '.join(accounts)}",
                line=acc.line_no,
                excerpt=acc.raw,
                confidence=CONF_VERIFIED_LIVE,
            )
        else:
            rep.pass_(
                "LIVE030",
                f"account '{acc.value}' is one of your live associations",
                line=acc.line_no,
                confidence=CONF_VERIFIED_LIVE,
            )
    _check_account_policy(script, rep, site, part, accounts)
    _check_qos_policy(script, rep, site, part)


def _check_account_policy(script, rep, site, part, accounts) -> None:
    """Account-to-partition permissions from the dated verified matrix.

    Enforced even offline because --test-only does not catch these: a
    chiru/a100 request passes the dry run and then pends forever.
    """
    vp = site.verified_partition(part)
    if vp is None:
        return
    allowed = [str(a) for a in vp.get("allowed_accounts") or []]
    denied = [str(a) for a in vp.get("denied_accounts") or []]
    acc = script.directive_for("--account")
    if acc is None or not acc.value:
        return
    tag = _verified_tag(site)
    note = vp.get("verified_note")
    if acc.value in denied or (allowed and acc.value not in allowed):
        detail = f" ({note})" if note else ""
        live_hint = ""
        if accounts is not None:
            live_hint = (
                f"; your live associations: {', '.join(accounts)}"
                if accounts
                else "; no live associations found"
            )
        rep.error(
            _rid(site, "023"),
            f"account '{acc.value}' is not permitted on partition '{part}' {tag}{detail}. "
            "The job would pass sbatch --test-only and then stay pending with "
            "'Job's account not permitted to use this partition'"
            + live_hint,
            line=acc.line_no,
            excerpt=acc.raw,
            suggestion=(
                f"use one of: {', '.join(allowed)}" if allowed
                else f"do not use '{acc.value}' on '{part}'"
            ),
            confidence=CONF_VERIFIED_LIVE,
        )
    elif allowed and acc.value in allowed:
        rep.pass_(
            _rid(site, "023"),
            f"account '{acc.value}' is permitted on '{part}' ({tag})",
            line=acc.line_no,
            confidence=CONF_VERIFIED_LIVE,
        )


def _check_qos_policy(script, rep, site, part) -> None:
    vp = site.verified_partition(part)
    if vp is None:
        return
    required = vp.get("required_qos")
    if not required:
        return
    qos_d = script.directive_for("--qos")
    tag = _verified_tag(site)
    if qos_d is None or not qos_d.value:
        rep.error(
            _rid(site, "024"),
            f"partition '{part}' requires --qos={required} ({tag}); without it the job "
            f"stays pending with \"Job's QOS not permitted to use this partition\"",
            suggestion=f"Add: #SBATCH --qos={required}",
            confidence=CONF_VERIFIED_LIVE,
        )
    elif qos_d.value != required:
        rep.error(
            _rid(site, "024"),
            f"--qos={qos_d.value} is not permitted on '{part}'; the verified value is "
            f"'{required}' ({tag})",
            line=qos_d.line_no,
            excerpt=qos_d.raw,
            suggestion=f"#SBATCH --qos={required}",
            confidence=CONF_VERIFIED_LIVE,
        )
    else:
        rep.pass_(
            _rid(site, "024"),
            f"--qos={required} matches the verified requirement for '{part}' ({tag})",
            line=qos_d.line_no,
            confidence=CONF_VERIFIED_LIVE,
        )


def _check_time(script, rep, site, part, doc, live_p) -> None:
    time_d = script.directive_for("--time")
    want = parse_time(time_d.value) if time_d else None
    if want is None or want <= 0:
        return
    live_max = live_p.max_time if live_p is not None else None
    learned_max = site.verified_max_time(part)
    limits = [x for x in doc.max_times if x is not None] if doc else []

    if live_max is not None and live_max >= 0:
        if want > live_max:
            rep.error(
                "LIVE011",
                f"requested --time={time_d.value} exceeds live MaxTime "
                f"{fmt_time(live_max)} on '{part}'",
                line=time_d.line_no,
                excerpt=time_d.raw,
                suggestion=f"use --time<={fmt_time(live_max)}",
                confidence=CONF_VERIFIED_LIVE,
            )
        else:
            rep.pass_(
                "LIVE011",
                f"--time={time_d.value} within live MaxTime {fmt_time(live_max)}",
                line=time_d.line_no,
                confidence=CONF_VERIFIED_LIVE,
            )
            if doc is not None and doc.status == "disputed":
                rep.info(
                    "LIVE011",
                    f"documented MaxTime conflict for '{part}' resolved live: {fmt_time(live_max)}",
                    confidence=CONF_VERIFIED_LIVE,
                )
        return

    if learned_max is not None and learned_max >= 0:
        # previously-learned live MaxTime: authoritative even offline
        if want > learned_max:
            rep.error(
                _rid(site, "033"),
                f"--time={time_d.value} exceeds the learned MaxTime {fmt_time(learned_max)} "
                f"for '{part}' ({_verified_tag(site)})",
                line=time_d.line_no,
                excerpt=time_d.raw,
                suggestion=f"stay <= {fmt_time(learned_max)}",
                confidence=CONF_VERIFIED_LIVE,
            )
        else:
            rep.pass_(
                _rid(site, "033"),
                f"--time={time_d.value} within learned MaxTime {fmt_time(learned_max)} "
                f"({_verified_tag(site)})",
                line=time_d.line_no,
                confidence=CONF_VERIFIED_LIVE,
            )
            if doc is not None and doc.status == "disputed":
                rep.info(
                    _rid(site, "033"),
                    f"documented MaxTime dispute for '{part}' resolved by learned live "
                    f"data: {fmt_time(learned_max)}",
                    line=time_d.line_no,
                    confidence=CONF_VERIFIED_LIVE,
                )
        return

    if not limits:
        return
    if want > max(limits):
        rep.error(
            _rid(site, "030"),
            f"--time={time_d.value} exceeds every documented MaxTime for '{part}' "
            f"({' vs '.join(doc.max_times_raw)})",
            line=time_d.line_no,
            excerpt=time_d.raw,
            suggestion=f"stay <= {fmt_time(max(limits))} until verified live",
            confidence=CONF_DOCUMENTED,
        )
    elif len(set(limits)) > 1:
        if want > min(limits):
            rep.warn(
                _rid(site, "031"),
                f"documented MaxTime for '{part}' is disputed ({' vs '.join(doc.max_times_raw)}); "
                f"--time={time_d.value} may exceed the real limit",
                line=time_d.line_no,
                excerpt=time_d.raw,
                suggestion="run with --live to resolve",
                confidence=CONF_CONFLICT,
            )
        else:
            rep.info(
                _rid(site, "032"),
                f"documented MaxTime dispute for '{part}' ({' vs '.join(doc.max_times_raw)}) — "
                "this request fits both candidates",
                line=time_d.line_no,
                confidence=CONF_CONFLICT,
            )
    else:
        rep.pass_(
            _rid(site, "030"),
            f"--time={time_d.value} within documented MaxTime {fmt_time(limits[0])}",
            line=time_d.line_no,
            confidence=CONF_DOCUMENTED,
        )


def _check_storage(script, rep, site) -> None:
    preferred = site.preferred_storage
    if site.home_path_prefix:
        home = site.home_path_prefix.rstrip("/")
    else:
        home = os.path.expanduser("~")
    writable_note = (
        "" if site.assume_storage_writable else " (writability not assumed — check on the cluster)"
    )
    caveat = f" — {site.preferred_storage_caveat}" if site.preferred_storage_caveat else ""
    quota = f" ({site.home_quota_note})" if site.home_quota_note else ""
    candidates = []
    chdir = script.get("--chdir")
    if chdir:
        candidates.append((None, chdir))
    for line_no, text in script.body_lines:
        match = re.match(r"^cd\s+([^\s;&|]+)", text)
        if match:
            candidates.append((line_no, match.group(1)))
    praised = False
    for line_no, target in candidates:
        expanded = os.path.expanduser(target)
        if expanded.startswith("$"):
            continue
        if expanded == preferred or expanded.startswith(preferred + "/"):
            if not praised:
                rep.pass_(
                    _rid(site, "040"),
                    f"working directory '{expanded}' is on the recommended storage "
                    f"({preferred}){writable_note}{caveat}",
                    confidence=CONF_DOCUMENTED,
                )
                praised = True
        elif expanded == home or expanded.startswith(home + "/"):
            rep.warn(
                _rid(site, "040"),
                f"working directory '{expanded}' is under the home directory{quota}; the "
                f"manual recommends {preferred} for workload data",
                line=line_no,
                confidence=CONF_DOCUMENTED,
                suggestion=f"move data to {preferred}/<project> after confirming it suits "
                "your retention needs",
            )


def _check_gpu(script, rep, site, part, state, live_p) -> None:
    catalog = site.gpu_catalog
    verified_types = {g.casefold() for g in site.verified_gres_types(part)}
    requests = list(iter_gpu_requests(script))
    for line_no, raw, gtype, count in requests:
        if gtype is not None and state is not None and live_p is not None:
            types = state.partition_gres_types(part)
            if types:
                if gtype in types:
                    rep.pass_(
                        "LIVE020",
                        f"GPU type '{gtype}' is configured on partition '{part}'",
                        line=line_no,
                        confidence=CONF_VERIFIED_LIVE,
                    )
                else:
                    rep.error(
                        "LIVE020",
                        f"GPU type '{gtype}' is not configured on partition '{part}' "
                        f"(live GRES types: {', '.join(sorted(types))})",
                        line=line_no,
                        excerpt=raw,
                        suggestion="request one of the live types above",
                        confidence=CONF_VERIFIED_LIVE,
                    )
            elif state.partition_has_gpu(part):
                rep.info(
                    "LIVE020",
                    f"nodes of '{part}' report untyped gpu GRES; type '{gtype}' cannot be "
                    "verified live",
                    line=line_no,
                    confidence=CONF_VERIFIED_LIVE,
                )
        elif gtype is not None and verified_types:
            # dated verified matrix: per-partition GRES strings confirmed on
            # the real cluster; stronger than the manual's catalog
            if gtype.casefold() in verified_types:
                rep.pass_(
                    _rid(site, "051"),
                    f"GPU type '{gtype}' is verified on partition '{part}' "
                    f"({_verified_tag(site)})",
                    line=line_no,
                    confidence=CONF_VERIFIED_LIVE,
                )
            else:
                rep.error(
                    _rid(site, "051"),
                    f"GPU type '{gtype}' is not among the types verified on '{part}' "
                    f"({', '.join(sorted(verified_types))}) ({_verified_tag(site)})",
                    line=line_no,
                    excerpt=raw,
                    suggestion="request one of the verified types above, or re-run "
                    "with --live after a cluster change",
                    confidence=CONF_VERIFIED_LIVE,
                )
        elif gtype is not None:
            if gtype.casefold() in {g.casefold() for g in catalog}:
                rep.pass_(
                    _rid(site, "050"),
                    f"GPU type '{gtype}' is in the documented catalog (the live GRES string "
                    "may differ)",
                    line=line_no,
                    confidence=CONF_DOCUMENTED,
                )
            else:
                rep.warn(
                    _rid(site, "050"),
                    f"GPU type '{gtype}' is not in the documented catalog ({', '.join(catalog)})",
                    line=line_no,
                    excerpt=raw,
                    suggestion="run with --live to discover configured GRES types",
                    confidence=CONF_DOCUMENTED,
                )
        if count and state is not None and live_p is not None:
            max_count = state.partition_gpu_max(part, gtype)
            if max_count is not None and count > max_count:
                rep.error(
                    "LIVE020",
                    f"requests {count} x gpu:{gtype or 'gpu'} but nodes of '{part}' provide "
                    f"at most {max_count}",
                    line=line_no,
                    excerpt=raw,
                    suggestion=f"reduce to <= {max_count}",
                    confidence=CONF_VERIFIED_LIVE,
                )
    if requests and state is not None and live_p is not None:
        if not state.partition_has_gpu(part):
            rep.error(
                "LIVE021",
                f"partition '{part}' has no GPUs on any live node",
                confidence=CONF_VERIFIED_LIVE,
            )


def _check_gpu_jobs_only(script, rep, site, part) -> None:
    """AMD Policy B: GPU queues are for GPU jobs; usage is monitored and
    CPU-only jobs in them are a blockable violation."""
    if not site.gpu_jobs_only_partitions or part not in site.gpu_jobs_only_partitions:
        return
    gpu_requested = any(
        count is None or count >= 1
        for _line_no, _raw, _gtype, count in iter_gpu_requests(script)
    )
    if gpu_requested:
        return
    rep.error(
        _rid(site, "070"),
        f"partition '{part}' is a GPU-only queue (site policy; CPU-only jobs there are "
        "monitored and can get the account blocked), but this script requests no GPU",
        suggestion="add --gres=gpu:N, or move the CPU workload to a CPU partition",
        confidence=CONF_DOCUMENTED,
    )


def _check_gpu_vendor_tools(script, rep, site) -> None:
    """The AMD manual's own example runs nvidia-smi on MI210 hardware."""
    if site.gpu_vendor != "amd":
        return
    for line_no, text in script.body_lines:
        if "nvidia-smi" in text:
            rep.error(
                _rid(site, "071"),
                "'nvidia-smi' cannot work here: this site has AMD MI210 GPUs "
                "(the manual's own example gets this wrong)",
                line=line_no,
                excerpt=text,
                suggestion="use rocm-smi (or rocminfo) to inspect AMD GPUs",
                confidence=CONF_DOCUMENTED,
            )


def _check_nodelist(script, rep, site, part) -> None:
    nodelist = script.get("--nodelist")
    if not nodelist:
        return
    nodelist_d = script.directive_for("--nodelist")
    for node in expand_hostlist(nodelist):
        mapped = site.node_partition_map.get(node)
        if mapped and part and mapped != part:
            rep.warn(
                _rid(site, "060"),
                f"node '{node}' is documented under partition '{mapped}', not '{part}'",
                line=nodelist_d.line_no if nodelist_d else None,
                confidence=CONF_DOCUMENTED,
                suggestion="drop --nodelist or match it to the partition",
            )


def _check_node_states(script, rep, site, part, state, live_p) -> None:
    if state is None or live_p is None or not live_p.nodes:
        return
    bad = [n for n in live_p.nodes if n in state.nodes and state.nodes[n].is_down()]
    if bad and len(bad) == len(live_p.nodes):
        rep.warn(
            "LIVE040",
            f"every node of partition '{part}' is down/drained: {', '.join(live_p.nodes)}",
            confidence=CONF_VERIFIED_LIVE,
        )


def _check_memory_vs_nodes(script, rep, site, part, state, live_p) -> None:
    if state is None or live_p is None:
        return
    mem_d = script.directive_for("--mem")
    if mem_d is None:
        return
    mb, problem = parse_memory(mem_d.value)
    if problem or mb is None:
        return  # invalid units are SLURM021/022's job
    caps = [
        state.nodes[n].real_memory
        for n in live_p.nodes
        if n in state.nodes and state.nodes[n].real_memory
    ]
    if not caps:
        return
    cap = min(caps)
    if mb > cap:
        rep.error(
            "LIVE012",
            f"--mem={mem_d.value} (~{int(mb)}MB) exceeds the smallest node memory on "
            f"'{part}' (~{cap}MB); the job can never start",
            line=mem_d.line_no,
            excerpt=mem_d.raw,
            suggestion=f"use --mem<={cap // 1024}G",
            confidence=CONF_VERIFIED_LIVE,
        )
