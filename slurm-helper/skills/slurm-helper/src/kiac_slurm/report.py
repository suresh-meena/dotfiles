"""Deterministic PASS/WARN/ERROR rendering (stage 10), text and JSON."""

from __future__ import annotations

import json
from typing import List

from .diagnostics import Report


def _render_diag(diag) -> List[str]:
    simple = (
        diag.excerpt is None
        and diag.suggestion is None
        and diag.confidence is None
        and "\n" not in diag.message
        and not diag.escalated
    )
    head = f"{diag.level:<5} {diag.rule_id:<7}"
    if simple:
        return [f"{head} {diag.message}".rstrip()]
    if diag.line is not None:
        head += f" line {diag.line}"
    if diag.escalated:
        head += "  (strict)"
    lines = [head.rstrip()]
    if diag.excerpt:
        lines.append(f"      {diag.excerpt}")
    for message_line in diag.message.splitlines():
        lines.append(f"      {message_line}")
    if diag.suggestion:
        lines.append(f"      Suggested: {diag.suggestion}")
    if diag.confidence:
        lines.append(f"      Confidence: {diag.confidence}")
    return lines


def render_text(rep: Report, path: str, live: bool = False, strict: bool = False) -> str:
    out: List[str] = []
    for diag in rep.items:
        out.extend(_render_diag(diag))
        out.append("")
    counts = rep.counts()
    mode = "live check" if live else "offline check"
    if strict:
        mode += " (strict)"
    out.append(
        f"{counts['pass']} PASS, {counts['warn']} WARN, {counts['error']} ERROR"
        f"  |  {path}  |  {mode}"
    )
    return "\n".join(out)


def render_json(rep: Report, path: str, live: bool = False, strict: bool = False) -> str:
    payload = {
        "file": path,
        "live": bool(live),
        "strict": bool(strict),
        "summary": rep.counts(),
        "exit_code": rep.exit_code(),
        "diagnostics": [
            {
                "level": d.level,
                "rule_id": d.rule_id,
                "line": d.line,
                "excerpt": d.excerpt,
                "message": d.message,
                "suggestion": d.suggestion,
                "confidence": d.confidence,
                "escalated": d.escalated,
            }
            for d in rep.items
        ],
    }
    return json.dumps(payload, indent=2)
