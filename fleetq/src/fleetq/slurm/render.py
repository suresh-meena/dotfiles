"""Scripts that run on a cluster login node or inside a batch allocation (§3.2–3.6).

Everything here is POSIX ``sh``/``bash`` with standard tools, because login
nodes may run an old Python or none. No resident process is ever left behind:
each script is a bounded one-shot command.

Cluster control root (shared, compute-visible, durable)::

    <root>/fence                       "<fleet_id> <epoch>"
    <root>/cache/<sha>.tar.gz          verified snapshot bundles (pinned while referenced)
    <root>/attempts/<attempt>/
        manifest.json  batch.sh        pushed before any submission claim
        cache.released                set only after cache-dependent recovery is safe
        submit.claim/                  mkdir: at most one sbatch per attempt
        calling_sbatch                 written right before the single sbatch call
        receipt                        sbatch --parsable output, atomically published
        slurm_job_id                   written by the batch script when it starts
        runner_entered/                mkdir: the payload runs at most once
        cancel                         tombstone: a later start runs no payload
        result.json                    payload exit code, from the batch script
        logs/slurm-%j.out|err
"""

from __future__ import annotations

import shlex
from typing import Any


def slurm_time(seconds: int) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{days}-{hours:02d}:{minutes:02d}:{secs:02d}" if days else f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _q(value: str) -> str:
    return shlex.quote(value)


# Attempt IDs are generated as ``att_`` plus 24 lowercase hex digits. Keep the
# check inside each remote script so malformed arguments cannot escape the
# attempts directory, even if a caller bypasses Python-side validation.
_ATTEMPT_ID_GUARD = r'''valid_attempt_id() {
  case "$1" in att_????????????????????????) ;; *) return 1 ;; esac
  case "${1#att_}" in *[!a-f0-9]*) return 1 ;; esac
  return 0
}'''

_CONTROL_ROOT_GUARD = r'''valid_control_root() {
  path=$1
  root_mode=${2:-full}
  case "$path" in /*) ;; *) return 1 ;; esac
  case "$path" in /|*/|*//*|*/./*|*/.|*/../*|*/..) return 1 ;; esac
  home=${HOME:-}
  if [ -n "$home" ]; then
    [ "$path" != "$home" ] || return 1
    case "$home/" in "$path/"*) return 1 ;; esac
  fi
  rest=${path#/}; cur=
  while [ -n "$rest" ]; do
    part=${rest%%/*}
    if [ "$part" = "$rest" ]; then rest=; else rest=${rest#*/}; fi
    [ -n "$part" ] || return 1
    case "$part" in .ssh|.gnupg|.config|.local|.cache) return 1 ;; esac
    cur="$cur/$part"
    [ ! -L "$cur" ] || return 1
  done
  case "$path" in /home|/root|/root/*|/etc|/etc/*|/usr|/usr/*|/var|/var/*|/boot|/boot/*|/proc|/proc/*|/sys|/sys/*|/dev|/dev/*|/bin|/bin/*|/sbin|/sbin/*|/lib|/lib/*|/lib64|/lib64/*|/opt|/opt/*|/run|/run/*)
    return 1 ;; esac
  candidate=$path; suffix=
  while [ ! -e "$candidate" ]; do
    [ ! -L "$candidate" ] || return 1
    leaf=${candidate##*/}; suffix="/$leaf$suffix"
    parent=${candidate%/*}; [ "$parent" != "$candidate" ] || return 1
    candidate=$parent
  done
  [ -d "$candidate" ] || return 1
  physical=$(CDPATH= cd -P "$candidate" 2>/dev/null && pwd -P) || return 1
  physical=$physical$suffix
  if [ -n "$home" ] && [ -d "$home" ]; then
    home_real=$(CDPATH= cd -P "$home" 2>/dev/null && pwd -P) || return 1
    [ "$physical" != "$home_real" ] || return 1
    case "$home_real/" in "$physical/"*) return 1 ;; esac
  fi
  uid=$(id -u 2>/dev/null) || return 1
  private_dir() {
    dir=$1; legacy=$2
    [ -d "$dir" ] && [ ! -L "$dir" ] || return 1
    owner=$(stat -c %u -- "$dir" 2>/dev/null) || return 1
    mode=$(stat -c %a -- "$dir" 2>/dev/null) || return 1
    [ "$owner" = "$uid" ] || return 1
    [ "$mode" = 700 ] && return 0
    [ "$legacy" = 1 ] && [ "$mode" = 755 ]
  }
  private_file() {
    file=$1; legacy=$2
    [ -f "$file" ] && [ ! -L "$file" ] || return 1
    owner=$(stat -c %u -- "$file" 2>/dev/null) || return 1
    mode=$(stat -c %a -- "$file" 2>/dev/null) || return 1
    [ "$owner" = "$uid" ] || return 1
    [ "$mode" = 600 ] && return 0
    [ "$legacy" = 1 ] && [ "$mode" = 644 ]
  }
  if [ ! -e "$path" ]; then
    [ "$root_mode" = init ] || return 1
    private_dir "$candidate" 1 || return 1
    return 0
  fi
  legacy=0; [ "$root_mode" = init ] && legacy=1
  private_dir "$path" "$legacy" || return 1
  for entry in "$path"/* "$path"/.[!.]*; do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    name=${entry##*/}
    case "$name" in attempts|cache|jobs|fence|fence.tmp|controller.lock) ;; *) return 1 ;; esac
    [ ! -L "$entry" ] || return 1
    case "$name" in
      attempts|cache|jobs)
        [ ! -e "$entry" ] || private_dir "$entry" "$legacy" || return 1
        ;;
      fence|fence.tmp|controller.lock)
        private_file "$entry" "$legacy" || return 1
        ;;
    esac
  done
  if [ "$root_mode" = full ]; then
    private_dir "$path/attempts" 0 && private_dir "$path/cache" 0 || return 1
    if [ -e "$path/jobs" ]; then private_dir "$path/jobs" 0 || return 1; fi
  fi
  return 0
}'''

CONTROL_ROOT_CHECK_SCRIPT = r'''
__CONTROL_ROOT_GUARD__
if valid_control_root "$1" full; then echo '{"ok":true}'; else echo '{"ok":false}'; fi
'''.replace("__CONTROL_ROOT_GUARD__", _CONTROL_ROOT_GUARD)


