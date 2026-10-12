"""fq — the fleetq client (stdlib only, Python 3.11+).

Built into the single file ``bin/fq`` together with ``fleetq/bundles.py``.

Exit codes are a stable contract (§6.1):

  0 ok (for wait: completed successfully)   1 job ended unsuccessfully
  2 refused (usage, forbidden, quota, unsatisfiable, preflight)
  3 not found   4 wait timed out, job still active   5 fleetqd unreachable
  6 authentication   7 server/version error   8 rate limited   130 interrupted

With ``--json``, stdout is exactly one JSON object. Progress goes to stderr.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import stat
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit
from pathlib import Path
from typing import Any, BinaryIO

try:  # concatenated build: bundles.py precedes this file
    bundle_build  # type: ignore[used-before-def]  # noqa: B018
except NameError:  # pragma: no cover
    from fleetq.bundles import BundleError, bundle_build, bundle_manifest_json  # type: ignore

CLIENT_VERSION = "0.1.0"
EXIT = {"ok": 0, "job_failed": 1, "refused": 2, "not_found": 3, "wait_timeout": 4, "unreachable": 5,
        "auth": 6, "server": 7, "rate_limited": 8, "interrupted": 130}
_CODE_EXIT = {"unauthorized": 6, "not_found": 3, "bundle_missing": 3, "rate_limited": 8, "version_mismatch": 7,
              "internal": 7, "draining": 5, "not_ready": 5, "insufficient_storage": 5}
WAIT_SLICE_S = 55

# Progress bars are meant to redraw a terminal row. When `fq logs` is captured
# by an agent or piped to another command, those control characters become
# literal noise and can repeat thousands of times. Keep the exact log available
# via `--raw`, while making captured human output readable by default.
_ANSI_LOG_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])"
)
_TQDM_LINE = re.compile(
    r"^(?P<label>.*?)(?P<pct>\d{1,3})%\s*\|[^|]*\|\s*"
    r"(?P<done>[\d,]+)/(?P<total>[\d,]+)(?P<tail>.*)$"
)


def _clean_log_line(line: str) -> str:
    """Render one captured line the way a terminal would leave it."""
    line = _ANSI_LOG_ESCAPE.sub("", line)
    if line.endswith("\r"):
        line = line[:-1]                      # CRLF line ending, not a redraw
    # tqdm uses CR to return to the start of the current terminal row.
    line = line[line.rfind("\r") + 1:]
    kept: list[str] = []
    for char in line:
        if char == "\b":
            if kept:
                kept.pop()
        elif char == "\t" or ord(char) >= 32:
            kept.append(char)
        # Drop other terminal control characters.
    return "".join(kept)


class _LogCleaner:
    """Clean captured output incrementally, so `fq logs -f | ...` still streams.

    `feed` returns only finished lines. Consecutive redraws of one progress bar
    are held and printed once, as their latest row, when ordinary output (or
    the end of the log) follows them.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._partial = ""
        self._progress: dict[str, str] = {}

    def _flush_progress(self, rows: list[str]) -> None:
        rows.extend(row + "\n" for row in self._progress.values())
        self._progress.clear()

    def _take(self, line: str, rows: list[str], *, newline: bool) -> None:
        match = _TQDM_LINE.match(line)
        if match is None:
            self._flush_progress(rows)
            rows.append(line + "\n" if newline else line)
            return
        label = match.group("label").strip() or "progress"
        self._progress[label] = (f"{match.group('label')}{match.group('pct')}% "
                                 f"({match.group('done')}/{match.group('total')}){match.group('tail')}")

    def feed(self, data: bytes) -> bytes:
        *lines, partial = (self._partial + self._decoder.decode(data)).split("\n")
        # A redraw overwrites everything before its CR, so a bar that never ends
        # its line is not held in full. A trailing CR is not yet a redraw: the
        # next chunk may start with the LF of a CRLF.
        cr = partial.rfind("\r", 0, len(partial) - 1)
        self._partial = partial[cr:] if cr > 0 else partial
        rows: list[str] = []
        for line in lines:
            self._take(_clean_log_line(line), rows, newline=True)
        return "".join(rows).encode("utf-8")

    def finish(self) -> bytes:
        tail = self._partial + self._decoder.decode(b"", final=True)
        self._partial = ""
        rows: list[str] = []
        line = _clean_log_line(tail)
        if line:
            self._take(line, rows, newline=False)
        self._flush_progress(rows)
        return "".join(rows).encode("utf-8")


def _clean_log_output(data: bytes) -> bytes:
    """Remove terminal redraw noise and keep only the latest row of each progress bar."""
    cleaner = _LogCleaner()
    return cleaner.feed(data) + cleaner.finish()


class ClientError(Exception):
    def __init__(self, exit_code: int, envelope: dict[str, Any]) -> None:
        super().__init__(envelope.get("error", {}).get("message", "error"))
        self.exit_code = exit_code
        self.envelope = envelope


def _err(code: str, message: str, exit_code: int, **details: Any) -> ClientError:
    return ClientError(exit_code, {"schema": "fq.error/v1", "ok": False,
                                   "error": {"code": code, "message": message, "details": details}})


# ---- configuration ---------------------------------------------------------------

def load_client_config() -> dict[str, Any]:
    path = Path(os.environ.get("FQ_CONFIG", "~/.config/fleetq/client.toml")).expanduser()
    cfg: dict[str, Any] = {}
    if path.exists():
        if path.stat().st_size > 64 * 1024:
            raise _err("invalid_argument", f"{path} is unreasonably large", 2)
        with open(path, "rb") as handle:
            cfg = tomllib.load(handle)
        unknown = set(cfg) - {"url", "token_file"}
        if unknown:
            raise _err("invalid_argument", f"unknown keys in {path}: {sorted(unknown)}", 2)
    url = os.environ.get("FQ_URL") or cfg.get("url")
    if not url:
        raise _err("invalid_argument", "no fleetqd URL: set FQ_URL or url in ~/.config/fleetq/client.toml", 2)
    if not isinstance(url, str) or url.strip() != url:
        raise _err("invalid_argument", "fleetqd URL must be an HTTPS origin", 2)
    try:
        parts = urlsplit(url)
        host, _port = parts.hostname, parts.port
    except ValueError as exc:
        raise _err("invalid_argument", f"invalid fleetqd URL: {exc}", 2) from exc
    if (not host or parts.username or parts.password or parts.path not in ("", "/")
            or parts.query or parts.fragment or
            not (parts.scheme == "https" or
                 (parts.scheme == "http" and host in ("127.0.0.1", "::1")))):
        hint = ("write http://127.0.0.1:PORT; `localhost` is not trusted to be loopback"
                if host == "localhost" else "a bare origin such as https://numpi.TAILNET.ts.net")
        raise ClientError(2, {"schema": "fq.error/v1", "ok": False, "error": {
            "code": "invalid_argument", "hint": hint,
            "message": f"fleetqd URL {url!r} must be an HTTPS origin with no path "
                       "(plain HTTP only on 127.0.0.1 or ::1)"}})
    token_file = Path(os.environ.get("FQ_TOKEN_FILE") or cfg.get("token_file") or "~/.config/fleetq/token").expanduser()
    return {"url": url.rstrip("/"), "token_file": token_file}


