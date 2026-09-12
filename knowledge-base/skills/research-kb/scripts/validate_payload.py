#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_validation import PayloadError, list_schemas, validate_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_payload.py",
        description="Validate a research payload against a bundled offline schema.",
    )
    parser.add_argument("--schema", required=True, help=f"One of: {', '.join(list_schemas())}")
    parser.add_argument("--input", required=True, help="Path to the UTF-8 JSON payload.")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON (always emitted).")
    args = parser.parse_args(argv)
    try:
        result = validate_file(args.schema, args.input)
    except PayloadError as exc:
        result = {
            "valid": False,
            "schema": args.schema,
            "input": args.input,
            "errors": [str(exc)],
            "note": "The payload could not be read or parsed.",
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