def render_batch(*, attempt_id: str, adir: str, root: str, request: dict[str, Any], spec: dict[str, Any],
                 site: dict[str, Any], bundle_digest: str | None, bundle_sha256: str | None,
                 array_index: int | None = None, job_id: int | None = None, attempt_n: int | None = None) -> str:
    """The batch script Slurm spools at submission. Self-contained by design.

    Resources come only from the normalized request, never from ``#SBATCH``
    lines inside the user's own script, which runs as the payload (§3.4).
    """
    lines = ["#!/bin/bash", f"#SBATCH --job-name=fq-{attempt_id}", f"#SBATCH --partition={request['partition']}"]
    if request.get("account"):
        lines.append(f"#SBATCH --account={request['account']}")
    if request.get("qos"):
        lines.append(f"#SBATCH --qos={request['qos']}")
    lines += [
        f"#SBATCH --time={slurm_time(request['time_s'])}",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        f"#SBATCH --cpus-per-task={int(request['cpus'])}",
        f"#SBATCH --mem={int(request['mem_mb'])}M",
        f"#SBATCH --output={adir}/logs/slurm-%j.out",
        f"#SBATCH --error={adir}/logs/slurm-%j.err",
    ]
    if request.get("gpus"):
        gtype = request.get("gres_type")
        if (site.get("gpu_request_mode") or "gres") == "gpus":
            lines.append(f"#SBATCH --gpus={int(request['gpus'])}")
        else:
            lines.append(f"#SBATCH --gres=gpu:{gtype + ':' if gtype else ''}{int(request['gpus'])}")
    if site.get("no_requeue", True):
        lines.append("#SBATCH --no-requeue")
    warn = spec["control"].get("warn_signal")
    if warn:
        # B: signals the batch shell, which forwards to the payload's own group below.
        # Without B:, Slurm signals job steps only, and this payload is not an srun step.
        lines.append(f"#SBATCH --signal=B:{warn}@{int(spec['control']['warn_before_s'])}")
    cmd = spec["command"]
    if "argv" in cmd:
        payload = " ".join(_q(a) for a in cmd["argv"])
    elif "script" in cmd:
        payload = "/bin/bash " + " ".join(_q(a) for a in [cmd["script"]["path"], *cmd["script"]["args"]])
    else:
        payload = f"/bin/bash -c {_q(cmd['wrap'])}"
    env_lines = [f"export {k}={_q(v)}" for k, v in sorted(spec["env"].items())]
    if array_index is not None:
        # Never SLURM_ARRAY_TASK_ID: each fleetq array member is its own Slurm job.
        env_lines.append(f"export FQ_ARRAY_TASK_ID={int(array_index)}")
    if attempt_n is not None:
        env_lines.append(f"export FQ_ATTEMPT={int(attempt_n)}")
    if job_id is not None:
        # On the cluster's shared filesystem, so a resubmitted job resumes on any compute node.
        ckpt = f"{root}/jobs/{int(job_id)}/checkpoint"
        env_lines += [f"FQ_CHECKPOINT_DIR={_q(ckpt)}",
                      'if [ -n "$(ls -A "$FQ_CHECKPOINT_DIR" 2>/dev/null)" ]; then FQ_RESUMED=1; else FQ_RESUMED=0; fi',
                      'mkdir -p "$FQ_CHECKPOINT_DIR" && export FQ_CHECKPOINT_DIR FQ_RESUMED']
    if "in_place" in spec["workdir"]:
        workdir = _q(spec["workdir"]["in_place"])
        extract = ""
    else:
        sha = bundle_digest.split(":", 1)[1]
        cached = f"{root}/cache/{sha}.tar.gz"
        # The archive was validated on numpi (no links, devices, absolute or
        # '..' paths) and its bytes are checked here before extraction, into a
        # fresh private directory, refusing to overwrite anything (§6.3).
        extract = "\n".join([
            'CODE="$D/code"',
            'if [ ! -d "$CODE" ]; then',
            f'  echo "{bundle_sha256}  {cached}" | sha256sum -c --status - || {{ echo "fleetq: bundle integrity check failed" >&2; fq_result 125 setup_failed; exit 125; }}',
            '  TMPC="$D/.code.$$"; mkdir -m 700 "$TMPC" || exit 125',
            f'  tar -xzf {_q(cached)} -C "$TMPC" --no-same-owner --no-same-permissions --keep-old-files || {{ fq_result 125 setup_failed; exit 125; }}',
            '  mv "$TMPC" "$CODE" || exit 125',
            'fi',
        ])
        workdir = f'"$CODE"/{_q(spec["workdir"].get("subdir") or ".")}'
    setup = spec.get("setup") or ""
    if warn:
        # The batch shell gets the signal (B:) and passes it to the main payload process
        # only -- setup execs into it -- never to children that may not handle it. `wait`
        # returns early when the trap fires, so wait until the child is really gone.
        run_payload = "\n".join([
            f"fq_warn() {{ [ -n \"${{child:-}}\" ] && kill -s {warn} \"$child\" 2>/dev/null; }}",
            f"trap fq_warn {warn}",
            f"bash -c {_q(setup + chr(10) + 'exec ' + payload)} &",
            "child=$!",
            'while :; do wait "$child"; rc=$?; kill -0 "$child" 2>/dev/null || break; done',
        ])
    else:
        run_payload = f"( {setup}\n  {payload} )\nrc=$?"
    body = f"""
set -u
umask 077
D={_q(adir)}
fq_result() {{
  printf '{{"exit_code": %s, "phase": "%s", "slurm_job_id": "%s", "restart": %s}}\\n' "$1" "$2" "${{SLURM_JOB_ID:-}}" "${{SLURM_RESTART_COUNT:-0}}" > "$D/result.json.tmp.$$" || return 1
  sync -f "$D/result.json.tmp.$$" 2>/dev/null || return 1
  mv -f "$D/result.json.tmp.$$" "$D/result.json" || return 1
  sync -f "$D" 2>/dev/null || return 1
}}
printf '%s %s\\n' "${{SLURM_JOB_ID:-}}" "${{SLURM_RESTART_COUNT:-0}}" > "$D/slurm_job_id.tmp.$$" && sync -f "$D/slurm_job_id.tmp.$$" 2>/dev/null && mv -f "$D/slurm_job_id.tmp.$$" "$D/slurm_job_id" && sync -f "$D" 2>/dev/null || {{ fq_result 125 setup_failed; exit 125; }}
# A cancel tombstone that landed before start means: run nothing.
if [ -e "$D/cancel" ]; then echo "fleetq: cancelled before start" >&2; exit 0; fi
# The payload is entered at most once per attempt, even if Slurm restarts the script (§3.3).
if ! mkdir "$D/runner_entered" 2>/dev/null; then
  echo "fleetq: payload already entered for {attempt_id}; not rerunning (restart ${{SLURM_RESTART_COUNT:-0}})" >&2
  : > "$D/replay.${{SLURM_JOB_ID:-x}}.${{SLURM_RESTART_COUNT:-0}}"
  exit 0
fi
if ! sync -f "$D" 2>/dev/null; then
  fq_result 125 setup_failed
  exit 125
fi
{extract}
cd {workdir} || {{ fq_result 125 setup_failed; exit 125; }}
{chr(10).join(env_lines)}
export FQ_ATTEMPT_ID={_q(attempt_id)}
{run_payload}
fq_result "$rc" payload
exit "$rc"
"""
    return "\n".join(lines) + "\n" + body