def read_token(path: Path) -> str:
    """Read the bearer token only from a private regular file we own (§6.1)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ClientError(6, {"schema": "fq.error/v1", "ok": False, "error": {
            "code": "unauthorized", "message": f"no token file at {path}",
            "hint": "on numpi: fleetqd token create ... --out FILE; then chmod 600 FILE and set FQ_TOKEN_FILE"}})
    if stat.S_ISLNK(st.st_mode):
        raise _err("unauthorized", f"{path} is a symlink; refusing to follow it for a credential", 6)
    if not stat.S_ISREG(st.st_mode):
        raise _err("unauthorized", f"{path} is not a regular file", 6)
    if st.st_uid != os.getuid():
        raise _err("unauthorized", f"{path} is not owned by you", 6)
    if st.st_mode & 0o077:
        raise _err("unauthorized", f"{path} is readable by others; chmod 600 it", 6)
    token = path.read_text().strip()
    if not token:
        raise _err("unauthorized", f"{path} is empty", 6)
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never carry credentials across a redirect
        return None


class Api:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        # No proxy routing for authenticated tailnet/local traffic (§6.1).
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def call(self, method: str, path: str, *, body: Any = None, data: bytes | BinaryIO | None = None,
             headers: dict[str, str] | None = None, timeout: float = 30) -> tuple[int, dict[str, Any]]:
        hdrs = {"Authorization": f"Bearer {self.token}", "X-FQ-Client-Version": CLIENT_VERSION, **(headers or {})}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=hdrs)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                envelope = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                envelope = {}
            if not envelope.get("error"):
                envelope = {"schema": "fq.error/v1", "ok": False,
                            "error": {"code": "internal", "message": f"HTTP {exc.code}"}}
            return exc.code, envelope
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise _err("daemon_unavailable", f"fleetqd unreachable at {self.url}: {exc}", 5)

    def download(self, path: str, dest: Path, *, expected_sha256: str | None = None,
                 overwrite: bool = False, timeout: float = 300) -> str:
        """Verify a streamed download before publishing it at ``dest``."""
        req = urllib.request.Request(self.url + path, method="GET",
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "X-FQ-Client-Version": CLIENT_VERSION})
        tmp: Path | None = None
        digest = hashlib.sha256()
        try:
            fd, name = tempfile.mkstemp(prefix=f".{dest.name}.fq-", dir=dest.parent)
            tmp = Path(name)
            with os.fdopen(fd, "wb") as out, self.opener.open(req, timeout=timeout) as resp:
                for block in iter(lambda: resp.read(1 << 20), b""):
                    digest.update(block)
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            got = digest.hexdigest()
            if expected_sha256 is not None and got != expected_sha256:
                raise _err("integrity", f"downloaded bytes for {dest.name!r} do not match the recorded hash", 1)
            if overwrite:
                os.replace(tmp, dest)
            else:
                # The temporary file is in dest's directory, so linking it is
                # atomic and cannot replace a file or symlink created meanwhile.
                try:
                    os.link(tmp, dest)
                except FileExistsError:
                    raise _err("already_exists", f"{dest} already exists; use --overwrite to replace it", 2)
            return got
        except ClientError:
            raise
        except urllib.error.HTTPError as exc:
            raise _err("not_found" if exc.code == 404 else "internal", f"download failed: HTTP {exc.code}",
                       _status_exit(exc.code))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise _err("daemon_unavailable", f"fleetqd unreachable at {self.url}: {exc}", 5)
        except OSError as exc:      # the local side: the destination directory, the disk
            raise _err("local_io", f"cannot write {dest}: {exc}", 2)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def ok(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        status, envelope = self.call(method, path, **kw)
        if status >= 400 or envelope.get("ok") is False:
            code = envelope.get("error", {}).get("code", "internal")
            raise ClientError(_CODE_EXIT.get(code, _status_exit(status)), envelope)
        return envelope


def _status_exit(status: int) -> int:
    """The exit code for an HTTP status whose error code the client does not know."""
    return {401: 6, 404: 3, 429: 8}.get(status, 2 if status < 500 else 7)


# ---- units ---------------------------------------------------------------------------

def parse_size_mb(text: str) -> int:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]?)i?B?", text.strip(), re.IGNORECASE)
    if not m:
        raise _err("invalid_argument", f"bad size {text!r} (use e.g. 512M, 32G)", 2)
    value, unit = float(m.group(1)), (m.group(2) or "M").upper()
    return max(1, int(value * {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[unit]))


def parse_duration_s(text: str) -> int:
    text = text.strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", text)
    if m:
        return int(float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)])
    m = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d{2})(?::(\d{2}))?", text)
    if m:
        days, a, b, c = m.groups()
        h, mnt, s = (int(a), int(b), int(c)) if c is not None else (0, int(a), int(b))
        return int(days or 0) * 86400 + h * 3600 + mnt * 60 + s
    raise _err("invalid_argument", f"bad duration {text!r} (use 4h, 90m, 1-00:00:00)", 2)


# ---- output ---------------------------------------------------------------------------

def emit(args, envelope: dict[str, Any], human: str | None = None) -> None:
    if args.json:
        sys.stdout.write(json.dumps(envelope, sort_keys=True) + "\n")
    elif human is not None:
        print(human)


def fmt_job(job: dict[str, Any]) -> str:
    where = job["placement"]["target"] or "-"
    gpus = ",".join(g[-8:] for g in job["placement"]["gpus"]) or "-"
    outcome = job["execution"]["outcome"] or ""
    reason = f" ({job['reason']})" if job.get("reason") else ""
    return (f"{job['id']:>6}  {job['name'][:24]:24}  {job['phase']:<18} {outcome:<14} {where:<14} "
            f"gpus={gpus}{reason}")


def progress(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---- commands --------------------------------------------------------------------------

def _spec_from_args(args) -> dict[str, Any]:
    forms = [bool(args.argv), bool(args.wrap), bool(args.script)]
    if sum(forms) != 1:
        raise _err("invalid_argument", "give exactly one of: -- ARGV..., --wrap 'CMD', or --script PATH", 2)
    if args.argv:
        command = {"argv": args.argv}
    elif args.wrap:
        command = {"wrap": args.wrap}
    else:
        command = {"script": {"path": args.script, "args": args.script_args or []}}
    env = {}
    for item in args.env or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise _err("invalid_argument", f"--env expects KEY=VALUE, got {item!r}", 2)
        env[key] = value
    if args.env_file:
        for lineno, line in enumerate(Path(args.env_file).read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
                raise _err("invalid_argument", f"{args.env_file}:{lineno}: expected KEY=VALUE (never sourced as shell)", 2)
            env[key.strip()] = value
    if args.gpus is None:
        raise _err("invalid_argument", "--gpus is required (use --gpus 0 for CPU-only jobs)", 2)
    spec: dict[str, Any] = {
        "name": args.name or (Path(args.argv[0]).name if args.argv else "job"),
        "command": command,
        "placement": {"on": args.on.split(",") if args.on else None,
                      "each": args.each.split(",") if args.each else None,
                      "allow_clusters": args.allow_clusters, "queue": args.queue,
                      "account": args.account, "qos": args.qos,
                      "spill": _spill(args)},
        "resources": {"gpus": args.gpus,
                      "vram_mb": parse_size_mb(args.vram) if args.vram else None,
                      "gpu_model": args.gpu_model,
                      "cpus": args.cpus, "mem_mb": parse_size_mb(args.mem) if args.mem else None,
                      "time_s": parse_duration_s(args.time) if args.time else None,
                      "scratch_mb": parse_size_mb(args.scratch) if args.scratch else 0},
        "needs": args.needs or [], "needs_rw": args.needs_rw or [],
        "env": env, "setup": args.setup,
        "control": {"priority": args.priority, "hold": args.hold,
                    "retry": max(args.retry, args.resume or 0),
                    "retry_on": sorted(set(args.retry_on.split(",") if args.retry_on else [])
                                       | ({"timeout", "preempted", "node_fail"} if args.resume else set())),
                    "warn_signal": args.warn_signal or ("USR1" if args.resume else None),
                    "warn_before_s": parse_duration_s(args.warn_before) if args.warn_before else 300,
                    "interactive": bool(getattr(args, "interactive", False)),
                    "kill_grace_s": parse_duration_s(args.kill_grace) if args.kill_grace else 60,
                    "after": [_dep(d) for d in (args.after or [])],
                    "begin": parse_begin(args.begin) if args.begin else None},
        "collect": args.collect or [],
        "provenance": {"client": f"fq/{CLIENT_VERSION}", "cwd": os.getcwd(), "host": os.uname().nodename},
    }
    if args.array:
        spec["control"]["array"] = _array(args.array)
    return spec


def _spill(args) -> dict[str, Any] | None:
    if bool(args.spill_after) != bool(args.spill_to):
        raise _err("invalid_argument", "--spill-after and --spill-to go together", 2)
    if not args.spill_to:
        return None
    return {"after_s": parse_duration_s(args.spill_after), "to": args.spill_to.split(",")}


def parse_begin(text: str) -> str:
    """``+2h``/``+90m`` from now, or a local ``YYYY-MM-DD[THH:MM[:SS]]``, as the daemon's UTC form."""
    import datetime as _dt
    text = text.strip()
    if text.startswith("+"):
        when = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=parse_duration_s(text[1:]))
    else:
        try:
            when = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise _err("invalid_argument", f"bad --begin {text!r} (use +2h or 2026-09-24T09:00)", 2)
        when = when.astimezone(_dt.timezone.utc)          # a naive time is local time
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dep(text: str) -> dict[str, Any]:
    kind, _, ref = text.rpartition(":")
    if ref.startswith("grp_"):
        return {"group": ref, "type": kind or "afterok"}      # every member of an --each group or array
    if not ref.isdigit():
        raise _err("invalid_argument", f"--after expects [TYPE:]JOBID or [TYPE:]GROUP, got {text!r}", 2)
    return {"job": int(ref), "type": kind or "afterok"}


