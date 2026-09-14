"""kiac-slurm: KIAC-aware Slurm generator, validator, and diagnostics CLI.

Subcommands: new, check, submit, doctor, resources, account, explain,
interactive, inspect. `check` never submits; `submit` always runs the same
checker first and needs explicit intent (--yes).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from . import __version__, rules_generic, rules_kiac
from .config import ConfigError, default_config_path, load_site_config
from .diagnostics import (
    CONF_DOCUMENTED,
    CONF_INFERRED,
    CONF_VERIFIED_LIVE,
    Report,
)
from . import learn
from .generator import TEMPLATES, GeneratorError, generate
from .live import (
    Runner,
    accounts as live_accounts,
    accounts_qos,
    discover,
    slurm_available,
    test_only,
)
from .parser import fmt_time, parse as parse_script
from .report import render_json, render_text
from .rules_generic import iter_gpu_requests


# ---------------------------------------------------------------------------
# check pipeline (also used by submit)
# ---------------------------------------------------------------------------

def run_check(
    path: str,
    *,
    live: bool = False,
    strict: bool = False,
    runner: Optional[Runner] = None,
    no_cache: bool = False,
    shellcheck: str = "auto",
    site=None,
    cache_dir=None,
) -> Report:
    runner = runner or Runner()
    if site is None:
        site = load_site_config()
    rep = Report()
    try:
        script = parse_script(path)
    except OSError as exc:
        rep.error("IO001", f"cannot read {path}: {exc}")
        return rep

    rules_generic.check_structure(script, rep)                    # 1
    rules_generic.check_shell(path, rep, runner=runner, shellcheck=shellcheck)  # 2
    rules_generic.check_generic(script, rep)                      # 3-4
    rules_generic.check_filesystem(script, rep)                   # 6
    rules_generic.check_modules(script, rep, runner=runner)       # 6b

    state = None
    account_list = None
    if live:
        if not slurm_available(runner):
            rep.warn("LIVE000", "live checks skipped: no Slurm commands on this host",
                     confidence=CONF_INFERRED)
        else:
            state = discover(runner=runner, cache_dir=cache_dir, force=no_cache)  # 7
            if state is None:
                rep.warn("LIVE000", "live checks skipped: scheduler discovery failed",
                         confidence=CONF_INFERRED)
            else:
                account_list = live_accounts(runner)               # 8
                learn.record_discovery(site, state)                # self-learning
    rules_kiac.check_kiac(script, rep, site, state=state, accounts=account_list)  # 5+7+8

    if state is not None:                                          # 9
        ok, out = test_only(runner, path)
        if ok:
            detail = f": {_first_line(out)}" if out else ""
            rep.pass_(
                "LIVE001",
                f"sbatch --test-only accepted the request{detail}",
                confidence=CONF_VERIFIED_LIVE,
            )
            if script.has("--account") or script.has("--qos"):
                rep.info(
                    "LIVE002",
                    "sbatch --test-only does NOT enforce account-partition or QOS policy "
                    "(verified 2026-09-14: a chiru/a100 request passed the dry run and then "
                    "stayed pending); validate new account/partition combos with a "
                    "5-minute smoke job",
                    confidence=CONF_VERIFIED_LIVE,
                )
        else:
            rep.error(
                "LIVE001",
                "sbatch --test-only rejected the request",
                excerpt=_tail(out, 6) or "no output",
                confidence=CONF_VERIFIED_LIVE,
            )
    if strict:
        rep.escalate_warnings()
    return rep


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_check(args, runner: Runner) -> int:
    site = load_site_config(args.config)
    rep = run_check(
        args.file,
        live=args.live,
        strict=args.strict,
        runner=runner,
        no_cache=args.no_cache,
        shellcheck=args.shellcheck,
        site=site,
    )
    if args.json:
        print(render_json(rep, args.file, live=args.live, strict=args.strict))
    else:
        print(render_text(rep, args.file, live=args.live, strict=args.strict))
    return rep.exit_code()


def cmd_submit(args, runner: Runner) -> int:
    site = load_site_config(args.config)
    live = not args.offline and slurm_available(runner)
    rep = run_check(args.file, live=live, runner=runner, no_cache=args.no_cache, site=site)
    print(render_text(rep, args.file, live=live))
    if rep.errors:
        print("refusing to submit: preflight reported errors", file=sys.stderr)
        return 1
    if not args.yes:
        print("preflight passed; re-run with --yes to submit for real", file=sys.stderr)
        return 0
    rc, out = runner.run(["sbatch", args.file])
    if rc != 0:
        learn.record({
            "kind": "submit-error",
            "subject": args.file,
            "observed": _first_line(out) or f"exit {rc}",
            "detail": _tail(out, 6),
            "source": "sbatch",
        })
    print(out.strip() or f"sbatch exited {rc}")
    return 0 if rc == 0 else 1


def cmd_new(args, runner: Runner) -> int:
    site = load_site_config(args.config)
    fields = (
        "job_name", "partition", "time", "ntasks", "cpus_per_task", "mem", "gres",
        "nodes", "ntasks_per_node", "account", "qos", "array", "workdir", "module",
    )
    values = {k: ("" if getattr(args, k, None) is None else str(getattr(args, k))) for k in fields}
    values["command"] = "" if args.run_command is None else str(args.run_command)
    if args.template == "h200" and not values.get("qos"):
        vp = site.verified_partition("h200") or {}
        required = vp.get("required_qos") or site.partitions.get("h200").batch_qos
        if required and required != "unknown":
            values["qos"] = str(required)
    text = generate(args.template, values, strict_bash=not args.no_strict_bash)

    state = discover(runner=runner) if slurm_available(runner) else None
    for confidence, note in _generator_notes(args.template, values, site, state):
        print(f"note [{confidence}] {note}", file=sys.stderr)

    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}", file=sys.stderr)
        target = args.output
    else:
        sys.stdout.write(text)
        target = None

    if not args.no_verify:
        if target is None:
            handle = tempfile.NamedTemporaryFile("w", suffix=".sbatch", delete=False)
            handle.write(text)
            handle.close()
            check_path = handle.name
        else:
            check_path = target
        try:
            rep = run_check(check_path, site=site, runner=runner)
        finally:
            if target is None:
                os.unlink(check_path)
        print(render_text(rep, args.output or "<generated>"), file=sys.stderr)
    return 0


def _generator_notes(template, values, site, state) -> List[tuple]:
    notes = []
    part = values.get("partition") or ("h200" if template == "h200" else "")
    if part:
        if state is not None and part in state.partitions:
            notes.append((CONF_VERIFIED_LIVE, f"partition '{part}' present on the live cluster"))
        elif part in site.partitions:
            doc = site.partitions[part]
            extra = " (documented limits are disputed)" if doc.status == "disputed" else ""
            notes.append((CONF_DOCUMENTED, f"partition '{part}' from the manual's table; "
                                           f"limits unverified live{extra}"))
        else:
            notes.append((CONF_INFERRED,
                          f"partition '{part}' is neither documented nor live-verified — "
                          "check before submitting"))
    gres = values.get("gres") or ""
    parts = gres.split(":")
    if len(parts) >= 2 and parts[0] == "gpu" and not parts[1].isdigit():
        gtype = parts[1]
        verified = {g.casefold() for g in site.verified_gres_types(part)}
        if gtype.casefold() in verified:
            as_of = f" as of {site.verified_as_of}" if site.verified_as_of else ""
            notes.append((CONF_VERIFIED_LIVE,
                          f"GPU type '{gtype}' is verified on '{part}'{as_of}"))
        elif gtype.casefold() in {g.casefold() for g in site.gpu_catalog}:
            notes.append((CONF_DOCUMENTED,
                          f"GPU type '{gtype}' is in the documented catalog; the live GRES "
                          "string may differ"))
        else:
            notes.append((CONF_INFERRED,
                          f"GPU type '{gtype}' is not in the documented catalog — verify live"))
    if not values.get("time"):
        notes.append((CONF_INFERRED, "time defaulted to 1:00:00 (conservative)"))
    if part:
        vp = site.verified_partition(part)
        account = values.get("account") or ""
        if vp:
            allowed = [str(a) for a in vp.get("allowed_accounts") or []]
            if allowed and account and account not in allowed:
                notes.append((CONF_VERIFIED_LIVE,
                              f"account '{account}' is NOT permitted on '{part}' "
                              f"(allowed: {', '.join(allowed)}); the job would pass "
                              "--test-only and then stay pending"))
            elif account and allowed:
                notes.append((CONF_VERIFIED_LIVE,
                              f"account '{account}' is permitted on '{part}'"))
            required_qos = vp.get("required_qos")
            if required_qos:
                notes.append((CONF_VERIFIED_LIVE,
                              f"partition '{part}' requires --qos={required_qos}; without it "
                              "the job stays pending with a QOS permission error"))
        elif template == "h200" and values.get("account"):
            notes.append((CONF_DOCUMENTED,
                          "H200 account provided; verify against your associations with "
                          "`kiac-slurm account`"))
    return notes


def cmd_doctor(args, runner: Runner) -> int:
    site = load_site_config(args.config)
    lines = []
    core_ok = True
    for tool in ("sbatch", "sinfo", "scontrol", "squeue", "sacct", "sacctmgr", "scancel"):
        rc, out = runner.run([tool, "--version"])
        if rc == 0:
            lines.append(("PASS", f"{tool}: {_first_line(out)}"))
        else:
            lines.append(("WARN", f"{tool}: not available"))
            if tool in ("sbatch", "sinfo"):
                core_ok = False
    rc, _ = runner.run(["shellcheck", "--version"])
    lines.append(("INFO", f"shellcheck: {'available (optional)' if rc == 0 else 'not installed (optional)'}"))
    rc, _ = runner.run(["bash", "-lc", "type module"])
    lines.append((
        "PASS" if rc == 0 else "WARN",
        "module system: " + ("visible in login shells" if rc == 0
                             else "not visible from here; verify on the login node"),
    ))

    state = None
    if slurm_available(runner):
        state = discover(runner=runner, force=args.no_cache)
    if state is not None:
        lines.append(("PASS", f"cluster state via {state.source}: "
                              f"{len(state.partitions)} partitions, {len(state.nodes)} nodes"))
        gres = state.gpu_state_summary()
        lines.append(("INFO", f"GPU GRES types: {', '.join(gres) if gres else 'none reported'}"))
        acc_qos = accounts_qos(runner)
        if acc_qos:
            summary = ", ".join(
                f"{acc}({'/'.join(sorted(qos)) or 'no qos'})" for acc, qos in sorted(acc_qos.items())
            )
            lines.append(("INFO", f"accounts/QOS: {summary}"))
        else:
            lines.append(("INFO", "accounts/QOS: not discoverable"))
    else:
        lines.append(("WARN", f"no live cluster state; documented table has "
                              f"{len(site.partitions)} partitions (values may be disputed)"))

    preferred = site.preferred_storage
    if os.path.isdir(preferred):
        writable = os.access(preferred, os.W_OK)
        lines.append(("PASS", f"storage: {preferred} present" + (", writable" if writable else "")))
    else:
        lines.append(("WARN", f"storage: {preferred} not mounted here"))
    lines.append(("INFO", "home quota: unknown until queried (manual gives conflicting figures)"))

    entries = learn.read_observations()
    if entries:
        latest = max(entries, key=lambda e: str(e.get("ts")))
        lines.append((
            "INFO",
            f"learn: {len(entries)} observation(s), latest {latest.get('ts')} "
            "(review with `kiac-slurm learn log`)",
        ))
    else:
        lines.append(("INFO", "learn: no observations recorded yet"))

    for level, message in lines:
        print(f"{level:<5} {message}")
    return 0 if core_ok else 1


def cmd_resources(args, runner: Runner) -> int:
    site = load_site_config(args.config)
    state = None
    if not args.documented and slurm_available(runner):
        state = discover(runner=runner, force=args.no_cache)
        if state is not None:
            learn.record_discovery(site, state)

    if state is not None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.fetched_at))
        print(f"# live cluster state via {state.source}, fetched {stamp} [verified-live]")
        rows = []
        for name in sorted(state.partitions):
            part = state.partitions[name]
            nodes = part.nodes or []
            states = sorted({state.nodes[n].state.split("+")[0] for n in nodes if n in state.nodes})
            agg = {}
            for node_name in nodes:
                node = state.nodes.get(node_name)
                if node is None:
                    continue
                for gname, gtype, count in node.gres:
                    if gname != "gpu":
                        continue
                    key = gtype or "gpu"
                    agg[key] = agg.get(key, 0) + count
            rows.append((
                name,
                fmt_time(part.max_time),
                ",".join(nodes) or "-",
                ",".join(states) or "?",
                ",".join(f"{k}:{v}" for k, v in sorted(agg.items())) or "-",
            ))
        _print_table(["PARTITION", "MAXTIME", "NODES", "STATES", "GPU GRES"], rows)
    else:
        as_of = site.verified_as_of
        header = "# documented KIAC partition table [documented; manual conflicts marked disputed]"
        if site.verified.get("partitions"):
            header += f"; GPU types/account policy verified live {as_of}"
        print(header)
        rows = []
        for name in site.partition_names:
            doc = site.partitions[name]
            times = " vs ".join(doc.max_times_raw)
            if doc.status == "disputed":
                times += " [disputed]"
            vp = site.verified_partition(name) or {}
            gres = ",".join(str(g) for g in vp.get("gres_types") or []) or "run --live"
            rows.append((name, times, ",".join(doc.nodes_raw), doc.status, gres))
        _print_table(["PARTITION", "MAXTIME", "NODES", "STATUS", "GPU GRES"], rows)
        for name in site.partition_names:
            vp = site.verified_partition(name) or {}
            bits = []
            if vp.get("allowed_accounts"):
                bits.append(f"accounts: {','.join(str(a) for a in vp['allowed_accounts'])}")
            if vp.get("denied_accounts"):
                bits.append(f"denied: {','.join(str(a) for a in vp['denied_accounts'])}")
            if vp.get("required_qos"):
                bits.append(f"requires --qos={vp['required_qos']}")
            if bits:
                print(f"  {name}: {'; '.join(bits)}")
        if not args.documented and not slurm_available(runner):
            print("\nno live Slurm on this host; run on the KIAC login node for verified state")
    return 0


def cmd_account(args, runner: Runner) -> int:
    site = load_site_config(args.config)
    acc_qos = accounts_qos(runner)
    if acc_qos:
        print("your Slurm account associations [verified-live]:")
        for account, qos in sorted(acc_qos.items()):
            suffix = f"  (QOS: {', '.join(sorted(qos))})" if qos else ""
            print(f"  - {account}{suffix}")
    else:
        print("could not read your Slurm associations (sacctmgr/sshare unavailable or empty).")
        print("check on the login node:  sacctmgr -nP show assoc user=$USER format=Account,QOS")
    requiring = [p for p, d in site.partitions.items() if d.account_required]
    if requiring:
        print(f"\npartitions requiring --account: {', '.join(requiring)}")
    if site.verified.get("partitions"):
        as_of = site.verified_as_of
        print(f"\nverified account/partition policy (as of {as_of}):")
        for name in site.partition_names:
            vp = site.verified_partition(name) or {}
            bits = []
            if vp.get("allowed_accounts"):
                bits.append("accounts " + ",".join(str(a) for a in vp["allowed_accounts"]))
            if vp.get("denied_accounts"):
                bits.append("DENIES " + ",".join(str(a) for a in vp["denied_accounts"]))
            if vp.get("required_qos"):
                bits.append(f"needs --qos={vp['required_qos']}")
            if bits:
                print(f"  {name}: {'; '.join(bits)}")
        print("  note: sbatch --test-only does NOT enforce account/QOS policy — validate "
              "new combos with a 5-minute smoke job")
    else:
        print("the manual does not name account strings; use a verified association above, "
              "never a guess")
    return 0


def cmd_explain(args, runner: Runner) -> int:
    site = load_site_config(args.config)
    script = parse_script(args.file)
    print(f"{args.file}: {len(script.directives)} directive(s)")
    for directive in script.directives:
        value = f"={directive.value}" if directive.value is not None else ""
        desc = EXPLANATIONS.get(directive.option, "see man sbatch")
        print(f"  line {directive.line_no:<4} {(directive.option + value):<34} {desc}")
    for line in _resource_totals(script):
        print(f"  {line}")
    rep = run_check(args.file, site=site, runner=runner, shellcheck="off")
    counts = rep.counts()
    print(f"  problems: {counts['error']} ERROR, {counts['warn']} WARN "
          f"(details: kiac-slurm check {args.file})")
    return 0


def cmd_interactive(args, runner: Runner) -> int:
    site = load_site_config(args.config)
    doc = site.partitions.get(args.partition)
    if doc is not None and doc.account_required and not args.account:
        print(f"error: partition '{args.partition}' requires --account (KIAC020); "
              f"see `kiac-slurm account`", file=sys.stderr)
        return 2
    base = [
        args.salloc and "salloc" or "srun",
        f"--partition={args.partition}",
        f"--time={args.time}",
        f"--cpus-per-task={args.cpus}",
        f"--mem={args.mem}",
    ]
    if args.gres:
        base.append(f"--gres={args.gres}")
    if args.account:
        base.append(f"--account={args.account}")
    if args.qos:
        base.append(f"--qos={args.qos}")
    if args.salloc:
        command = base
        print("# inside the allocation, run your build/debug commands directly")
    else:
        command = base + ["--pty", "bash", "-i"]
    if doc is not None and doc.account_required and not args.qos:
        vp = site.verified_partition(args.partition) or {}
        required = vp.get("required_qos") or doc.interactive_qos
        if required:
            command.append(f"--qos={required}")
            tag = f"verified live {site.verified_as_of}" if site.verified_as_of else "documented"
            print(f"note [{tag}]: added --qos={required} (partition '{args.partition}' rejects "
                  "jobs without it)")
    elif doc is not None and doc.interactive_qos and not args.qos:
        print(f"note [documented]: the manual uses --qos={doc.interactive_qos} for interactive "
              f"{args.partition} work; verify it applies to you")
    print(" ".join(command))
    if args.execute:
        os.execvp(command[0], command)
    return 0


def cmd_inspect(args, runner: Runner) -> int:
    jobid = args.jobid
    rc, out = runner.run(["squeue", "-h", "-j", jobid, "-o", "%T|%R|%P|%a|%L|%M|%N"])
    rows = [line for line in out.strip().splitlines() if line.strip()] if rc == 0 else []
    if rows:
        print(f"job {jobid} is known to the scheduler:")
        for row in rows:
            fields = (row.split("|") + [""] * 7)[:7]
            state, reason, part, account, tlimit, elapsed, nodes = fields
            print(f"  state={state}  partition={part}  account={account}  "
                  f"timelimit={tlimit}  elapsed={elapsed}  nodes={nodes}")
            if state == "PENDING" and reason:
                print(f"  reason={reason}")
                match = re.match(r"[A-Za-z]+", reason)
                explanation = REASON_EXPLANATIONS.get(match.group(0) if match else "")
                if explanation:
                    print(f"    -> {explanation}")
                learn.record_job_reason(reason, account or None)
        _print_cancel_hint(jobid)
        return 0

    rc, out = runner.run(["scontrol", "show", "job", jobid])
    if rc == 0 and "JobId" in out:
        print(f"job {jobid} (no longer queued):")
        fields = dict(re.findall(r"(\w+)=(\S+)", out))
        for key in ("JobState", "Reason", "RunTime", "TimeLimit", "Partition", "NodeList", "ExitCode"):
            if fields.get(key):
                print(f"  {key}={fields[key]}")
        if fields.get("Reason"):
            learn.record_job_reason(fields["Reason"], fields.get("Account") or None)
        _print_cancel_hint(jobid)
        return 0

    rc, out = runner.run(
        ["sacct", "-j", jobid, "-X", "-n", "-o", "JobID,State,ExitCode,Elapsed,MaxRSS,NodeList"]
    )
    if rc == 0 and out.strip():
        print(f"job {jobid} history (sacct):")
        for line in out.strip().splitlines():
            print(f"  {line}")
            for marker, hint in (
                ("OUT_OF_MEMORY", "ran out of RAM: raise --mem or check for leaks"),
                ("TIMEOUT", "hit the walltime: raise --time within the partition MaxTime"),
                ("FAILED", "nonzero exit: inspect the job's output/error logs"),
                ("CANCELLED", "cancelled by user or staff"),
            ):
                if marker in line:
                    print(f"    -> {hint}")
        _print_cancel_hint(jobid)
        return 0

    print(f"no information found for job {jobid} (squeue, scontrol, sacct all empty)")
    return 1


def _print_cancel_hint(jobid: str) -> None:
    print(f"\ncancel with: scancel {jobid}   (never `kill {jobid}`; that signals a Unix "
          f"PID, not a Slurm job)")


def cmd_learn(args, runner: Runner) -> int:
    site = load_site_config(args.config)
    if args.learn_cmd == "note":
        text = " ".join(args.text).strip()
        if not text:
            print("error: provide the note text", file=sys.stderr)
            return 2
        saved = learn.record({
            "kind": "manual-note",
            "subject": "note",
            "observed": text[:120],
            "detail": text,
            "source": "user",
        })
        print("recorded" if saved else "not recorded (KIAC_SLURM_LEARN=off or duplicate)")
        return 0

    if args.learn_cmd == "apply":
        entries = learn.read_observations()
        state = None
        if slurm_available(runner):
            state = discover(runner=runner, force=args.no_cache)
        new_verified = learn.build_verified_update(site, state, entries, runner=runner)
        config_path = Path(args.config) if args.config else default_config_path()
        if config_path is None:
            print("error: no site config found", file=sys.stderr)
            return 2
        config_path = Path(config_path)
        if not config_path.exists():
            print(f"error: config not found: {config_path}", file=sys.stderr)
            return 2
        changed, _new_text, diff = learn.plan_apply(config_path, new_verified, site.verified)
        if not changed:
            if state is None:
                print("nothing to change: no live scheduler reachable from here and the log "
                      "holds no policy evidence — run this on the login node to fold in a "
                      "live sweep")
            else:
                print("nothing to learn: verified_live already matches live state and the log")
            return 0
        if not args.yes:
            print(diff)
            print("\nreview the diff above, then re-run with --yes to write it "
                  f"(backup: {config_path}.bak; commit the result so other machines "
                  "inherit the learning)", file=sys.stderr)
            return 0
        diff = learn.apply_verified(config_path, new_verified, site.verified)
        print(f"updated {config_path} (backup at {config_path}.bak)")
        print(diff)
        return 0

    # default: log
    entries = learn.summarize(learn.read_observations())
    if not entries:
        print("no observations recorded yet; they accumulate automatically on every "
              "--live run and from `inspect` on pending jobs")
        return 0
    shown = entries[: args.limit]
    print(f"{len(entries)} unique observation(s) (showing {len(shown)}); "
          "apply with `kiac-slurm learn apply`\n")
    for entry in shown:
        head = f"{entry.get('ts', '?')}  {entry.get('kind', '?'):>16}  {entry.get('subject', '?')}"
        if entry.get("count", 1) > 1:
            head += f"  (x{entry['count']})"
        print(head)
        print(f"    observed: {entry.get('observed', '?')}")
        if entry.get("detail"):
            print(f"    {entry['detail']}")
        if entry.get("source"):
            print(f"    source: {entry['source']}")
    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _print_table(headers, rows) -> None:
    rows = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def _resource_totals(script) -> List[str]:
    lines = []
    ntasks = _to_int(script.get("--ntasks"))
    ntpn = _to_int(script.get("--ntasks-per-node"))
    nodes = script.get("--nodes") or "1"
    match = re.match(r"^(\d+)(?:-(\d+))?$", nodes)
    node_hi = int(match.group(2) or match.group(1)) if match else 1
    tasks = ntasks if ntasks else ((ntpn or 1) * node_hi if ntpn else 1)
    cpus = _to_int(script.get("--cpus-per-task")) or 1
    lines.append(f"totals: ~{tasks} task(s) x {cpus} CPU(s) = {tasks * cpus} CPU(s) "
                 f"across <= {node_hi} node(s)")
    mem = script.get("--mem")
    mem_cpu = script.get("--mem-per-cpu")
    if mem:
        lines.append(f"memory: {mem} per node (~{mem} x {node_hi} node(s))")
    elif mem_cpu:
        lines.append(f"memory: {mem_cpu} per CPU (~{tasks * cpus} CPUs total)")
    gpu_total = 0
    types = set()
    for directive in script.get_all("--gres"):
        parts = (directive.value or "").split(":")
        if parts and parts[0] == "gpu":
            count = int(parts[-1]) if parts[-1].isdigit() else 1
            if len(parts) == 3:
                types.add(parts[1])
            gpu_total += count * node_hi
    for option, multiplier in (("--gpus-per-node", node_hi), ("--gpus-per-task", tasks)):
        for directive in script.get_all(option):
            tail = (directive.value or "").split(":")[-1]
            if tail.isdigit():
                gpu_total += int(tail) * multiplier
    total_gpus = _to_int(script.get("--gpus"))
    if total_gpus:
        gpu_total += total_gpus
    if gpu_total or types:
        suffix = f" ({', '.join(sorted(types))})" if types else ""
        lines.append(f"GPUs: ~{gpu_total} total{suffix} (approximate; --gres counts per node)")
    return lines


def _to_int(value) -> Optional[int]:
    if value is None or not str(value).isdigit():
        return None
    return int(value)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _tail(text: str, n: int) -> Optional[str]:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:]) if lines else None


EXPLANATIONS = {
    "--account": "Slurm accounting account; gates partitions like h200",
    "--array": "job array spec; %A master id, %a task index in filenames",
    "--begin": "earliest submission time",
    "--chdir": "working directory when the job starts",
    "--constraint": "node feature constraint",
    "--cpus-per-task": "CPU cores per task; tasks x this = total cores",
    "--cpus-per-gpu": "CPU cores per allocated GPU",
    "--dependency": "wait on other jobs (afterok:ID, afterany:ID, ...)",
    "--distribution": "task layout across nodes/sockets",
    "--error": "stderr file; %j job id, %x job name",
    "--exclusive": "do not share nodes with other jobs",
    "--export": "environment passed to the job (ALL|NONE|VARS)",
    "--gres": "generic resources per node, e.g. gpu:a100:1",
    "--gpus": "total GPUs for the whole job",
    "--gpus-per-node": "GPUs per node",
    "--gpus-per-task": "GPUs per task",
    "--job-name": "job name shown in squeue; usable as %x in filenames",
    "--licenses": "named licenses required",
    "--mail-type": "email events (BEGIN, END, FAIL, ALL)",
    "--mail-user": "address for job mail",
    "--mem": "real memory per node; suffixes K/M/G/T, no suffix = MB",
    "--mem-per-cpu": "memory per allocated CPU",
    "--mem-per-gpu": "memory per allocated GPU",
    "--nodes": "node count, or min-max",
    "--nodelist": "request specific nodes",
    "--ntasks": "number of tasks (processes) in the job",
    "--ntasks-per-node": "tasks placed on each node",
    "--open-mode": "append (append) or truncate (truncate) output files",
    "--output": "stdout file; %j job id, %x job name, %A/%a for arrays",
    "--partition": "queue to submit into",
    "--qos": "quality of service associated with the job",
    "--requeue": "requeue the job if it fails node-wise",
    "--reservation": "submit into a named reservation",
    "--signal": "signal before the time limit, e.g. B:USR1@120",
    "--time": "walltime limit; MM:SS, HH:MM:SS, or D-HH:MM:SS",
    "--time-min": "soft minimum time for backfill",
    "--tmp": "local scratch disk per node",
    "--wait": "block until the job finishes",
    "--wckey": "workload characterization key (accounting)",
    "--wrap": "run this command string instead of a script file",
}

REASON_EXPLANATIONS = {
    "Resources": "not enough free nodes right now; nothing is wrong with the request — "
                 "wait, or ask for fewer/smaller resources",
    "Priority": "other queued jobs outrank this one; wait",
    "Dependency": "waiting on the jobs listed in --dependency",
    "PartitionTimeLimit": "request exceeds the partition MaxTime; run "
                          "kiac-slurm check FILE --live",
    "QOSMaxWallTimePerJobLimit": "QOS walltime cap exceeded; lower --time or change QOS",
    "QOSMaxCpuPerJobLimit": "QOS CPU cap exceeded",
    "QOSMaxNodePerJobLimit": "QOS node cap exceeded",
    "QOSMaxMemoryPerJob": "QOS memory cap exceeded",
    "AssocGrpCpuLimit": "your association's running-CPU limit is reached",
    "AssocGrpMemLimit": "your association's running-memory limit is reached",
    "AssocGrpJobsLimit": "your association's running-job count limit is reached",
    "AssocMaxJobsLimit": "your association's job cap is reached",
    "ReqNodeNotAvail": "a requested node is down or absent; check sinfo -n <node>",
    "NodeCfgErr": "node configuration does not satisfy the request",
    "JobHeldAdmin": "held by staff; contact them",
    "JobHeldUser": "held by you: scontrol release <jobid> once fixed",
    "BeginTime": "the --begin time has not arrived yet",
    "Licenses": "waiting for licenses",
    "FrontEndDown": "frontend node down; staff issue",
    "Prolog": "node startup phase still running",
    "AccountingPolicy": "accounting/fair-share limits",
}


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiac-slurm",
        description="KIAC-aware Slurm batch script generator, validator, and diagnostics",
    )
    parser.add_argument("--version", action="version",
                        version=f"kiac-slurm {__version__} (slurm-helper)")
    sub = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None,
                        help="site config path (default: config/kiac.yaml in the skill tree)")

    p = sub.add_parser("new", parents=[common],
                       help="generate a batch script from parameters")
    p.add_argument("-t", "--template", choices=TEMPLATES, default="cpu")
    p.add_argument("--name", dest="job_name", default=None, help="job name (default 'job')")
    p.add_argument("--partition", default=None)
    p.add_argument("--time", default=None, help="walltime HH:MM:SS (default 1:00:00)")
    p.add_argument("--ntasks", type=int, default=None)
    p.add_argument("--cpus", dest="cpus_per_task", type=int, default=None)
    p.add_argument("--mem", default=None, help="memory, e.g. 32G (default 4G)")
    p.add_argument("--gres", default=None, help="e.g. gpu:1 or gpu:a100:1")
    p.add_argument("--nodes", type=int, default=None)
    p.add_argument("--ntasks-per-node", dest="ntasks_per_node", type=int, default=None)
    p.add_argument("--account", default=None)
    p.add_argument("--qos", default=None)
    p.add_argument("--array", default=None, help="array spec, e.g. 1-100:10%%8")
    p.add_argument("--workdir", default=None, help="working directory (default '.')")
    p.add_argument("--module", default=None, help="module to load, e.g. python/3.11")
    p.add_argument("--command", dest="run_command", default=None,
                   help="command to run (default 'python3 main.py')")
    p.add_argument("-o", "--output", default=None, help="write to this file instead of stdout")
    p.add_argument("--no-strict-bash", action="store_true",
                   help="emit 'set -e' instead of 'set -euo pipefail'")
    p.add_argument("--no-verify", action="store_true", help="skip the offline self-check")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("check", parents=[common],
                       help="validate a batch script (offline, or --live for full preflight)")
    p.add_argument("file")
    p.add_argument("--live", action="store_true",
                   help="query the scheduler and run sbatch --test-only")
    p.add_argument("--strict", action="store_true", help="treat warnings as errors")
    p.add_argument("--json", action="store_true", help="JSON diagnostics for editor/CI use")
    p.add_argument("--no-cache", action="store_true", help="refresh cached cluster state")
    p.add_argument("--shellcheck", choices=("auto", "on", "off"), default="auto")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("submit", parents=[common],
                       help="full preflight, then sbatch (requires --yes)")
    p.add_argument("file")
    p.add_argument("--yes", action="store_true",
                   help="actually submit after preflight passes")
    p.add_argument("--offline", action="store_true",
                   help="skip live preflight even if Slurm is available")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("doctor", parents=[common],
                       help="check Slurm commands, cluster state, accounts, modules, storage")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("resources", parents=[common],
                       help="summarize partitions, MaxTime, nodes, GRES (live or documented)")
    p.add_argument("--documented", action="store_true",
                   help="show the manual's table instead of live state")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(func=cmd_resources)

    p = sub.add_parser("account", parents=[common],
                       help="show usable Slurm accounts/QOS without guessing")
    p.set_defaults(func=cmd_account)

    p = sub.add_parser("explain", parents=[common],
                       help="explain each directive, resource totals, and problems")
    p.add_argument("file")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("interactive", parents=[common],
                       help="construct an srun/salloc debugging allocation (prints by default)")
    p.add_argument("--partition", required=True)
    p.add_argument("--time", default="1:00:00")
    p.add_argument("--cpus", type=int, default=1)
    p.add_argument("--mem", default="4G")
    p.add_argument("--gres", default=None)
    p.add_argument("--account", default=None)
    p.add_argument("--qos", default=None)
    p.add_argument("--salloc", action="store_true", help="emit salloc instead of srun")
    p.add_argument("--execute", action="store_true", help="run the constructed command")
    p.set_defaults(func=cmd_interactive)

    p = sub.add_parser("inspect", parents=[common],
                       help="explain a pending/running/finished job")
    p.add_argument("jobid")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("learn", parents=[common],
                       help="review recorded evidence and fold it into the site config")
    learn_sub = p.add_subparsers(dest="learn_cmd")
    lp = learn_sub.add_parser("log", help="show recorded observations (default)")
    lp.add_argument("--limit", type=int, default=25)
    lp = learn_sub.add_parser("apply", help="rewrite the verified_live config section "
                                            "(prints a diff; --yes to write)")
    lp.add_argument("--yes", action="store_true", help="write the config update")
    lp.add_argument("--no-cache", action="store_true")
    lp = learn_sub.add_parser("note", help="record a manual observation")
    lp.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_learn, learn_cmd="log", limit=25)

    return parser


def main(argv=None, runner: Optional[Runner] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    if runner is None:
        runner = Runner()
    try:
        return args.func(args, runner) or 0
    except (ConfigError, GeneratorError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