# The submit-once wrapper, run through `fleetctl exec --admin --op-class action`.
# Arguments: root fleet_id epoch attempt_id. Prints exactly one JSON object.
SUBMIT_WRAPPER = r'''
set -u
umask 077
root=$1; fleet=$2; epoch=$3; att=$4
j() { printf '%s\n' "$1"; exit 0; }
__ATTEMPT_ID_GUARD__
__CONTROL_ROOT_GUARD__
valid_control_root "$root" || j '{"result":"never_started","reason":"unsafe_control_root"}'
valid_attempt_id "$att" || j '{"result":"never_started","reason":"invalid_attempt_id"}'
d="$root/attempts/$att"
exec 9>"$root/controller.lock" || exit 1
flock -x 9 || exit 1
durable_replace() {
  src=$1; dst=$2
  sync -f "$src" 2>/dev/null || return 1
  mv -f "$src" "$dst" || return 1
  sync -f "${dst%/*}" 2>/dev/null || return 1
}
[ -r "$root/fence" ] || j '{"result":"never_started","reason":"no_fence"}'
read f e < "$root/fence"
[ "$f" = "$fleet" ] || j '{"result":"never_started","reason":"fleet_mismatch"}'
case "$epoch" in ''|*[!0-9]*) j '{"result":"never_started","reason":"invalid_fence"}' ;; esac
case "$e" in ''|*[!0-9]*) j '{"result":"never_started","reason":"invalid_fence"}' ;; esac
[ "$epoch" -ge "$e" ] || j '{"result":"never_started","reason":"stale_epoch"}'
[ "$epoch" -le "$e" ] || j '{"result":"never_started","reason":"unfenced_epoch"}'
[ -r "$d/batch.sh" ] && [ -e "$d/stage.ready" ] || j '{"result":"never_started","reason":"not_staged"}'
if [ -e "$d/cache.released" ]; then j '{"result":"never_started","reason":"cache_pin_released"}'; fi
if [ -s "$d/receipt" ]; then j "{\"result\":\"started\",\"receipt\":\"$(cat "$d/receipt")\",\"replay\":true}"; fi
# Refuse before creating the submit claim unless this target actually has a
# responsive Slurm controller. A generic host with a stray/mocked sbatch in
# PATH must never be treated as a configured Slurm site.
command -v sbatch >/dev/null 2>&1 && command -v scontrol >/dev/null 2>&1 || j '{"result":"never_started","reason":"slurm_unavailable"}'
scontrol ping >/dev/null 2>&1 || j '{"result":"never_started","reason":"slurm_unavailable"}'
# The claim is the boundary: once it exists, sbatch may have been called.
mkdir "$d/submit.claim" 2>/dev/null || j '{"result":"unknown","reason":"claim_exists_no_receipt"}'
sync -f "$d" 2>/dev/null || j '{"result":"unknown","reason":"claim_not_durable"}'
if [ -e "$d/cancel" ]; then j '{"result":"never_started","reason":"cancelled"}'; fi
: > "$d/calling_sbatch.tmp" && durable_replace "$d/calling_sbatch.tmp" "$d/calling_sbatch" || j '{"result":"unknown","reason":"boundary_not_durable"}'
mkdir -p "$d/logs"
out=$(sbatch --parsable "$d/batch.sh" 2>"$d/sbatch.err"); rc=$?
if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
  jid=$out
  case "$out" in
    *';'*)
      jid=${out%%;*}
      cluster=${out#*;}
      case "$cluster" in *[!A-Za-z0-9._-]*|"") j '{"result":"unknown","reason":"malformed_sbatch_receipt"}' ;; esac
      ;;
  esac
  case "$jid" in *[!0-9]*|"") j '{"result":"unknown","reason":"malformed_sbatch_receipt"}' ;; esac
  printf '%s' "$out" > "$d/receipt.tmp" && durable_replace "$d/receipt.tmp" "$d/receipt" || j '{"result":"unknown","reason":"receipt_not_durable"}'
  j "{\"result\":\"started\",\"receipt\":\"$out\"}"
fi
err=$(head -c 400 "$d/sbatch.err" | tr '"\\\n\r\t' "'    ")
# Only errors that prove nothing was accepted release the attempt; a timeout or
# lost controller contact may have accepted the job (§3.3).
case "$err" in
  *"Invalid partition"*|*"Invalid account"*|*"Invalid qos"*|*"time limit is invalid"*|*"Invalid generic resource"*|*"Requested node configuration is not available"*|*"invalid partition"*|*"Invalid job array"*|*"More processors requested than permitted"*|*"Memory required by task is not available"*)
    j "{\"result\":\"never_started\",\"reason\":\"sbatch_rejected\",\"permanent\":true,\"stderr\":\"$err\"}" ;;
  *) j "{\"result\":\"unknown\",\"reason\":\"sbatch_uncertain\",\"rc\":$rc,\"stderr\":\"$err\"}" ;;
esac
'''.replace("__ATTEMPT_ID_GUARD__", _ATTEMPT_ID_GUARD).replace(
    "__CONTROL_ROOT_GUARD__", _CONTROL_ROOT_GUARD)

