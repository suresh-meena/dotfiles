from __future__ import annotations

import hashlib
import json
import posixpath
from pathlib import Path
from typing import Any

from ..errors import ModelctlError
from .fingerprint import fingerprint_from_listing

# Recognizable model dir markers per spec §5.4
MARKERS = {"config.json"}

def _is_model_dir_listing(files: list[str]) -> bool:
    # check presence of config.json and at least one safetensors or index
    has_config = "config.json" in files
    has_weights = any(f.endswith(".safetensors") or f == "model.safetensors.index.json" for f in files)
    has_tokenizer = any(f.startswith("tokenizer") for f in files)
    return has_config and (has_weights or has_tokenizer)


def scan_local_roots(roots: list[str]) -> list[dict[str, Any]]:
    """Local filesystem scan for demo/testing - restricted to declared roots."""
    out: list[dict[str, Any]] = []
    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            continue
        for child in rp.iterdir():
            if not child.is_dir():
                continue
            try:
                files = [p.name for p in child.iterdir() if p.is_file()]
            except PermissionError:
                continue
            if _is_model_dir_listing(files):
                # fingerprint stub
                listing = []
                for p in child.iterdir():
                    if p.is_file():
                        stat = p.stat()
                        listing.append({"relative": p.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
                # hash config.json if present
                cfg_hash = None
                cfg_path = child / "config.json"
                if cfg_path.exists():
                    h = hashlib.sha256()
                    h.update(cfg_path.read_bytes())
                    cfg_hash = h.hexdigest()
                idx_hash = None
                for idx_name in ("model.safetensors.index.json", "pytorch_model.bin.index.json"):
                    idx_path = child / idx_name
                    if idx_path.exists():
                        h = hashlib.sha256()
                        h.update(idx_path.read_bytes())
                        idx_hash = h.hexdigest()
                        break
                # compute fingerprint
                from .fingerprint import fingerprint_from_listing

                fp = fingerprint_from_listing(str(child), listing, cfg_hash, idx_hash)
                total_size = sum(x["size"] for x in listing)
                out.append(
                    {
                        "canonical_path": str(child),
                        "files": listing,
                        "config_hash": cfg_hash,
                        "index_hash": idx_hash,
                        "fingerprint": fp,
                        "size_bytes": total_size,
                    }
                )
    return out


def validate_path_inside_roots(path: str, roots: list[str]) -> bool:
    # canonicalize posix
    canon = posixpath.normpath(path)
    for r in roots:
        rn = posixpath.normpath(r)
        if canon == rn or canon.startswith(rn.rstrip("/") + "/"):
            return True
    return False


def remote_scan_command(roots: list[str]) -> list[str]:
    """Return argv for remote scan helper (POSIX). The actual remote invocation is via SSH transport with strict argv encoding."""
    # We emit a python snippet that walks only declared roots; no shell interpolation.
    # The SSH adapter will execute this as `python3 -c '<snippet>'` with argv vector, not shell.
    # For now return the marker for transport layer.
    return ["python3", "-c", f"import os,json,hashlib;roots={json.dumps(roots)};print(json.dumps({{'roots':roots}}))"]


# Remote scan snippet executed as `python3 -c <snippet>` on the remote host.
# Constraints (belt and suspenders on top of argv quoting): single-quoted
# python strings only, and no $, backtick, backslash, or semicolon anywhere
# in the snippet. Roots are fed over stdin, never on the command line.
REMOTE_SCAN_SNIPPET = (
    "import json,os,hashlib,sys\n"
    "roots=json.load(sys.stdin)\n"
    "out=[]\n"
    "for r in roots:\n"
    "    try:\n"
    "        for c in os.scandir(r):\n"
    "            if not c.is_dir():\n"
    "                continue\n"
    "            try:\n"
    "                files=[f.name for f in os.scandir(c.path) if f.is_file()]\n"
    "            except OSError:\n"
    "                continue\n"
    "            has_cfg='config.json' in files\n"
    "            has_w=any(f.endswith('.safetensors') or f=='model.safetensors.index.json' for f in files)\n"
    "            has_t=any(f.startswith('tokenizer') for f in files)\n"
    "            if not (has_cfg and (has_w or has_t)):\n"
    "                continue\n"
    "            listing=[]\n"
    "            for f in os.scandir(c.path):\n"
    "                if f.is_file():\n"
    "                    st=f.stat()\n"
    "                    listing.append({'relative':f.name,'size':st.st_size,'mtime_ns':st.st_mtime_ns})\n"
    "            cfg_hash=None\n"
    "            cp=os.path.join(c.path,'config.json')\n"
    "            if os.path.exists(cp):\n"
    "                h=hashlib.sha256()\n"
    "                h.update(open(cp,'rb').read())\n"
    "                cfg_hash=h.hexdigest()\n"
    "            idx_hash=None\n"
    "            for name in ('model.safetensors.index.json','pytorch_model.bin.index.json'):\n"
    "                ip=os.path.join(c.path,name)\n"
    "                if os.path.exists(ip):\n"
    "                    h=hashlib.sha256()\n"
    "                    h.update(open(ip,'rb').read())\n"
    "                    idx_hash=h.hexdigest()\n"
    "                    break\n"
    "            out.append({'canonical_path':c.path,'files':listing,'config_hash':cfg_hash,'index_hash':idx_hash,'size_bytes':sum(x['size'] for x in listing)})\n"
    "    except OSError:\n"
    "        continue\n"
    "print(json.dumps({'roots':roots,'artifacts':out},separators=(',',':')))\n"
)


def remote_scan(transport: Any, roots: list[str]) -> list[dict[str, Any]]:
    """Scan model dirs on a remote host via SSH.

    Roots travel over stdin, never embedded in the command line. The remote
    snippet returns the same artifact shape as scan_local_roots (minus
    fingerprints, which are recomputed locally from the listing).
    """
    payload = json.dumps(roots)
    try:
        cp = transport.run(["python3", "-c", REMOTE_SCAN_SNIPPET], input_data=payload.encode(), timeout=120)
    except Exception as e:
        raise ModelctlError(code="E_SSH_UNREACHABLE", message=f"remote scan failed: {str(e)[:300]}")
    if cp.returncode != 0:
        raise ModelctlError(code="E_SSH_UNREACHABLE", message=cp.stderr.decode(errors="ignore")[:500])
    try:
        parsed = json.loads(cp.stdout.decode())
    except Exception:
        raise ModelctlError(code="E_SSH_UNREACHABLE", message="unparseable remote scan output")
    out: list[dict[str, Any]] = []
    for a in parsed.get("artifacts", []):
        cfg_hash = a.get("config_hash")
        idx_hash = a.get("index_hash")
        fp = fingerprint_from_listing(a["canonical_path"], a["files"], cfg_hash, idx_hash)
        out.append(
            {
                "canonical_path": a["canonical_path"],
                "files": a["files"],
                "config_hash": cfg_hash,
                "index_hash": idx_hash,
                "fingerprint": fp,
                "size_bytes": a.get("size_bytes", 0),
            }
        )
    return out
