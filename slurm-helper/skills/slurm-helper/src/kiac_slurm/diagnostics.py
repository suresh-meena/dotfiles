"""Diagnostic levels, provenance labels, and the report container.

Every finding carries a rule ID (SH* shell, SLURM* generic semantics,
KIAC* site policy, LIVE* scheduler-verified, FS* filesystem) and, where a
claim depends on a data source, a confidence label:

  verified-live    read from the running scheduler this session
  documented       stated by the KIAC manual, uncontested
  document-conflict the manual contradicts itself; unresolved until --live
  inferred         a default or heuristic, not a site fact
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

PASS = "PASS"
INFO = "INFO"
WARN = "WARN"
ERROR = "ERROR"

CONF_VERIFIED_LIVE = "verified-live"
CONF_DOCUMENTED = "documented"
CONF_CONFLICT = "document-conflict"
CONF_INFERRED = "inferred"


@dataclass
class Diagnostic:
    level: str
    rule_id: str
    message: str
    line: Optional[int] = None
    excerpt: Optional[str] = None
    suggestion: Optional[str] = None
    confidence: Optional[str] = None
    escalated: bool = False


class Report:
    """Ordered container of diagnostics; exit code derives from levels."""

    def __init__(self) -> None:
        self.items: List[Diagnostic] = []

    def add(
        self,
        level: str,
        rule_id: str,
        message: str,
        line: Optional[int] = None,
        excerpt: Optional[str] = None,
        suggestion: Optional[str] = None,
        confidence: Optional[str] = None,
    ) -> Diagnostic:
        diag = Diagnostic(
            level=level,
            rule_id=rule_id,
            message=message,
            line=line,
            excerpt=excerpt,
            suggestion=suggestion,
            confidence=confidence,
        )
        self.items.append(diag)
        return diag

    def pass_(self, rule_id: str, message: str, **kw) -> Diagnostic:
        return self.add(PASS, rule_id, message, **kw)

    def info(self, rule_id: str, message: str, **kw) -> Diagnostic:
        return self.add(INFO, rule_id, message, **kw)

    def warn(self, rule_id: str, message: str, **kw) -> Diagnostic:
        return self.add(WARN, rule_id, message, **kw)

    def error(self, rule_id: str, message: str, **kw) -> Diagnostic:
        return self.add(ERROR, rule_id, message, **kw)

    def _with_level(self, level: str) -> List[Diagnostic]:
        return [d for d in self.items if d.level == level]

    @property
    def errors(self) -> List[Diagnostic]:
        return self._with_level(ERROR)

    @property
    def warnings(self) -> List[Diagnostic]:
        return self._with_level(WARN)

    @property
    def passes(self) -> List[Diagnostic]:
        return self._with_level(PASS)

    def escalate_warnings(self) -> int:
        """Strict mode: promote every warning to an error."""
        count = 0
        for diag in self.items:
            if diag.level == WARN:
                diag.level = ERROR
                diag.escalated = True
                count += 1
        return count

    def counts(self) -> dict:
        return {
            "pass": len(self.passes),
            "info": len(self._with_level(INFO)),
            "warn": len(self.warnings),
            "error": len(self.errors),
        }

    def exit_code(self) -> int:
        return 1 if self.errors else 0

    def rule_ids(self) -> List[str]:
        return [d.rule_id for d in self.items]