# Arguments: root fleet_id epoch attempt_id staging_id [bundle_sha]. Files are
# first copied to a private staging directory. This publication step shares
# the same lock and fence check as submission, so an old controller cannot
# replace a newer controller's staged attempt after a fence has advanced.
STAGE_WRAPPER = r'''
set -u
umask 077
root=$1; fleet=$2; epoch=$3; att=$4; stage=$5; bundle_sha=${6:-}
__ATTEMPT_ID_GUARD__
__CONTROL_ROOT_GUARD__
valid_control_root "$root" || { echo '{"ok":false,"reason":"unsafe_control_root"}'; exit 0; }
valid_attempt_id "$att" || { echo '{"ok":false,"reason":"invalid_attempt_id"}'; exit 0; }
case "$stage" in ''|*[!a-f0-9]*) echo '{"ok":false,"reason":"invalid_stage_id"}'; exit 0 ;; esac
[ "${#stage}" -eq 32 ] || { echo '{"ok":false,"reason":"invalid_stage_id"}'; exit 0; }
d="$root/attempts/$att"; src="$root/cache/.stage-$stage"
exec 9>"$root/controller.lock" || exit 1
flock -x 9 || exit 1
j() { printf '%s\n' "$1"; exit 0; }
[ -r "$root/fence" ] || j '{"ok":false,"reason":"no_fence"}'
read f e < "$root/fence"
[ "$f" = "$fleet" ] || j '{"ok":false,"reason":"fleet_mismatch"}'
case "$epoch" in ''|*[!0-9]*) j '{"ok":false,"reason":"invalid_fence"}' ;; esac
case "$e" in ''|*[!0-9]*) j '{"ok":false,"reason":"invalid_fence"}' ;; esac
[ "$epoch" -eq "$e" ] || j '{"ok":false,"reason":"stale_or_unfenced_epoch"}'
[ -d "$src" ] && [ ! -L "$src" ] || j '{"ok":false,"reason":"stage_missing"}'
[ -r "$src/attempt/batch.sh" ] && [ -r "$src/attempt/manifest.json" ] || j '{"ok":false,"reason":"stage_missing"}'
chmod 600 "$src/attempt/batch.sh" "$src/attempt/manifest.json" || j '{"ok":false,"reason":"stage_permissions_failed"}'
manifest="$src/attempt/manifest.json"
[ "$(awk 'END { print NR }' "$manifest")" -eq 1 ] || j '{"ok":false,"reason":"stage_manifest_invalid"}'
[ "$(grep -o '"bundle_digest":' "$manifest" | wc -l | tr -d ' ')" = 1 ] || j '{"ok":false,"reason":"stage_manifest_invalid"}'
manifest_digest=$(sed -n 's/.*"bundle_digest": "sha256:\([0-9a-f]\{64\}\)".*/\1/p' "$manifest")
if [ -n "$bundle_sha" ]; then
  [ "$manifest_digest" = "$bundle_sha" ] || j '{"ok":false,"reason":"stage_manifest_digest_mismatch"}'
else
  grep -q '"bundle_digest": null' "$manifest" || j '{"ok":false,"reason":"stage_manifest_digest_mismatch"}'
fi
manifest_hash=$(sha256sum < "$manifest" | awk '{print $1}')
printf '%s' "$manifest_hash" | grep -Eq '^[0-9a-f]{64}$' || j '{"ok":false,"reason":"stage_manifest_hash_failed"}'
if [ -n "$bundle_sha" ]; then
  [ -r "$src/cache/$bundle_sha.tar.gz" ] || j '{"ok":false,"reason":"bundle_missing"}'
  chmod 600 "$src/cache/$bundle_sha.tar.gz" || j '{"ok":false,"reason":"bundle_permissions_failed"}'
fi
mkdir -p "$d" "$root/cache" || j '{"ok":false,"reason":"publish_dir_failed"}'
if [ -e "$d/cache.released" ]; then j '{"ok":false,"reason":"cache_pin_released"}'; fi
# Never alter an attempt once submission has crossed its durable boundary.
if [ -e "$d/submit.claim" ] || [ -e "$d/calling_sbatch" ] || [ -e "$d/receipt" ]; then
  j '{"ok":false,"reason":"already_submitted"}'
fi
if [ -n "$bundle_sha" ]; then
  cache="$root/cache/$bundle_sha.tar.gz"
  if [ -e "$cache" ]; then
    cmp -s "$src/cache/$bundle_sha.tar.gz" "$cache" || j '{"ok":false,"reason":"cache_digest_collision"}'
  else
    # A hard link publishes without ever replacing a cache entry.
    ln "$src/cache/$bundle_sha.tar.gz" "$cache" 2>/dev/null || {
      [ -e "$cache" ] && cmp -s "$src/cache/$bundle_sha.tar.gz" "$cache" || j '{"ok":false,"reason":"cache_publish_failed"}'
    }
    sync -f "$root/cache" 2>/dev/null || j '{"ok":false,"reason":"cache_not_durable"}'
  fi
fi
rm -f "$d/stage.ready" || j '{"ok":false,"reason":"stage_marker_failed"}'
mv -f "$src/attempt/batch.sh" "$d/batch.sh.tmp.$stage" || j '{"ok":false,"reason":"batch_publish_failed"}'
mv -f "$src/attempt/manifest.json" "$d/manifest.json.tmp.$stage" || j '{"ok":false,"reason":"manifest_publish_failed"}'
sync -f "$d/batch.sh.tmp.$stage" 2>/dev/null && mv -f "$d/batch.sh.tmp.$stage" "$d/batch.sh" || j '{"ok":false,"reason":"batch_not_durable"}'
sync -f "$d/manifest.json.tmp.$stage" 2>/dev/null && mv -f "$d/manifest.json.tmp.$stage" "$d/manifest.json" || j '{"ok":false,"reason":"manifest_not_durable"}'
printf '%s\n' "$manifest_hash" > "$d/manifest.sha256.tmp.$stage" && sync -f "$d/manifest.sha256.tmp.$stage" 2>/dev/null && mv -f "$d/manifest.sha256.tmp.$stage" "$d/manifest.sha256" || j '{"ok":false,"reason":"manifest_hash_not_durable"}'
sync -f "$d" 2>/dev/null || j '{"ok":false,"reason":"attempt_not_durable"}'
: > "$d/stage.ready.tmp.$stage" && sync -f "$d/stage.ready.tmp.$stage" 2>/dev/null && mv -f "$d/stage.ready.tmp.$stage" "$d/stage.ready" || j '{"ok":false,"reason":"stage_marker_not_durable"}'
sync -f "$d" 2>/dev/null || j '{"ok":false,"reason":"attempt_not_durable"}'
rm -rf "$src"
j '{"ok":true}'
'''.replace("__ATTEMPT_ID_GUARD__", _ATTEMPT_ID_GUARD).replace(
    "__CONTROL_ROOT_GUARD__", _CONTROL_ROOT_GUARD)

# Cache GC deliberately considers only canonical cache archives. Transfer
# directories (.stage-*) are left alone because fleetctl writes them outside
# this lock; their publication and all cache deletion are serialized here.
_CACHE_RETENTION_COMMON = r'''
set -u
umask 077
root=$1; fleet=$2; epoch=$3
__ATTEMPT_ID_GUARD__
__CONTROL_ROOT_GUARD__
valid_control_root "$root" || { echo '{"ok":false,"reason":"unsafe_control_root"}'; exit 0; }
exec 9>"$root/controller.lock" || { echo '{"ok":false,"reason":"lock_unavailable"}'; exit 0; }
flock -x 9 || { echo '{"ok":false,"reason":"lock_unavailable"}'; exit 0; }
if [ ! -r "$root/fence" ]; then echo '{"ok":false,"reason":"no_fence"}'; exit 0; fi
read f e < "$root/fence"
[ "$f" = "$fleet" ] || { echo '{"ok":false,"reason":"fleet_mismatch"}'; exit 0; }
case "$epoch" in ''|*[!0-9]*) echo '{"ok":false,"reason":"invalid_fence"}'; exit 0 ;; esac
case "$e" in ''|*[!0-9]*) echo '{"ok":false,"reason":"invalid_fence"}'; exit 0 ;; esac
[ "$epoch" -eq "$e" ] || { echo '{"ok":false,"reason":"stale_or_unfenced_epoch"}'; exit 0; }
private_dir() {
  [ -d "$1" ] && [ ! -L "$1" ] || return 1
  [ "$(stat -c %u -- "$1" 2>/dev/null)" = "$(id -u)" ] || return 1
  [ "$(stat -c %a -- "$1" 2>/dev/null)" = 700 ]
}
private_file() {
  [ -f "$1" ] && [ ! -L "$1" ] || return 1
  [ "$(stat -c %u -- "$1" 2>/dev/null)" = "$(id -u)" ] || return 1
  [ "$(stat -c %a -- "$1" 2>/dev/null)" = 600 ]
}
private_dir "$root/attempts" && private_dir "$root/cache" || {
  echo '{"ok":false,"reason":"unsafe_cache_layout"}'; exit 0;
}
'''