def _array(text: str) -> dict[str, Any]:
    spec, _, throttle = text.partition("%")
    indices: list[int] = []
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        indices.extend(range(int(lo), int(hi or lo) + 1))
    return {"indices": indices, "throttle": int(throttle) if throttle else None}


def _upload_bundle(api: Api, directory: Path, extra_excludes: list[str]) -> str:
    cache = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "fleetq" / "bundles"
    cache.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cache, suffix=".tar.gz")
    os.close(fd)
    try:
        try:
            info = bundle_build(directory, Path(tmp), extra_excludes=tuple(extra_excludes))
        except BundleError as exc:
            raise _err("bundle_too_large" if exc.code in ("too_large", "file_too_large", "too_many_members",
                                                          "limits_expanded") else "bundle_invalid",
                       f"{exc.code}: {exc.message}", 2)
        status, _ = api.call("HEAD", f"/api/v1/bundles/{info.digest}")
        if status != 200:
            progress(f"uploading snapshot {info.digest[:19]}… ({info.compressed_bytes} bytes, {info.members} files)")
            with open(tmp, "rb") as source:
                api.ok("PUT", f"/api/v1/bundles/{info.digest}", data=source,
                       headers={"Content-Type": "application/gzip",
                                "Content-Length": str(os.fstat(source.fileno()).st_size)}, timeout=600)
        for rel, reason in info.skipped[:10]:
            progress(f"  skipped {rel}: {reason}")
        return info.digest
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def cmd_submit(api: Api, args) -> int:
    spec = _spec_from_args(args)
    if args.in_place:
        spec["workdir"] = {"in_place": args.in_place}
        if args.dry_run:
            emit(args, {"schema": "fq.dryrun/v1", "ok": True, "spec": spec},
                 f"would submit code already at {args.in_place}; nothing to upload\n"
                 + json.dumps(spec, indent=2, sort_keys=True))
            return 0
    else:
        root = Path(args.dir or ".").resolve()
        if args.dry_run:
            fd, tmp = tempfile.mkstemp(suffix=".tar.gz")
            os.close(fd)
            info = bundle_build(root, Path(tmp), extra_excludes=tuple(args.exclude or []))
            os.unlink(tmp)
            emit(args, {"schema": "fq.dryrun/v1", "ok": True, "spec": spec,
                        "bundle": json.loads(bundle_manifest_json(info))},
                 f"would upload {info.members} files, {info.compressed_bytes} bytes as {info.digest}")
            return 0
        spec["workdir"] = {"bundle": _upload_bundle(api, root, args.exclude or []), "subdir": "."}
    resp = _post_job(api, args, spec)
    job_ids = resp["jobs"]
    if args.wait:
        # Before blocking, so an interrupted or failed wait still leaves the ids behind.
        progress("submitted job " + ", ".join(str(j) for j in job_ids)
                 + (" (replayed: already submitted)" if resp.get("idempotent_replay") else ""))
    if not args.wait:
        emit(args, resp, "\n".join(f"submitted job {j}" for j in job_ids)
             + (" (replayed: already submitted)" if resp.get("idempotent_replay") else ""))
        return 0
    return _wait_and_report(api, args, job_ids)


def _post_job(api: Api, args, spec: dict[str, Any]) -> dict[str, Any]:
    key = args.idempotency_key or f"cli-{secrets.token_hex(16)}"
    if not 0 < len(key) <= 200 or not all(" " <= char <= "~" for char in key):
        raise _err("invalid_argument", "--idempotency-key must be 1-200 printable ASCII characters", 2)
    # Named by digest: a key is free text, and must not choose where the file goes.
    pending = _pending_dir() / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
    # Saved before transmission, so a lost response is recoverable, not a duplicate (§6.1).
    pending.write_text(json.dumps({"key": key, "spec": spec, "ts": time.time()}))
    try:
        resp = _with_retries(lambda: api.ok("POST", "/api/v1/jobs", body=spec, headers={"Idempotency-Key": key}))
    except ClientError as exc:
        if exc.exit_code == 5:
            exc.envelope["error"]["hint"] = (f"the response was lost; re-run with --idempotency-key {key} to "
                                             "retrieve the job without submitting it twice")
        elif exc.exit_code != 7:
            pending.unlink(missing_ok=True)    # a definite answer: no job was created
        raise
    pending.unlink(missing_ok=True)
    return resp


def _pending_dir() -> Path:
    d = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "fleetq" / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _with_retries(fn, attempts: int = 3):
    for i in range(attempts):
        try:
            return fn()
        except ClientError as exc:
            retry_after = exc.envelope.get("error", {}).get("retry_after")
            if exc.exit_code in (5, 8) and i < attempts - 1:
                time.sleep(min(float(retry_after or 2 * (i + 1)), 30))
                continue
            raise


def _wait_and_report(api: Api, args, job_ids: list[int]) -> int:
    deadline = time.monotonic() + (parse_duration_s(args.timeout) if args.timeout else 10**9)
    docs: list[dict[str, Any]] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        slice_s = min(WAIT_SLICE_S, remaining)
        if len(job_ids) == 1:
            env = _with_retries(lambda: api.ok("GET", f"/api/v1/jobs/{job_ids[0]}/wait?until=terminal&timeout={slice_s}",
                                               timeout=slice_s + 30))
            docs = [env["job"]]
        else:
            env = _with_retries(lambda: api.ok("POST", "/api/v1/wait", body={"ids": job_ids, "mode": "all",
                                                                              "until": "terminal", "timeout": slice_s},
                                               timeout=slice_s + 30))
            docs = env["jobs"]
        if all(d["terminal"] for d in docs):
            break
    if not docs:
        docs = [api.ok("GET", f"/api/v1/jobs/{j}")["job"] for j in job_ids]
    done = all(d["terminal"] for d in docs)
    envelope = {"schema": "fq.job/v1" if len(docs) == 1 else "fq.wait/v1", "ok": True,
                **({"job": docs[0]} if len(docs) == 1 else {"jobs": docs}),
                "waited": {"until": "terminal", "met": done}}
    emit(args, envelope, "\n".join(fmt_job(d) for d in docs))
    if not done:
        return EXIT["wait_timeout"]           # a wait timeout never cancels
    if getattr(args, "propagate_exit", False) and len(docs) == 1:
        code = docs[0]["execution"]["exit"]["code"]
        return code if isinstance(code, int) else EXIT["job_failed"]
    return EXIT["ok"] if all(d["success"] for d in docs) else EXIT["job_failed"]


def cmd_wait(api: Api, args) -> int:
    return _wait_and_report(api, args, list(args.jobs))


