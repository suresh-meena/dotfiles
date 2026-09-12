#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sqlite3
import sys
import tomllib
from pathlib import Path


def check_sqlite() -> dict[str, object]:
    connection = sqlite3.connect(":memory:")
    try:
        features: dict[str, object] = {"sqlite_version": sqlite3.sqlite_version}
        try:
            connection.execute("CREATE TABLE probe_strict (x INTEGER) STRICT")
            connection.execute("DROP TABLE probe_strict")
            features["strict_tables"] = True
        except sqlite3.Error:
            features["strict_tables"] = False
        row = connection.execute("SELECT json_valid('{}')").fetchone()
        features["json"] = bool(row and row[0])
        try:
            connection.execute("CREATE VIRTUAL TABLE temp.probe_fts USING fts5(x)")
            connection.execute("DROP TABLE temp.probe_fts")
            features["fts5"] = True
        except sqlite3.Error:
            features["fts5"] = False
        features["backup_api"] = hasattr(connection, "backup")
        return features
    finally:
        connection.close()


def parse_routing(project_root: Path) -> dict[str, object]:
    routing_path = project_root / ".research" / "project.toml"
    result: dict[str, object] = {"routing_file": None, "routing": None}
    if not routing_path.is_file():
        return result
    result["routing_file"] = str(routing_path)
    try:
        with routing_path.open("rb") as handle:
            data = tomllib.load(handle)
        result["routing"] = {
            "project_id": (data.get("project") or {}).get("id"),
            "state_root": (data.get("state") or {}).get("root", ".research/state"),
            "transport": (data.get("controller") or {}).get("transport", "cli"),
        }
    except (OSError, tomllib.TOMLDecodeError) as exc:
        result["routing_error"] = str(exc)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="doctor.py",
        description=(
            "Inspect local Python/SQLite features, optional routing TOML, and whether an rkb "
            "executable is discoverable. Does not open a project database, contact a controller, "
            "execute the discovered binary, grant permissions, or assert backend readiness."
        ),
    )
    parser.add_argument("--project-root", default=".", help="Project root to inspect.")
    args = parser.parse_args(argv)
    project_root = Path(args.project_root).expanduser().resolve()
    report = {
        "python_version": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 11),
        "sqlite": check_sqlite(),
        "rkb_discoverable": shutil.which("rkb"),
        "jsonschema_available": importlib.util.find_spec("jsonschema") is not None,
        "project_root": str(project_root),
    }
    report.update(parse_routing(project_root))
    report["note"] = (
        "This helper inspects without mutating. Passing preflight does not prove that a controller "
        "is reachable, that a project database is valid, or that any operation is authorized."
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