CACHE_PIN_RELEASE_SCRIPT = (_CACHE_RETENTION_COMMON + r'''
att=$4
valid_attempt_id "$att" || { echo '{"ok":false,"reason":"invalid_attempt_id"}'; exit 0; }
d="$root/attempts/$att"; manifest="$d/manifest.json"; marker="$d/cache.released"
private_dir "$d" && private_file "$manifest" || { echo '{"ok":false,"reason":"attempt_metadata_unreadable"}'; exit 0; }
[ -r "$d/manifest.sha256" ] && private_file "$d/manifest.sha256" || { echo '{"ok":false,"reason":"attempt_manifest_hash_missing"}'; exit 0; }
expected_hash=$(cat "$d/manifest.sha256"); actual_hash=$(sha256sum < "$manifest" | awk '{print $1}')
[ "$expected_hash" = "$actual_hash" ] || { echo '{"ok":false,"reason":"attempt_manifest_hash_mismatch"}'; exit 0; }
printf '%s' "$expected_hash" | grep -Eq '^[0-9a-f]{64}$' || { echo '{"ok":false,"reason":"attempt_manifest_hash_invalid"}'; exit 0; }
[ "$(awk 'END { print NR }' "$manifest")" -eq 1 ] || { echo '{"ok":false,"reason":"attempt_manifest_invalid"}'; exit 0; }
count=$(grep -o '"bundle_digest":' "$manifest" | wc -l | tr -d ' ')
[ "$count" = 1 ] || { echo '{"ok":false,"reason":"attempt_manifest_invalid"}'; exit 0; }
line=$(cat "$manifest")
digest=$(printf '%s\n' "$line" | sed -n 's/.*"bundle_digest": "sha256:\([0-9a-f]\{64\}\)".*/\1/p')
[ -n "$digest" ] || { echo '{"ok":true,"released":false,"reason":"no_bundle"}'; exit 0; }
# Never clear a pin while submission could have been accepted or while the
# exact Slurm allocation is still visible. A missing receipt after the durable
# submit claim is ambiguous and intentionally leaves the cache pinned.
submitted=0
for item in submit.claim calling_sbatch receipt slurm_job_id; do
  if [ -e "$d/$item" ] || [ -L "$d/$item" ]; then submitted=1; fi
done
if [ "$submitted" = 1 ]; then
  if [ -e "$d/submit.claim" ] || [ -L "$d/submit.claim" ]; then
    private_dir "$d/submit.claim" || { echo '{"ok":false,"reason":"submission_metadata_invalid"}'; exit 0; }
  else
    echo '{"ok":false,"reason":"submission_metadata_invalid"}'; exit 0
  fi
  for item in calling_sbatch receipt slurm_job_id; do
    if [ -e "$d/$item" ] || [ -L "$d/$item" ]; then
      private_file "$d/$item" || { echo '{"ok":false,"reason":"submission_metadata_invalid"}'; exit 0; }
    fi
  done
  [ -r "$d/receipt" ] || { echo '{"ok":false,"reason":"submission_unresolved"}'; exit 0; }
  receipt=$(cat "$d/receipt")
  jid=${receipt%%;*}
  case "$jid" in ''|*[!0-9]*) echo '{"ok":false,"reason":"receipt_invalid"}'; exit 0 ;; esac
  if [ -e "$d/slurm_job_id" ]; then
    read started_id started_restart < "$d/slurm_job_id"
    case "$started_id" in ''|*[!0-9]*) echo '{"ok":false,"reason":"slurm_job_id_invalid"}'; exit 0 ;; esac
    [ "$started_id" = "$jid" ] || { echo '{"ok":false,"reason":"slurm_job_id_mismatch"}'; exit 0; }
  fi
  command -v squeue >/dev/null 2>&1 || { echo '{"ok":false,"reason":"squeue_unavailable"}'; exit 0; }
  reply=$(squeue --noheader --jobs "$jid" --format=%i 2>&1); rc=$?
  if [ "$rc" -ne 0 ]; then
    case "$reply" in *"Invalid job id specified"*) reply="" ;; *) echo '{"ok":false,"reason":"squeue_unavailable"}'; exit 0 ;; esac
  fi
  jobs=$reply
  [ -z "$(printf '%s' "$jobs" | tr -d '[:space:]')" ] || { echo '{"ok":false,"reason":"slurm_job_still_visible"}'; exit 0; }
fi
if [ -e "$marker" ] || [ -L "$marker" ]; then
  private_file "$marker" || { echo '{"ok":false,"reason":"release_marker_invalid"}'; exit 0; }
  ts=$(cat "$marker")
  case "$ts" in ''|*[!0-9]*) echo '{"ok":false,"reason":"release_marker_invalid"}'; exit 0 ;; esac
  echo '{"ok":true,"released":true,"already":true}'
  exit 0
fi
now=$(date +%s) || { echo '{"ok":false,"reason":"clock_unavailable"}'; exit 0; }
case "$now" in ''|*[!0-9]*) echo '{"ok":false,"reason":"clock_unavailable"}'; exit 0 ;; esac
tmp="$marker.tmp.$$"
printf '%s\n' "$now" > "$tmp" && sync -f "$tmp" 2>/dev/null && mv "$tmp" "$marker" &&
  sync -f "$d" 2>/dev/null || { rm -f "$tmp"; echo '{"ok":false,"reason":"release_marker_not_durable"}'; exit 0; }
echo '{"ok":true,"released":true,"already":false}'
''').replace("__ATTEMPT_ID_GUARD__", _ATTEMPT_ID_GUARD).replace(
    "__CONTROL_ROOT_GUARD__", _CONTROL_ROOT_GUARD)