def fmt_duration(seconds: float | None) -> str:
    """squeue's [D-]HH:MM:SS, or MM:SS under an hour."""
    if seconds is None:
        return "-"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _display_reason(j: dict[str, Any]) -> str:
    import datetime as _dt
    if j["phase"] == "HELD":
        return "held"
    if j.get("not_before") and j["phase"] == "PENDING":
        when = _dt.datetime.strptime(j["not_before"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=_dt.timezone.utc)
        if when > _dt.datetime.now(_dt.timezone.utc):
            return "begins " + when.astimezone().strftime("%Y-%m-%d %H:%M")
    if j["phase"] in ("RUNNING", "SUBMITTED") and j.get("gpu_ids"):
        return ",".join(g[-8:] for g in j["gpu_ids"])
    if j["phase"] == "TERMINAL":
        return f"exit {j['exit_code']}" if j.get("exit_code") not in (None, 0) else (j["reason"] or "")
    return j["reason"] or ""


QUEUE_COLUMNS = {
    "id": ("JOBID", 7, lambda j: str(j["id"])), "name": ("NAME", 20, lambda j: j["name"] or ""),
    "user": ("USER", 10, lambda j: j["owner"]), "st": ("ST", 3, lambda j: j["st"]),
    "where": ("WHERE", 18, lambda j: j["where"] or "-"), "gpus": ("GPUS", 4, lambda j: str(j["gpus"])),
    "time": ("TIME", 10, lambda j: fmt_duration(j["elapsed_s"]) if j["started"] else "0:00"),
    "limit": ("LIMIT", 10, lambda j: fmt_duration(j["limit_s"])),
    "prio": ("PRIO", 5, lambda j: str(j["effective_priority"])),
    "pos": ("POS", 4, lambda j: str(j["position"]) if j["position"] else "-"),
    "slurm": ("SLURMID", 9, lambda j: j["remote_id"] if j["backend"] == "slurm" and j["remote_id"] else "-"),
    "attempt": ("ATT", 3, lambda j: str(j["attempt"])), "group": ("GROUP", 20, lambda j: j["group_id"] or "-"),
    "submitted": ("SUBMITTED", 19, lambda j: (j["submitted"] or "")[:19]),
    "wait": ("WAIT", 10, lambda j: fmt_duration(j.get("wait_s"))),
    "gpuh": ("GPU-H", 8, lambda j: f"{j.get('gpu_hours', 0):.2f}"),
    "outcome": ("OUTCOME", 13, lambda j: j["outcome"] or ""),
    "reason": ("REASON", 0, _display_reason),
}
DEFAULT_QUEUE_FORMAT = "id,name,user,st,where,gpus,time,limit,prio,pos,reason"
DEFAULT_HISTORY_FORMAT = "id,name,st,where,gpus,wait,time,gpuh,outcome,reason"


def _table(rows: list[dict[str, Any]], fmt: str, header: bool) -> str:
    cols = []
    for key in fmt.split(","):
        if key.strip() not in QUEUE_COLUMNS:
            raise _err("invalid_argument", f"unknown column {key!r}; choose from {','.join(QUEUE_COLUMNS)}", 2)
        cols.append(QUEUE_COLUMNS[key.strip()])
    lines = []
    if header:
        lines.append(" ".join(f"{title:<{width}}" if width else title for title, width, _ in cols).rstrip())
    for j in rows:
        cells = []
        for _, width, get in cols:
            text = str(get(j))
            cells.append(f"{text[:width]:<{width}}" if width else text)
        lines.append(" ".join(cells).rstrip())
    return "\n".join(lines)


def _queue_query(args) -> str:
    import urllib.parse
    params = {"user": args.user, "name": args.name, "where": args.where, "group": args.group,
              "state": args.state, "all_users": "true" if args.all_users else None,
              "finished": "true" if args.all else None, "limit": str(args.limit)}
    return urllib.parse.urlencode({k: v for k, v in params.items() if v})


def cmd_queue(api: Api, args) -> int:
    """squeue for the fleet: a read of numpi only, safe to repeat and to --watch."""
    import time as _time
    if args.watch and args.json:
        raise _err("invalid_argument", "--watch redraws a table; with --json, poll `fq --json q` instead", 2)
    while True:
        env = api.ok("GET", "/api/v1/queue?" + _queue_query(args))
        if args.json:
            emit(args, env)
        else:
            body = _table(env["jobs"], args.format or DEFAULT_QUEUE_FORMAT, not args.noheader)
            if args.watch:
                sys.stdout.write("\033[H\033[J" + _time.strftime("%H:%M:%S  ") +
                                 f"{env['pending_in_line']} waiting to be placed\n")
            print(body if env["jobs"] else ("no jobs" if not args.noheader else ""))
        if not args.watch:
            return 0
        try:
            _time.sleep(max(2.0, args.watch))
        except KeyboardInterrupt:
            return EXIT["interrupted"]


def cmd_history(api: Api, args) -> int:
    """sacct for the fleet: finished jobs with wait, run time and GPU-hours."""
    import urllib.parse
    params = {"since_s": str(parse_duration_s(args.since)), "user": args.user, "where": args.where,
              "all_users": "true" if args.all_users else None, "summary": "true" if args.summary else None,
              "limit": str(args.limit)}
    env = api.ok("GET", "/api/v1/history?" + urllib.parse.urlencode({k: v for k, v in params.items() if v}))
    if args.json:
        emit(args, env)
        return 0
    if args.summary:
        t = env["summary"]
        lines = [f"{'WHERE':<18} {'JOBS':>5} {'OK':>5} {'FAIL':>5} {'CANC':>5} {'GPU-H':>9} {'MEAN WAIT':>10}"]
        for srow in t["by_where"] + [{"where": "total", **t["total"], "mean_wait_s": None}]:
            lines.append(f"{srow['where'][:18]:<18} {srow['jobs']:>5} {srow['completed']:>5} {srow['failed']:>5} "
                         f"{srow['cancelled']:>5} {srow['gpu_hours']:>9.2f} {fmt_duration(srow['mean_wait_s']):>10}")
        print("\n".join(lines))
    else:
        print(_table(env["jobs"], args.format or DEFAULT_HISTORY_FORMAT, True) if env["jobs"] else "no finished jobs")
    return 0


def cmd_show(api: Api, args) -> int:
    env = api.ok("GET", f"/api/v1/jobs/{args.job}")
    emit(args, env, json.dumps(env["job"], indent=2, sort_keys=True))
    return 0


def cmd_explain(api: Api, args) -> int:
    env = api.ok("GET", f"/api/v1/jobs/{args.job}/explain")
    lines = [f"job {env['job']}: {env['phase']}" + (f" — {env['reason']}" if env["reason"] else "")]
    for d in env["decisions"][:3]:
        detail = d["detail"]
        if isinstance(detail, list):
            for r in detail:
                lines.append(f"  {r.get('target')}: {r.get('reason')} "
                             + " ".join(f"{k}={v}" for k, v in r.items() if k not in ("target", "reason")))
        else:
            lines.append(f"  {d['decision']}: {detail}")
    emit(args, env, "\n".join(lines))
    return 0


def _select_jobs(api: Api, args) -> list[int]:
    """Explicit ids, or every one of your unfinished jobs matching --name/--group/--phase."""
    import fnmatch
    ids = list(getattr(args, "jobs", None) or [])
    if not (args.name or args.group or args.phase):
        if not ids:
            raise _err("invalid_argument", "name at least one job, or select with --name/--group/--phase", 2)
        return ids
    before = None
    while True:
        page = api.ok("GET", "/api/v1/jobs?active=true&limit=200" + (f"&before={before}" if before else ""))
        for j in page["jobs"]:
            if ((not args.name or fnmatch.fnmatchcase(j["name"] or "", args.name))
                    and (not args.group or j.get("group_id") == args.group)
                    and (not args.phase or j["phase"] == args.phase.upper())):
                ids.append(j["id"])
        before = page.get("next_before")
        if not before:
            return sorted(set(ids))


def cmd_action(api: Api, args) -> int:
    if args.action == "priority":
        env = api.ok("POST", f"/api/v1/jobs/{args.job}/priority", body={"priority": args.value})
        emit(args, env, fmt_job(env["job"]))
        return 0
    single = hasattr(args, "job") or (len(args.jobs) == 1 and not (args.name or args.group or args.phase))
    if single:
        # One named job keeps the plain job envelope (what scripts already parse).
        job_id = args.job if hasattr(args, "job") else args.jobs[0]
        env = api.ok("POST", f"/api/v1/jobs/{job_id}/{args.action}")
        emit(args, env, fmt_job(env["job"]) if args.action != "requeue"
             else f"job {job_id} requeued as {env['job']['id']}")
        return 0
    ids = _select_jobs(api, args)
    results, failed = [], 0
    for job_id in ids:
        try:
            env = api.ok("POST", f"/api/v1/jobs/{job_id}/{args.action}")
            results.append({"job": job_id, "ok": True, "phase": env["job"]["phase"]})
            if not args.json:
                print(fmt_job(env["job"]))
        except ClientError as exc:
            failed += 1
            err = exc.envelope.get("error", {})
            results.append({"job": job_id, "ok": False, "error": err})
            if not args.json:
                print(f"fq: job {job_id}: {err.get('code')}: {err.get('message')}", file=sys.stderr)
    if args.json:
        emit(args, {"schema": "fq.actions/v1", "ok": failed == 0, "action": args.action, "results": results})
    if not ids and not args.json:
        print("no jobs matched")
    return 0 if failed == 0 else 2


def cmd_modify(api: Api, args) -> int:
    patch: dict[str, Any] = {}
    res = {k: v for k, v in {
        "gpus": args.gpus, "vram_mb": parse_size_mb(args.vram) if args.vram else None, "gpu_model": args.gpu_model,
        "cpus": args.cpus, "mem_mb": parse_size_mb(args.mem) if args.mem else None,
        "time_s": parse_duration_s(args.time) if args.time else None,
        "scratch_mb": parse_size_mb(args.scratch) if args.scratch else None}.items() if v is not None}
    if res:
        patch["resources"] = res
    placement = {k: v for k, v in {"on": args.on.split(",") if args.on else None, "queue": args.queue,
                                   "account": args.account, "qos": args.qos}.items() if v is not None}
    if args.allow_clusters:
        placement["allow_clusters"] = True
    if placement:
        patch["placement"] = placement
    control = {k: v for k, v in {"priority": args.priority, "retry": args.retry,
                                 "begin": parse_begin(args.begin) if args.begin else None}.items() if v is not None}
    if args.begin_now:
        control["begin"] = None
    if control:
        patch["control"] = control
    if args.name:
        patch["name"] = args.name
    if not patch:
        raise _err("invalid_argument", "nothing to change", 2)
    env = api.ok("PATCH", f"/api/v1/jobs/{args.job}", body=patch)
    emit(args, env, fmt_job(env["job"]))
    return 0


def cmd_nodes(api: Api, args) -> int:
    env = api.ok("GET", "/api/v1/nodes")
    lines = []
    for n in env["nodes"]:
        drain = f" drained({n['drain']['reason']})" if n["drain"] else ""
        lines.append(f"{n['id']:<16} {n['backend']:<6} {n['mode'] or '-':<10} {n['state']:<12} "
                     f"{'enabled' if n['enabled'] else 'disabled'}{drain}")
        for g in n["gpus"]:
            lines.append(f"    {g['uuid']}  {g['model'] or '':<16} job={g['fleetq_job'] or '-'}")
    emit(args, env, "\n".join(lines) or "no nodes")
    return 0


# NVML start-up failures, by the name Fleetmon's helper reports.
GPU_ERRORS = {
    "nvmlerror_librmversionmismatch": "NVIDIA driver and library versions differ; reboot the host",
    "nvmlerror_drivernotloaded": "the NVIDIA driver is not loaded",
    "nvmlerror_librarynotfound": "the NVML library is missing",
    "nvmlerror_nopermission": "no permission to read the GPUs",
}


def _gpu_error_notes(hosts: list) -> str:
    return "".join(f"\n{host['target']}: GPUs unreadable ({GPU_ERRORS.get(host['gpu_error'], host['gpu_error'])}), "
                   "so none of its GPUs are listed or counted." for host in hosts if host.get("gpu_error"))


def fmt_usage(env: dict) -> str:
    def text(value):
        return "".join(c for c in str(value) if c.isprintable()) if value is not None else "-"

    def percent(value):
        return f"{value * 100:.0f}%" if value is not None else "-"

    def memory(used, total):
        def gib(value):
            return f"{value / 2**30:.1f}" if value is not None else "?"
        return f"{gib(used)}/{gib(total)}" if used is not None or total is not None else "-"

    rows = [["MACHINE", "STATE", "CPU", "RAM GiB", "GPU", "MODEL", "GPU %", "VRAM GiB", "FREE", "AGE"]]
    counts = {"idle": 0, "busy": 0, "unknown": 0}
    for host in env["hosts"]:
        for gpu in host["gpus"] or [None]:
            gpu = gpu or {}
            state = host["state"]
            if host["stale"] and state not in {"stale", "unknown"}:
                state += " (stale)"
            verdict = gpu.get("availability")
            if gpu:
                counts[verdict if verdict in counts else "unknown"] += 1
            rows.append([
                text(host["target"]), text(state), percent(host["cpu_busy_fraction"]),
                memory(host["ram_used_bytes"], host["ram_total_bytes"]),
                text(gpu.get("index")), text(gpu.get("model") or ("unreadable" if host.get("gpu_error") else None)),
                percent(gpu.get("utilization_fraction")),
                memory(gpu.get("vram_used_bytes"), gpu.get("vram_total_bytes")),
                {"idle": "idle", "busy": "busy"}.get(verdict, "?" if gpu or host.get("gpu_error") else "-"),
                fmt_duration(host["sample_age_s"]),
            ])
    if len(rows) == 1:
        output = "no machines in Fleetmon's cached inventory"
    else:
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        output = "\n".join("  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
                           for row in rows)
        output += (f"\n{counts['idle']} idle, {counts['busy']} busy, {counts['unknown']} unknown GPUs. "
                   "Cached telemetry; memory is used/total, AGE is sample age, '-' means unavailable.\n"
                   "FREE is Fleetmon's idle rule (two fresh idle samples); '?' means not proven either way. "
                   "A GPU idle here may still be reserved by fleetq: see `fq nodes`.")
        if any(host["gpus"] for host in env["hosts"]) and not any(
                gpu.get("availability") for host in env["hosts"] for gpu in host["gpus"]):
            output += "\nThis Fleetmon/fleetqd reports no idle verdicts; upgrade both to fill FREE."
        output += _gpu_error_notes(env["hosts"])
    if env["source"]["state"] != "live":
        output += f"\nFleetmon feed {env['source']['state']}; showing the last cached measurements."
    return output


def cmd_usage(api: Api, args) -> int:
    path = "/api/v1/usage"
    if args.machine is not None:
        path += "?machine=" + quote(args.machine, safe="")
    env = api.ok("GET", path)
    gpus = [gpu for host in env["hosts"] for gpu in host["gpus"]]
    if getattr(args, "idle", False) and gpus and all(gpu.get("availability") is None for gpu in gpus):
        # Without verdicts every GPU would be filtered out, which reads as "none are free".
        raise ClientError(EXIT["server"], {"schema": "fq.error/v1", "ok": False, "error": {
            "code": "feature_unavailable", "message": "this Fleetmon/fleetqd does not report idle verdicts",
            "hint": "upgrade Fleetmon and fleetqd on numpi; `fq usage` still shows measured utilization"}})
    if getattr(args, "idle", False):
        notes = _gpu_error_notes(env["hosts"])
        hosts = [{**host, "gpus": [g for g in host["gpus"] if g.get("availability") == "idle"]}
                 for host in env["hosts"]]
        env = {**env, "hosts": [host for host in hosts if host["gpus"]]}
        if not env["hosts"]:
            emit(args, env, "no GPU is provably idle right now (`fq usage` shows why)" + notes)
            return 0
        # A host whose GPUs are unreadable has none listed, so it is filtered out above.
        emit(args, env, fmt_usage(env) + notes)
        return 0
    emit(args, env, fmt_usage(env))
    return 0


def cmd_admin(api: Api, args) -> int:
    """Change one durable admission/dispatch switch on the controller."""
    switch, value = {
        "pause": ("dispatch", "paused"),
        "resume": ("dispatch", "on"),
        "accept-off": ("accept", "off"),
        "accept-on": ("accept", "on"),
    }[args.operation]
    env = api.ok("POST", f"/api/v1/admin/{switch}", body={"value": value})
    emit(args, env, f"{switch}: {value}")
    return 0


def cmd_node_action(api: Api, args) -> int:
    body = {"reason": args.reason} if args.operation == "drain" else {}
    env = api.ok("POST", f"/api/v1/nodes/{quote(args.name, safe='')}/{args.operation}", body=body)
    emit(args, env, f"{args.name}: {args.operation}")
    return 0


def cmd_logs(api: Api, args) -> int:
    """Read the numpi log cache. Never causes a remote read; --follow just re-reads the cache."""
    import base64
    import time as _time

    stream = "stderr" if args.err else "stdout"
    base_q = f"/api/v1/jobs/{args.job}/logs?stream={stream}" + (f"&attempt={args.attempt}" if args.attempt else "")
    offset = args.offset
    if args.tail is not None and offset is None:
        head = api.ok("GET", base_q + "&max_bytes=0")
        offset = max(head.get("base", 0), head.get("end", 0) - args.tail)
    generation = None
    collected = bytearray()
    last: dict[str, Any] = {}
    out = sys.stdout.buffer
    cleaner = _LogCleaner() if not args.json and not args.raw and not out.isatty() else None
    try:
        while True:
            q = base_q + "&max_bytes=262144" + (f"&offset={offset}" if offset is not None else "")
            q += f"&generation={generation}" if generation is not None else ""
            env = api.ok("GET", q)
            last = env
            if env.get("reset") and generation is not None:
                print(f"fq: log restarted on the node (generation {env['generation']})", file=sys.stderr)
            if env.get("gap"):
                gap = env["gap"]
                print(f"fq: {gap['to'] - gap['from']} bytes were evicted from the cache", file=sys.stderr)
            generation = env.get("generation", 0)
            data = base64.b64decode(env.get("data_b64") or "")
            if args.json:
                collected += data
            elif data:
                out.write(cleaner.feed(data) if cleaner is not None else data)
                out.flush()
            offset = env.get("next_offset", 0)
            if offset < env.get("end", 0):
                continue                               # more is cached: keep paging
            if not args.follow or env.get("complete"):
                break
            _time.sleep(2.0)
    except KeyboardInterrupt:
        if cleaner is not None:
            out.write(cleaner.finish())
            out.flush()
        return 130
    if args.json:
        emit(args, {**last, "data_b64": base64.b64encode(bytes(collected)).decode()})
    else:
        if cleaner is not None:
            out.write(cleaner.finish())
            out.flush()
        if not last.get("complete") and last.get("attempt") is not None:
            age = last.get("age_s")
            print("fq: log is still being collected" + (f" (cache {age:.0f}s old)" if age is not None else ""),
                  file=sys.stderr)
    return 0


def cmd_fetch(api: Api, args) -> int:
    """Download a job's collected outputs; each file is checked against the recorded hash."""
    import urllib.parse
    listing = api.ok("GET", f"/api/v1/jobs/{args.job}/artifacts")
    files = [f for f in listing["files"] if f["state"] == "COMPLETE"
             and (args.attempt is None or f["attempt"] == args.attempt)]
    if args.attempt is None and files:
        latest = max(f["attempt"] for f in files)
        files = [f for f in files if f["attempt"] == latest]
    if args.paths:
        files = [f for f in files if any(f["relpath"] == p or f["relpath"].startswith(p.rstrip("/") + "/")
                                         for p in args.paths)]
    out_root = Path(args.output).expanduser().resolve()
    fetched = []
    for f in files:
        expected = f.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise _err("integrity", f"{f['relpath']}: completed artifact has no valid SHA-256", 1)
        requested = Path(f["relpath"])
        if requested.is_absolute() or requested.name in ("", ".", ".."):
            raise _err("invalid_argument", f"refusing artifact path {f['relpath']!r} outside {out_root}", 2)
        dest_parent = (out_root / requested).parent.resolve()
        if dest_parent != out_root and out_root not in dest_parent.parents:
            raise _err("invalid_argument", f"refusing artifact path {f['relpath']!r} outside {out_root}", 2)
        # Resolve parent symlinks for containment, but leave the final path
        # component unresolved so a destination symlink itself is never followed.
        dest = dest_parent / requested.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        q = urllib.parse.quote(f["relpath"])
        got = api.download(f"/api/v1/jobs/{args.job}/artifacts/{q}?attempt={f['attempt']}", dest,
                           expected_sha256=expected, overwrite=args.overwrite)
        fetched.append({"relpath": f["relpath"], "path": str(dest), "size": f["size"], "sha256": got})
    env = {"schema": "fq.fetch/v1", "ok": True, "job": args.job, "state": listing["state"], "files": fetched}
    emit(args, env, "\n".join(x["path"] for x in fetched) or f"job {args.job}: no collected outputs "
                                                              f"(artifacts {listing['state']})")
    return 0 if fetched or not listing["files"] else 1


def _fleetctl() -> str:
    tool = os.environ.get("FQ_FLEETCTL") or shutil.which("fleetctl")
    if not tool:
        raise _err("invalid_argument", "fq shell uses your own fleetctl; none is on PATH (or set FQ_FLEETCTL)", 2)
    return tool


def cmd_shell(api: Api, args) -> int:
    """A shell -- or one command -- inside a running workstation job, with its GPUs.

    fleetqd only says where; your own fleetctl and SSH access open the session.
    The shell runs in a scope bound to the job, so it ends when the job does.
    """
    info = api.ok("GET", f"/api/v1/jobs/{args.job}/shell")
    command = list(getattr(args, "argv", None) or [])
    if args.json and not command:
        emit(args, info)
        return 0
    remote = info["argv"] + (["--", *command] if command else [])
    argv = [_fleetctl(), "exec", *([] if command else ["--tty"]), "--target", info["fleetctl_target"],
            "--", *remote]
    if not command:
        print(f"fq: shell on {info['node']} in job {info['job']} (GPUs {','.join(g[-8:] for g in info['gpus']) or '-'}); "
              "it ends when the job does", file=sys.stderr)
    return subprocess.call(argv)


def cmd_alloc(api: Api, args) -> int:
    """salloc: hold verified-idle GPUs on a workstation for interactive work."""
    import argparse as _argparse
    secs = parse_duration_s(args.time)
    sub = _argparse.Namespace(**{k: None for k in (
        "each", "vram", "gpu_model", "cpus", "mem", "scratch", "queue", "account", "qos", "exclude", "needs",
        "needs_rw", "env", "env_file", "setup", "after", "array", "retry_on", "kill_grace", "collect",
        "wrap", "script", "script_args", "warn_signal", "warn_before", "begin", "spill_after", "spill_to",
        "resume", "dir", "in_place", "on", "idempotency_key")})
    sub.__dict__.update(gpus=args.gpus, name=args.name, time=args.time, on=args.on, mem=args.mem, cpus=args.cpus,
                        vram=args.vram, gpu_model=args.gpu_model, in_place=args.in_place, dir=args.dir,
                        idempotency_key=args.idempotency_key, allow_clusters=False, priority=args.priority,
                        hold=False, retry=0, interactive=True, dry_run=False, json=args.json,
                        # Holds the GPUs until the walltime ends it, or `fq cancel` releases it.
                        argv=["sleep", str(secs + 60)])
    spec = _spec_from_args(sub)
    if args.in_place:
        spec["workdir"] = {"in_place": args.in_place}
    else:
        spec["workdir"] = {"bundle": _upload_bundle(api, Path(args.dir or ".").resolve(), []), "subdir": "."}
    job_id = _post_job(api, sub, spec)["jobs"][0]
    deadline = time.monotonic() + parse_duration_s(args.timeout)
    progress(f"fq: waiting for job {job_id} to get its GPUs (Ctrl-C leaves it queued)")
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                env = api.ok("GET", f"/api/v1/jobs/{job_id}")
                emit(args, env, f"job {job_id} is still {env['job']['phase']}: {env['job']['reason'] or ''}")
                return 4
            env = api.ok("GET", f"/api/v1/jobs/{job_id}/wait?until=started&timeout={min(WAIT_SLICE_S, left)}",
                         timeout=min(WAIT_SLICE_S, left) + 30)
            job = env["job"]
            if job["terminal"]:
                emit(args, env, f"job {job_id} ended before it started: {job['execution']['outcome']}")
                return 1
            if job["phase"] == "RUNNING":
                break
    except KeyboardInterrupt:
        return 130
    if not args.shell:
        emit(args, env, f"allocated job {job_id} on {job['placement']['target']}, GPUs "
                        f"{','.join(g[-8:] for g in job['placement']['gpus']) or '-'}\n"
                        f"  fq shell {job_id}        enter it\n  fq cancel {job_id}       release it")
        return 0
    shell_args = _argparse.Namespace(job=job_id, argv=[], json=False)
    try:
        rc = cmd_shell(api, shell_args)
    finally:
        if not args.keep:
            try:
                api.ok("POST", f"/api/v1/jobs/{job_id}/cancel")
                progress(f"fq: released job {job_id}")
            except ClientError as exc:
                progress(f"fq: could not release job {job_id}: {exc}; check `fq show {job_id}`")
    return rc


def cmd_whoami(api: Api, args) -> int:
    env = api.ok("GET", "/api/v1/whoami")
    emit(args, env, f"{env['owner']} via {env['label']} ({env['kind']}), scopes: {', '.join(env['scopes'])}")
    return 0


TOP_EPILOG = """\
exit status (stable; scripts and agents branch on it):
  0  ok (wait: the job succeeded)        5  fleetqd unreachable: retry, same --idempotency-key
  1  the job ended unsuccessfully        6  authentication (token missing, revoked, unreadable)
  2  refused or usage error: the answer  7  server or version error
  3  not found                           8  rate limited
  4  wait timed out; the job runs on   130  interrupted; any submitted job runs on

environment:
  FQ_URL         fleetqd origin, HTTPS (plain HTTP only on 127.0.0.1 or ::1)
  FQ_TOKEN_FILE  token file, mode 0600 (default ~/.config/fleetq/token)
  FQ_CONFIG      client config with `url` and `token_file` (default ~/.config/fleetq/client.toml)
  FQ_FLEETCTL    fleetctl used by `fq shell`

examples:
  fq usage --idle                                  GPUs free right now (cached; contacts no machine)
  fq --json submit --idempotency-key exp1-seed0 --gpus 1 --on rtx4090 -- python train.py --seed 0
  fq submit --gpus 1 --array 0-9%4 --name sweep -- python train.py   (task id in $FQ_ARRAY_TASK_ID)
  fq --json wait 42 --timeout 9m                   exit 4 means still running: call it again
  fq logs 42 --tail 4000 ; fq explain 42 ; fq cancel --name 'sweep*'

--json may go before or after the subcommand. Everything after a bare `--` is
the job's command, passed through untouched. Reads (q, history, usage, nodes,
logs) come from numpi's caches and never contact a machine, so they are safe
to repeat: use them, not squeue, sacct or nvidia-smi over ssh, for fleetq jobs
and Fleetmon-monitored machines. (They do not see other users' Slurm jobs.)
"""

SUBMIT_EPILOG = """\
The command goes last, after `--`:  fq submit --gpus 1 -- python train.py --lr 3e-4
or use --wrap 'CMD' (run by bash -c) or --script PATH (a file in the snapshot).

By default the current directory is snapshotted and uploaded (--dir, --exclude);
--in-place runs code that already sits at a path on the chosen machine instead.
Use --dry-run first to see the spec, and the upload, without submitting.
"""


class _Parser(argparse.ArgumentParser):
    """argparse, with a hint for the mistake everyone makes with job arguments."""

    def error(self, message: str):  # type: ignore[override]
        if "unrecognized arguments" in message:
            message += ("\n  hint: --json may go anywhere, but the job's own command and its flags go after"
                        " `--`, e.g. fq submit --gpus 1 -- python train.py --lr 3e-4")
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="fq", description="Submit and manage jobs on the fleet queue (fleetqd on numpi).",
                epilog=TOP_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="print exactly one JSON object on stdout")
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS: when absent here, the value given before the subcommand stands.
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print exactly one JSON object on stdout")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def command(name: str, help: str, **kw: Any) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, description=help[0].upper() + help[1:] + ".",
                              parents=[common], **kw)

    s = command("submit", "submit a job", epilog=SUBMIT_EPILOG,
                formatter_class=argparse.RawDescriptionHelpFormatter)
    where = s.add_argument_group("where it runs")
    where.add_argument("--on", help="run once on any one of these destinations (a,b)")
    where.add_argument("--each", help="run one job per destination (a,b)")
    where.add_argument("--allow-clusters", action="store_true",
                       help="let fleetq place it on a Slurm site without naming one (needs a token that allows it)")
    where.add_argument("--queue", help="cluster queue, e.g. kiac:a100")
    where.add_argument("--account", help="Slurm account on the cluster")
    where.add_argument("--qos", help="Slurm QOS on the cluster")
    where.add_argument("--spill-after", help="if not started within this long (e.g. 2h), also try --spill-to")
    where.add_argument("--spill-to", help="destinations to spill to, e.g. kiac:a100,amd")
    res = s.add_argument_group("resources")
    res.add_argument("--gpus", type=int, help="whole GPUs (required; 0 for CPU-only)")
    res.add_argument("--vram", help="minimum VRAM per GPU, e.g. 24G")
    res.add_argument("--gpu-model", help="only this GPU model, as `fq nodes` names it")
    res.add_argument("--cpus", type=int, help="CPU cores")
    res.add_argument("--mem", help="total RAM, e.g. 32G")
    res.add_argument("--time", help="walltime, e.g. 4h or 1-00:00:00")
    res.add_argument("--scratch", help="writable scratch space, e.g. 50G")
    code = s.add_argument_group("code and environment")
    code.add_argument("--dir", help="directory to snapshot and upload (default: .)")
    code.add_argument("--in-place", help="run code already at this path on the node (needs --on/--each)")
    code.add_argument("--exclude", action="append",
                      help="leave this pattern out of the uploaded snapshot (repeatable)")
    code.add_argument("--needs", action="append", help="a path that must be readable on the node")
    code.add_argument("--needs-rw", action="append", help="a path that must be writable on the node")
    code.add_argument("--env", action="append", help="KEY=VALUE (repeatable)")
    code.add_argument("--env-file", help="KEY=VALUE lines (never sourced as shell); wins over --env")
    code.add_argument("--setup", help="shell run before the command (e.g. activate a venv)")
    order = s.add_argument_group("ordering and retries")
    order.add_argument("--after", action="append",
                       help="[afterok|afterany|afternotok|after:]JOBID or GROUP (grp_...); repeatable")
    order.add_argument("--array", help="indices, e.g. 0-9%%2 (at most 2 at once); each task reads $FQ_ARRAY_TASK_ID")
    order.add_argument("--priority", type=int, default=0,
                       help="-1000..1000, higher is placed sooner (default 0); waiting jobs also gain priority as they age")
    order.add_argument("--name", help="job name (default: the command's basename)")
    order.add_argument("--hold", action="store_true", help="queue it held; start it later with `fq release`")
    order.add_argument("--begin", help="not before this time: +2h, or local 2026-09-24T09:00")
    order.add_argument("--retry", type=int, default=0, help="rerun up to N times on the --retry-on causes")
    order.add_argument("--retry-on", help="node_fail,exit,timeout,oom,preempted")
    order.add_argument("--warn-signal", help="signal the job this long before its walltime: USR1, USR2, INT, HUP")
    order.add_argument("--warn-before", help="how long before the walltime to signal (default 5m)")
    order.add_argument("--resume", type=int, metavar="N",
                       help="checkpointing job: warn with USR1 before walltime and resubmit up to N times after "
                            "timeout/preemption/node failure, back on the machine holding $FQ_CHECKPOINT_DIR")
    order.add_argument("--kill-grace", help="time between SIGTERM and SIGKILL on cancel (default 60s)")
    s.add_argument("--interactive", action="store_true", help=argparse.SUPPRESS)   # `fq alloc`
    out = s.add_argument_group("outputs and waiting")
    out.add_argument("--collect", action="append", help="relative output path to bring back for `fq fetch`")
    out.add_argument("--idempotency-key",
                     help="stable key for this exact request (derive it from the task); a retry with the same "
                          "key returns the original job instead of a second one")
    out.add_argument("--wait", action="store_true", help="block until the job ends (prints its id first)")
    out.add_argument("--timeout", help="with --wait: stop waiting after this long (exit 4; the job keeps running)")
    out.add_argument("--propagate-exit", action="store_true",
                     help="with --wait on one job: exit with the job's own exit code (which can collide with 2-8)")
    out.add_argument("--dry-run", action="store_true", help="print the job spec and the upload; submit nothing")
    cmd = s.add_argument_group("the command (exactly one)")
    cmd.add_argument("--wrap", help="a shell command line, run with bash -c")
    cmd.add_argument("--script", help="a script inside the uploaded snapshot, by relative path")
    cmd.add_argument("--script-args", nargs="*", help="arguments for --script")
    cmd.add_argument("argv", nargs="*", help="the command; put it after `--`")

    w = command("wait", "wait for jobs to finish")
    w.add_argument("jobs", nargs="+", type=int, help="job ids")
    w.add_argument("--timeout", help="stop waiting after this long (exit 4; the jobs keep running)")
    w.add_argument("--propagate-exit", action="store_true", help="with one job: exit with the job's exit code")

    q = command("queue", "squeue: running and waiting jobs, in dispatch order", aliases=["q"])
    q.add_argument("--all", action="store_true", help="include finished jobs")
    q.add_argument("-u", "--user", help="another user's jobs (needs manage_all)")
    q.add_argument("--all-users", action="store_true", help="everyone's jobs (needs manage_all)")
    q.add_argument("-t", "--state", help="e.g. PD,R or PENDING,RUNNING")
    q.add_argument("-n", "--name", help="glob on the job name, e.g. 'sweep-*'")
    q.add_argument("-w", "--where", help="only jobs placed on this machine or cluster")
    q.add_argument("--group", help="members of one array or --each group")
    q.add_argument("-o", "--format", help=f"columns, default {DEFAULT_QUEUE_FORMAT}")
    q.add_argument("--noheader", action="store_true", help="no column header")
    q.add_argument("--watch", type=float, nargs="?", const=5.0, metavar="SECONDS",
                   help="redraw every N s (at least 2; default 5); not with --json")
    q.add_argument("--limit", type=int, default=500, help="at most this many rows (default 500)")

    hi = command("history", "sacct: finished jobs with wait, run time and GPU-hours", aliases=["sacct"])
    hi.add_argument("--since", default="7d", help="how far back, e.g. 24h, 30d (default 7d)")
    hi.add_argument("--summary", action="store_true", help="totals per machine")
    hi.add_argument("-u", "--user", help="another user's jobs (needs manage_all)")
    hi.add_argument("--all-users", action="store_true", help="everyone's jobs (needs manage_all)")
    hi.add_argument("-w", "--where", help="only jobs that ran on this machine or cluster")
    hi.add_argument("-o", "--format", help=f"columns, default {DEFAULT_HISTORY_FORMAT}")
    hi.add_argument("--limit", type=int, default=1000, help="at most this many rows (default 1000)")

    for name, text in (("show", "one job's full record, as JSON"),
                       ("explain", "why a job is waiting, or where it was placed")):
        x = command(name, text)
        x.add_argument("job", type=int, help="job id")
    for action in ("cancel", "hold", "release"):
        x = command(action, f"{action} jobs by id, or every unfinished job matching the filters")
        x.add_argument("jobs", type=int, nargs="*", help="job ids")
        x.add_argument("--name", help="glob on the job name, e.g. 'sweep-*'")
        x.add_argument("--group", help="every member of an --each group or array (grp_...)")
        x.add_argument("--phase", help="only jobs in this phase, e.g. PENDING")
        x.set_defaults(action=action)
    for action, text in (("top", "move a job ahead of your other waiting jobs"),
                         ("requeue", "run a finished job again, as a new job")):
        x = command(action, text)
        x.add_argument("job", type=int, help="job id")
        x.set_defaults(action=action)
    m = command("modify", "change a job that has not started (what it runs cannot change)")
    m.add_argument("job", type=int, help="job id")
    for flag, text in (("--vram", "minimum VRAM per GPU, e.g. 24G"), ("--gpu-model", "only this GPU model"),
                       ("--mem", "total RAM, e.g. 32G"), ("--time", "walltime, e.g. 4h"),
                       ("--scratch", "scratch space, e.g. 50G"), ("--on", "destinations (a,b)"),
                       ("--queue", "cluster queue, e.g. kiac:a100"), ("--account", "Slurm account"),
                       ("--qos", "Slurm QOS"), ("--begin", "not before: +2h or 2026-09-24T09:00"),
                       ("--name", "job name")):
        m.add_argument(flag, help=text)
    for flag, text in (("--gpus", "whole GPUs"), ("--cpus", "CPU cores"),
                       ("--priority", "-1000..1000, higher is placed sooner"), ("--retry", "reruns allowed")):
        m.add_argument(flag, type=int, help=text)
    m.add_argument("--allow-clusters", action="store_true", help="let it be placed on a Slurm site")
    m.add_argument("--begin-now", action="store_true", help="drop a --begin time (wins over --begin)")
    x = command("priority", "set a waiting job's priority")
    x.add_argument("job", type=int, help="job id")
    x.add_argument("value", type=int, help="new priority, -1000..1000; higher is placed sooner")
    x.set_defaults(action="priority")

    sh = command("shell", "a shell (or `-- CMD`) inside a running workstation job")
    sh.add_argument("job", type=int, help="job id")
    sh.set_defaults(argv=[])
    al = command("alloc", "hold idle GPUs on a workstation for interactive work", aliases=["salloc"])
    al.add_argument("--gpus", type=int, default=1, help="whole GPUs (default 1)")
    al.add_argument("--time", default="2h", help="how long to hold them (default 2h)")
    al.add_argument("--on", help="any one of these workstations")
    for flag, text in (("--mem", "total RAM, e.g. 32G"), ("--vram", "minimum VRAM per GPU, e.g. 24G"),
                       ("--gpu-model", "only this GPU model"), ("--in-place", "work in code already at this path"),
                       ("--dir", "directory to snapshot and upload (default: .)"),
                       ("--idempotency-key", "stable key: a retry returns the same allocation")):
        al.add_argument(flag, help=text)
    al.add_argument("--cpus", type=int, help="CPU cores")
    al.add_argument("--priority", type=int, default=0, help="-1000..1000, higher is placed sooner")
    al.add_argument("--name", default="alloc", help="job name (default alloc)")
    al.add_argument("--timeout", default="10m", help="give up waiting for GPUs after this long (job stays queued)")
    al.add_argument("--shell", action="store_true", help="open a shell at once; release the GPUs when it exits")
    al.add_argument("--keep", action="store_true", help="with --shell: keep the allocation after the shell exits")

    lg = command("logs", "print a job's output from the numpi log cache")
    lg.add_argument("job", type=int, help="job id")
    lg.add_argument("--err", action="store_true", help="stderr instead of stdout")
    lg.add_argument("--tail", type=int, metavar="BYTES", help="only the last BYTES (ignored with --offset)")
    lg.add_argument("--offset", type=int, help="start at this byte offset")
    lg.add_argument("--attempt", type=int, help="an earlier attempt of a retried job")
    lg.add_argument("--follow", "-f", action="store_true", help="keep reading until the log is complete")
    lg.add_argument("--raw", action="store_true",
                    help="write exact stored bytes instead of cleaning captured terminal progress output")
    ft = command("fetch", "download a job's collected outputs (--collect), checking each hash")
    ft.add_argument("job", type=int, help="job id")
    ft.add_argument("paths", nargs="*", help="only these relative paths (default: all)")
    ft.add_argument("-o", "--output", default=".", help="directory to write into (default: .)")
    ft.add_argument("--overwrite", action="store_true", help="replace existing destination files")
    ft.add_argument("--attempt", type=int, help="an earlier attempt of a retried job")

    command("nodes", "fleetq's machines: state, drain, and the GPUs it has reserved")
    usage = command("usage", "measured GPU, CPU and memory use, and which GPUs are idle (cached)")
    usage.add_argument("machine", nargs="?", help="one Fleetmon machine (default: all)")
    usage.add_argument("--idle", action="store_true",
                       help="only GPUs Fleetmon proves idle (two fresh idle samples)")
    admin = command("admin", "pause dispatch or stop new admission while preserving cancellation")
    admin.add_argument("operation", choices=("pause", "resume", "accept-off", "accept-on"),
                       help="pause/resume dispatch, or stop/start accepting new jobs")
    node = command("node", "drain or resume one node")
    node.add_argument("operation", choices=("drain", "resume"), help="stop placing work there, or start again")
    node.add_argument("name", help="node id, as `fq nodes` lists it")
    node.add_argument("--reason", default="manual drain", help="reason recorded with a drain")
    command("whoami", "the token's owner, kind and scopes")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        split = argv.index("--")
        head, tail = argv[:split], argv[split + 1:]
    else:
        head, tail = argv, []
    parser = build_parser()
    args = parser.parse_args(head)
    if tail:
        args.argv = tail
    try:
        cfg = load_client_config()
        api = Api(cfg["url"], read_token(cfg["token_file"]))
        handlers = {"submit": cmd_submit, "wait": cmd_wait, "queue": cmd_queue, "q": cmd_queue, "show": cmd_show,
                    "explain": cmd_explain, "cancel": cmd_action, "hold": cmd_action, "release": cmd_action,
                    "priority": cmd_action, "top": cmd_action, "requeue": cmd_action, "modify": cmd_modify,
                    "nodes": cmd_nodes, "usage": cmd_usage, "node": cmd_node_action, "admin": cmd_admin,
                    "whoami": cmd_whoami, "logs": cmd_logs,
                    "history": cmd_history, "sacct": cmd_history,
                    "shell": cmd_shell, "alloc": cmd_alloc, "salloc": cmd_alloc,
                    "fetch": cmd_fetch}
        return handlers[args.cmd](api, args)
    except ClientError as exc:
        return _report(args, exc)
    except KeyboardInterrupt:
        print("fq: interrupted (any submitted job keeps running)", file=sys.stderr)
        return EXIT["interrupted"]
    except BrokenPipeError:
        # The reader stopped (`fq logs 7 | head`); that is not a failure of fq's.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT["ok"]
    except (OSError, ValueError, tomllib.TOMLDecodeError, BundleError) as exc:
        # Local trouble (a missing --env-file, a bad config, an unreadable directory) is a
        # usage error, never exit 1, which would read as "the job failed".
        return _report(args, _err("local_error", f"{type(exc).__name__}: {exc}", EXIT["refused"]))
    except Exception as exc:  # the exit-code contract outranks a traceback
        return _report(args, _err("client_error", f"fq bug: {type(exc).__name__}: {exc}", EXIT["server"]))


def _report(args, exc: ClientError) -> int:
    if args.json:
        sys.stdout.write(json.dumps(exc.envelope, sort_keys=True) + "\n")
    else:
        err = exc.envelope.get("error", {})
        print(f"fq: {err.get('code')}: {err.get('message')}", file=sys.stderr)
        if err.get("hint"):
            print(f"  hint: {err['hint']}", file=sys.stderr)
    return exc.exit_code
