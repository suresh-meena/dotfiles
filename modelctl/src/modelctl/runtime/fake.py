"""Fake vLLM-compatible HTTP runtime for local simulation (spec §3.2.2, §5.3).

Serves real /health and /v1/models endpoints on an actual port so lifecycle
verification (readiness, model identity, termination) exercises real code
paths. An optional --child spawns a helper process in the same process group
so process-group containment semantics can be verified.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _log(msg: str, log_file: Path | None) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    line = f"{ts} {msg}\n"
    if log_file is not None:
        try:
            with open(log_file, "a") as f:
                f.write(line)
        except OSError:
            sys.stderr.write(line)
            sys.stderr.flush()
    else:
        sys.stderr.write(line)
        sys.stderr.flush()


class FakeVllmHandler(BaseHTTPRequestHandler):
    served_model = "model"
    log_file: Path | None = None

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
            _log("GET /health -> 200", self.log_file)
        elif self.path == "/v1/models":
            payload = json.dumps({"data": [{"id": self.served_model}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
            _log("GET /v1/models -> 200", self.log_file)
        else:
            self.send_response(404)
            self.end_headers()
            _log(f"GET {self.path} -> 404", self.log_file)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="fake vLLM-compatible HTTP runtime")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--served-model", default="model")
    parser.add_argument("--log-file", default=None)
    parser.add_argument(
        "--child",
        action="store_true",
        help="spawn a helper child process in the same process group",
    )
    args = parser.parse_args()

    log_file = Path(args.log_file) if args.log_file else None
    FakeVllmHandler.served_model = args.served_model
    FakeVllmHandler.log_file = log_file

    child: subprocess.Popen | None = None
    if args.child:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(86400 * 365)"],
        )
        _log(f"spawned helper child pid={child.pid}", log_file)

    try:
        server = ThreadingHTTPServer((args.host, args.port), FakeVllmHandler)
    except OSError as e:
        _log(f"failed to bind {args.host}:{args.port}: {e}", log_file)
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        return 1

    _log(
        f"fake runtime listening on {args.host}:{args.port} serving model '{args.served_model}'",
        log_file,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