CACHE_GC_SCRIPT = (_CACHE_RETENTION_COMMON + r'''
mode=$4; purge=${5:-}
grace=604800
now=$(date +%s) || { echo '{"ok":false,"reason":"clock_unavailable"}'; exit 0; }
case "$now" in ''|*[!0-9]*) echo '{"ok":false,"reason":"clock_unavailable"}'; exit 0 ;; esac
declare -A pinned latest_release
blocked=""
add_blocked() {
  reason=$1
  [ -z "$blocked" ] || blocked="$blocked,"
  blocked="$blocked\"$reason\""
}
for adir in "$root/attempts"/* "$root/attempts"/.[!.]*; do
  [ -e "$adir" ] || [ -L "$adir" ] || continue
  att=${adir##*/}
  valid_attempt_id "$att" && private_dir "$adir" || { add_blocked "unexpected_attempt_entry"; continue; }
  manifest="$adir/manifest.json"; marker="$adir/cache.released"
  if ! private_file "$manifest" || ! private_file "$adir/manifest.sha256" || [ "$(awk 'END { print NR }' "$manifest" 2>/dev/null)" != 1 ]; then
    add_blocked "attempt_manifest_unreadable"; continue
  fi
  expected_hash=$(cat "$adir/manifest.sha256"); actual_hash=$(sha256sum < "$manifest" | awk '{print $1}')
  if [ "$expected_hash" != "$actual_hash" ] || ! printf '%s' "$expected_hash" | grep -Eq '^[0-9a-f]{64}$'; then
    add_blocked "attempt_manifest_hash_mismatch"; continue
  fi
  count=$(grep -o '"bundle_digest":' "$manifest" | wc -l | tr -d ' ')
  if [ "$count" != 1 ]; then add_blocked "attempt_manifest_invalid"; continue; fi
  line=$(cat "$manifest")
  digest=$(printf '%s\n' "$line" | sed -n 's/.*"bundle_digest": "sha256:\([0-9a-f]\{64\}\)".*/\1/p')
  if [ -z "$digest" ]; then
    if ! printf '%s\n' "$line" | grep -q '"bundle_digest": null'; then add_blocked "attempt_digest_invalid"; fi
    continue
  fi
  if [ -e "$marker" ] || [ -L "$marker" ]; then
    if ! private_file "$marker"; then add_blocked "release_marker_invalid"; continue; fi
    released=$(cat "$marker")
    case "$released" in ''|*[!0-9]*) add_blocked "release_marker_invalid"; continue ;; esac
    if [ "$released" -gt "$now" ]; then add_blocked "release_time_in_future"; continue; fi
    old=${latest_release[$digest]:-0}
    [ "$released" -le "$old" ] || latest_release[$digest]=$released
  else
    pinned[$digest]=1
  fi
  submitted=0
  for item in submit.claim calling_sbatch receipt slurm_job_id; do
    if [ -e "$adir/$item" ] || [ -L "$adir/$item" ]; then submitted=1; fi
  done
  if [ "$submitted" = 1 ]; then
    if [ -e "$adir/submit.claim" ] || [ -L "$adir/submit.claim" ]; then
      private_dir "$adir/submit.claim" || { add_blocked "submission_metadata_invalid"; continue; }
    else
      add_blocked "submission_metadata_invalid"; continue
    fi
    for item in calling_sbatch receipt slurm_job_id; do
      if [ -e "$adir/$item" ] || [ -L "$adir/$item" ]; then
        private_file "$adir/$item" || { add_blocked "submission_metadata_invalid"; continue 2; }
      fi
    done
    [ -r "$adir/receipt" ] || { add_blocked "submission_unresolved"; continue; }
    receipt=$(cat "$adir/receipt"); jid=${receipt%%;*}
    case "$jid" in ''|*[!0-9]*) add_blocked "receipt_invalid"; continue ;; esac
    if [ -e "$adir/slurm_job_id" ]; then
      read started_id started_restart < "$adir/slurm_job_id"
      [ "$started_id" = "$jid" ] || { add_blocked "slurm_job_id_mismatch"; continue; }
    fi
  fi
done

eligible=""
cache_blocked=""
for path in "$root/cache"/* "$root/cache"/.[!.]*; do
  [ -e "$path" ] || [ -L "$path" ] || continue
  name=${path##*/}
  case "$name" in .stage-*) continue ;; esac
  if ! printf '%s' "$name" | grep -Eq '^[0-9a-f]{64}\.tar\.gz$'; then
    [ -z "$cache_blocked" ] || cache_blocked="$cache_blocked,"
    cache_blocked="$cache_blocked\"unexpected_cache_entry\""
    continue
  fi
  digest=${name%.tar.gz}
  if [ -L "$path" ] || [ ! -f "$path" ] || [ "$(stat -c %u -- "$path" 2>/dev/null)" != "$(id -u)" ] || [ "$(stat -c %a -- "$path" 2>/dev/null)" != 600 ]; then
    [ -z "$cache_blocked" ] || cache_blocked="$cache_blocked,"
    cache_blocked="$cache_blocked\"cache_entry_invalid\""
    continue
  fi
  [ -z "${pinned[$digest]:-}" ] || continue
  mtime=$(stat -c %Y -- "$path" 2>/dev/null) || { [ -z "$cache_blocked" ] || cache_blocked="$cache_blocked,"; cache_blocked="$cache_blocked\"cache_stat_failed\""; continue; }
  base=$mtime; rel=${latest_release[$digest]:-0}
  [ "$rel" -le "$base" ] || base=$rel
  age=$((now - base))
  [ "$age" -ge "$grace" ] || continue
  [ -z "$eligible" ] || eligible="$eligible,"
  eligible="$eligible\"sha256:$digest\""
done

if [ -n "$cache_blocked" ]; then
  [ -z "$blocked" ] || blocked="$blocked,"
  blocked="$blocked$cache_blocked"
fi
if [ "$mode" = inspect ]; then
  if [ -n "$blocked" ]; then
    printf '{"ok":true,"mode":"inspect","eligible":[],"blocked":[%s],"grace_seconds":%s}\n' "$blocked" "$grace"
    exit 0
  fi
  printf '{"ok":true,"mode":"inspect","eligible":[%s],"blocked":[%s],"grace_seconds":%s}\n' "$eligible" "$blocked" "$grace"
  exit 0
fi
[ "$mode" = purge ] || { echo '{"ok":false,"reason":"invalid_mode"}'; exit 0; }
case "$purge" in sha256:*) purge_hex=${purge#sha256:} ;; *) echo '{"ok":false,"reason":"invalid_digest"}'; exit 0 ;; esac
printf '%s' "$purge_hex" | grep -Eq '^[0-9a-f]{64}$' || { echo '{"ok":false,"reason":"invalid_digest"}'; exit 0; }
[ -z "$blocked" ] || { printf '{"ok":false,"mode":"purge","removed":[],"blocked":[%s]}\n' "$blocked"; exit 0; }
case ",$eligible," in *,"\"$purge\"",*) ;; *) echo '{"ok":false,"mode":"purge","removed":[],"reason":"not_currently_eligible"}'; exit 0 ;; esac
path="$root/cache/$purge_hex.tar.gz"
[ -f "$path" ] && [ ! -L "$path" ] && [ "$(stat -c %u -- "$path" 2>/dev/null)" = "$(id -u)" ] || {
  echo '{"ok":false,"mode":"purge","removed":[],"reason":"cache_entry_changed"}'; exit 0;
}
rm -- "$path" && sync -f "$root/cache" 2>/dev/null || {
  echo '{"ok":false,"mode":"purge","removed":[],"reason":"purge_not_durable"}'; exit 0;
}
printf '{"ok":true,"mode":"purge","removed":["%s"],"blocked":[]}\n' "$purge"
''').replace("__ATTEMPT_ID_GUARD__", _ATTEMPT_ID_GUARD).replace(
    "__CONTROL_ROOT_GUARD__", _CONTROL_ROOT_GUARD)

# One observation session: a single squeue for known ids plus name lookups
# for attempts without a receipt, optional sacct, and control-file reads.
# Arguments: root accounting(0|1) then attempt ids.
OBSERVE_SCRIPT = r'''
set -u
root=$1; acct=$2; shift 2
__ATTEMPT_ID_GUARD__
__CONTROL_ROOT_GUARD__
valid_control_root "$root" || { printf '__END\n'; exit 0; }
ids=""
for att in "$@"; do
  valid_attempt_id "$att" || { printf '__END\n'; exit 0; }
  d="$root/attempts/$att"
  printf '__ATT %s\n' "$att"
  printf '__CLAIM %s\n' "$([ -d "$d/submit.claim" ] && echo 1 || echo 0)"
  printf '__STAGED %s\n' "$([ -r "$d/batch.sh" ] && [ -e "$d/stage.ready" ] && echo 1 || echo 0)"
  r=$(cat "$d/receipt" 2>/dev/null)
  printf '__RECEIPT %s\n' "$r"
  r=${r%%;*}
  [ -n "$r" ] && ids="$ids${ids:+,}$r"
done
printf '__SQUEUE_BEGIN\n'
if [ -n "$ids" ]; then squeue -h -j "$ids" --states=all -o '%i|%T|%r|%N|%j' 2>&1; printf '__SQUEUE_RC %s\n' "$?"; else printf '__SQUEUE_RC 0\n'; fi
printf '__NAMES_BEGIN\n'
for att in "$@"; do
  d="$root/attempts/$att"
  if [ -d "$d/submit.claim" ] && [ ! -s "$d/receipt" ]; then
    squeue -h -n "fq-$att" --states=all -o '%i|%T|%r|%N|%j'
    [ "$acct" = 1 ] && sacct -X -n -P --name="fq-$att" -S now-7days --format=JobID,JobName,State,ExitCode 2>/dev/null
  fi
done
printf '__SACCT_BEGIN\n'
if [ "$acct" = 1 ] && [ -n "$ids" ]; then sacct -X -n -P -j "$ids" -S now-7days --format=JobID,JobName,State,ExitCode; printf '__SACCT_RC %s\n' "$?"; fi
printf '__ATTEMPTS_BEGIN\n'
# Read local result files last. A batch can publish its result and exit while
# squeue/sacct runs; reading them first pairs a fresh terminal scheduler state
# with an older missing result and incorrectly classifies UNKNOWN_EXIT.
for att in "$@"; do
  d="$root/attempts/$att"
  printf '__ATT %s\n' "$att"
  printf '__STARTED %s\n' "$(cat "$d/slurm_job_id" 2>/dev/null)"
  printf '__ENTERED %s\n' "$([ -d "$d/runner_entered" ] && echo 1 || echo 0)"
  printf '__CANCEL %s\n' "$([ -e "$d/cancel" ] && echo 1 || echo 0)"
  printf '__RESULT %s\n' "$(cat "$d/result.json" 2>/dev/null | tr '\n' ' ')"
done
printf '__END\n'
'''.replace("__ATTEMPT_ID_GUARD__", _ATTEMPT_ID_GUARD).replace(
    "__CONTROL_ROOT_GUARD__", _CONTROL_ROOT_GUARD)

# Arguments: root fleet epoch — accept only a higher epoch; list attempts.
FENCE_SCRIPT = r'''
set -u
root=$1; fleet=$2; epoch=$3
__CONTROL_ROOT_GUARD__
valid_control_root "$root" init || { echo '{"accepted":false,"reason":"unsafe_control_root"}'; exit 0; }
umask 077
durable_replace() {
  src=$1; dst=$2
  sync -f "$src" 2>/dev/null || return 1
  mv -f "$src" "$dst" || return 1
  sync -f "${dst%/*}" 2>/dev/null || return 1
}
mkdir -p "$root/attempts" "$root/cache" || { echo '{"accepted":false,"reason":"root_unwritable"}'; exit 0; }
chmod 700 "$root" "$root/attempts" "$root/cache" || { echo '{"accepted":false,"reason":"root_unwritable"}'; exit 0; }
for f in "$root/fence" "$root/fence.tmp" "$root/controller.lock"; do
  [ ! -e "$f" ] || chmod 600 "$f" || { echo '{"accepted":false,"reason":"root_unwritable"}'; exit 0; }
done
exec 9>"$root/controller.lock" || { echo '{"accepted":false,"reason":"lock_unavailable"}'; exit 0; }
flock -x 9 || { echo '{"accepted":false,"reason":"lock_unavailable"}'; exit 0; }
if [ -r "$root/fence" ]; then read f e < "$root/fence"; else f=""; e=0; fi
if [ -n "$f" ] && [ "$f" != "$fleet" ]; then echo '{"accepted":false,"reason":"fleet_mismatch"}'; exit 0; fi
case "$epoch" in ''|*[!0-9]*) echo '{"accepted":false,"reason":"invalid_fence"}'; exit 0 ;; esac
case "$e" in ''|*[!0-9]*) echo '{"accepted":false,"reason":"invalid_fence"}'; exit 0 ;; esac
atts=$(ls "$root/attempts" 2>/dev/null | sed 's/.*/"&"/' | paste -sd, -)
if [ "$epoch" -le "$e" ]; then printf '{"accepted":false,"highest_epoch_seen":%s,"attempts":[%s]}\n' "$e" "$atts"; exit 0; fi
printf '%s %s\n' "$fleet" "$epoch" > "$root/fence.tmp" && durable_replace "$root/fence.tmp" "$root/fence" || { echo '{"accepted":false,"reason":"fence_not_durable"}'; exit 0; }
printf '{"accepted":true,"highest_epoch_seen":%s,"attempts":[%s]}\n' "$e" "$atts"
'''.replace("__CONTROL_ROOT_GUARD__", _CONTROL_ROOT_GUARD)

# Arguments: root fleet epoch attempt [slurm_id]. Fence check and cancel are serialized.
CANCEL_SCRIPT = r'''
set -u
umask 077
root=$1; fleet=$2; epoch=$3; att=$4; id=${5:-}
__ATTEMPT_ID_GUARD__
__CONTROL_ROOT_GUARD__
valid_control_root "$root" || { echo '{"tombstone":false,"reason":"unsafe_control_root"}'; exit 0; }
valid_attempt_id "$att" || { echo '{"tombstone":false,"reason":"invalid_attempt_id"}'; exit 0; }
exec 9>"$root/controller.lock" || { echo '{"tombstone":false,"reason":"lock_unavailable"}'; exit 0; }
flock -x 9 || { echo '{"tombstone":false,"reason":"lock_unavailable"}'; exit 0; }
if [ ! -r "$root/fence" ]; then echo '{"tombstone":false,"reason":"no_fence"}'; exit 0; fi
read f e < "$root/fence"
if [ "$f" != "$fleet" ]; then echo '{"tombstone":false,"reason":"fleet_mismatch"}'; exit 0; fi
case "$epoch" in ''|*[!0-9]*) echo '{"tombstone":false,"reason":"invalid_fence"}'; exit 0 ;; esac
case "$e" in ''|*[!0-9]*) echo '{"tombstone":false,"reason":"invalid_fence"}'; exit 0 ;; esac
if [ "$epoch" -lt "$e" ]; then printf '{"tombstone":false,"reason":"stale_epoch","highest_epoch_seen":%s}\n' "$e"; exit 0; fi
if [ "$epoch" -gt "$e" ]; then echo '{"tombstone":false,"reason":"unfenced_epoch"}'; exit 0; fi
d="$root/attempts/$att"
mkdir -p "$d" || { echo '{"tombstone":false,"reason":"tombstone_failed"}'; exit 0; }
if [ ! -e "$d/cancel" ]; then
  : > "$d/cancel.tmp" && sync -f "$d/cancel.tmp" 2>/dev/null &&
    mv -f "$d/cancel.tmp" "$d/cancel" && sync -f "$d" 2>/dev/null ||
    { echo '{"tombstone":false,"reason":"tombstone_failed"}'; exit 0; }
fi
if [ -n "$id" ]; then
  case "$id" in *[!0-9]*) echo '{"tombstone":true,"scancel_rc":null,"reason":"invalid_id"}'; exit 0 ;; esac
  scancel "$id"; rc=$?; printf '{"tombstone":true,"scancel_rc":%s}\n' "$rc"
else printf '{"tombstone":true,"scancel_rc":null}\n'; fi
'''.replace("__ATTEMPT_ID_GUARD__", _ATTEMPT_ID_GUARD).replace(
    "__CONTROL_ROOT_GUARD__", _CONTROL_ROOT_GUARD)


# Arguments: root attempt slurm_id stdout_offset stderr_offset max_bytes (offset -1 = skip).
# Bounded bytes per stream; only files inside this attempt's own logs directory.
LOGS_SCRIPT = r'''
set -u
root=$1; att=$2; id=$3; oo=$4; eo=$5; max=$6
__ATTEMPT_ID_GUARD__
__CONTROL_ROOT_GUARD__
valid_control_root "$root" || { echo '{"error":"unsafe_control_root"}'; exit 0; }
valid_attempt_id "$att" || { echo '{"error":"invalid_attempt_id"}'; exit 0; }
case "$id" in *[!0-9]*|"") echo '{"error":"bad_id"}'; exit 0 ;; esac
d="$root/attempts/$att/logs"
one() {
  f="$d/slurm-$id.$1"; off=$2
  if [ "$off" -lt 0 ]; then printf '"%s":null' "$3"; return; fi
  if [ ! -r "$f" ]; then printf '"%s":{"size":null,"data":""}' "$3"; return; fi
  size=$(wc -c < "$f" | tr -d ' ')
  data=$(tail -c +$((off + 1)) "$f" 2>/dev/null | head -c "$max" | base64 | tr -d '\n')
  printf '"%s":{"size":%s,"data":"%s"}' "$3" "$size" "$data"
}
printf '{'; one out "$oo" stdout; printf ','; one err "$eo" stderr; printf '}\n'
'''.replace("__ATTEMPT_ID_GUARD__", _ATTEMPT_ID_GUARD).replace(
    "__CONTROL_ROOT_GUARD__", _CONTROL_ROOT_GUARD)


# Arguments: root attempt workdir max_files max_bytes paths... (bash: find -print0).
# Stages approved outputs into <attempt>/outbox as numbered hard links, so one
# pull moves them and no transfer exclude can drop one. Records, one per line:
#   F <slot> <size> <b64 relpath>   M <b64 path> (missing)   R <b64 path> <reason>
# framed by S|A (staged now | already) ... D, or a single E <code> [detail].
COLLECT_STAGE_SCRIPT = r'''
set -u
root=$1; att=$2; wd=$3; maxf=$4; maxb=$5; shift 5
__ATTEMPT_ID_GUARD__
__CONTROL_ROOT_GUARD__
valid_control_root "$root" || { echo "E unsafe_control_root"; exit 0; }
valid_attempt_id "$att" || { echo "E invalid_attempt_id"; exit 0; }
d="$root/attempts/$att"; ob="$d/outbox"
private_dir "$d" 0 || { echo "E unsafe_attempt_dir"; exit 0; }
if [ -s "$ob/manifest.lines" ]; then echo A; cat "$ob/manifest.lines"; echo D; exit 0; fi
[ ! -e "$ob" ] && [ ! -L "$ob" ] || { echo "E unsafe_outbox"; exit 0; }
wdr=$(cd "$wd" 2>/dev/null && pwd -P) || { echo "E no_workdir"; exit 0; }
b64() { printf '%s' "$1" | base64 | tr -d '\n'; }
tmp=$(mktemp -d "$d/.outbox.XXXXXXXX") || { echo "E unwritable"; exit 0; }
lines="$tmp/manifest.lines"; : > "$lines"; other="$tmp/other"; : > "$other"; n=0; total=0
for rel in "$@"; do
  top="$wdr/$rel"
  if [ ! -e "$top" ] && [ ! -L "$top" ]; then echo "M $(b64 "$rel")" >> "$other"; continue; fi
  if [ -L "$top" ]; then echo "R $(b64 "$rel") symlink" >> "$other"; continue; fi
  real="$(cd "$(dirname "$top")" && pwd -P)/$(basename "$top")"
  case "$real/" in "$wdr"/*) ;; *) echo "R $(b64 "$rel") escapes_workdir" >> "$other"; continue ;; esac
  while IFS= read -r -d '' f; do
    r=${f#"$wdr"/}
    if [ -L "$f" ]; then echo "R $(b64 "$r") symlink" >> "$other"; continue; fi
    if [ ! -f "$f" ]; then echo "R $(b64 "$r") not_a_regular_file" >> "$other"; continue; fi
    size=$(stat -c %s "$f") || { rm -rf "$tmp"; echo "E stat_failed"; exit 0; }
    n=$((n + 1)); total=$((total + size))
    [ "$n" -le "$maxf" ] || { rm -rf "$tmp"; echo "E too_many_files"; exit 0; }
    slot=$(printf '%06d' $((n - 1)))
    ln "$f" "$tmp/$slot" 2>/dev/null || cp -p "$f" "$tmp/$slot" || { rm -rf "$tmp"; echo "E copy_failed"; exit 0; }
    echo "F $slot $size $(b64 "$r")" >> "$lines"
  done < <(find -P "$top" ! -type d -print0 2>/dev/null)
done
[ "$total" -le "$maxb" ] || { rm -rf "$tmp"; echo "E too_large $total"; exit 0; }
cat "$other" >> "$lines"; rm -f "$other"
mv "$tmp" "$ob" || { rm -rf "$tmp"; echo "E publish_failed"; exit 0; }
echo S; cat "$ob/manifest.lines"; echo D
'''.replace("__ATTEMPT_ID_GUARD__", _ATTEMPT_ID_GUARD).replace(
    "__CONTROL_ROOT_GUARD__", _CONTROL_ROOT_GUARD)

# Arguments: root attempt. Removes only this attempt's outbox (links; the outputs stay).
COLLECT_CLEAN_SCRIPT = r'''
set -u
__ATTEMPT_ID_GUARD__
__CONTROL_ROOT_GUARD__
valid_control_root "$1" || { echo '{"cleaned":false,"reason":"unsafe_control_root"}'; exit 0; }
valid_attempt_id "$2" || { echo '{"cleaned":false,"reason":"invalid_attempt_id"}'; exit 0; }
private_dir "$1/attempts/$2" 0 || { echo '{"cleaned":false,"reason":"unsafe_attempt_dir"}'; exit 0; }
rm -rf "$1/attempts/$2/outbox" && echo '{"cleaned":true}'
'''.replace("__ATTEMPT_ID_GUARD__", _ATTEMPT_ID_GUARD).replace(
    "__CONTROL_ROOT_GUARD__", _CONTROL_ROOT_GUARD)


def bash_argv(script: str, *args: str) -> list[str]:
    return ["bash", "-c", script, "fleetq", *args]


def sh_argv(script: str, *args: str) -> list[str]:
    """argv for running a script through fleetctl exec with no shell interpolation of our args."""
    return ["sh", "-c", script, "fleetq", *args]
