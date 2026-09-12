#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent
TERMS = [
    ("zeta", "lattice spacing control parameter"),
    ("eta", "discretization error estimator"),
    ("kappa", "finite-volume correction factor"),
    ("lambda", "cutoff regularization scale"),
    ("mu", "renormalization reference scale"),
    ("nu", "correlation length exponent"),
    ("rho", "spectral density normalization"),
    ("sigma", "statistical uncertainty model"),
    ("tau", "autocorrelation time"),
    ("phi", "order parameter definition"),
]
OBSERVABLE_TERMS = ["harmonic-trap-observable", "box-spectrum-observable", "shell-momentum-observable"]
HYPOTHESIS_TERMS = ["linear-scaling-hypothesis", "logarithmic-drift-hypothesis", "threshold-behavior-hypothesis", "independent-replicate-hypothesis"]
ISSUE_TERMS = ["normalization-drift-issue", "threshold-ambiguity-issue", "metadata-gap-issue"]
TASK_TERMS = ["coarse-check-task", "extrapolation-fit-task", "source-locate-task", "replicate-run-task"]
DECISION_TERMS = ["fit-window-decision", "seed-policy-decision"]


def capture(kind, subkind, title, state, *, body="", aliases=None, record_state="active"):
    payload = {
        "kind": kind,
        "subkind": subkind,
        "title": title,
        "state_json": state,
        "record_state": record_state,
    }
    if body:
        payload["body_md"] = body
    if aliases:
        payload["aliases"] = aliases
    return {"op": "capture", "payload": payload}


def build_records():
    records = []
    for index, (symbol, meaning) in enumerate(TERMS):
        alias = f"def-{symbol}"
        records.append(
            capture(
                "knowledge",
                "definition",
                f"Definition {symbol} ({meaning})",
                {
                    "subkind": "definition",
                    "meaning": f"Synthetic {meaning} used by fixture study number {index}.",
                    "symbol": symbol,
                    "namespace": "synthetic",
                    "units_or_domain": "fixture units",
                    "source_or_convention": "synthetic project convention",
                },
                aliases=[alias],
            )
        )
    for index, name in enumerate(HYPOTHESIS_TERMS):
        records.append(
            capture(
                "knowledge",
                "hypothesis",
                f"Hypothesis {name}",
                {
                    "subkind": "hypothesis",
                    "proposal": f"Under condition set {index}, the synthetic observable follows the {name} pattern.",
                    "applicability": {"condition_set": index},
                    "support_criteria": [f"residual trend for {name} is flat"],
                    "contradiction_criteria": [f"monotone drift remains for {name}"],
                },
                aliases=[f"hyp-{index}"],
            )
        )
    for index, name in enumerate(OBSERVABLE_TERMS):
        records.append(
            capture(
                "knowledge",
                "observation",
                f"Observation {name}",
                {
                    "subkind": "observation",
                    "observation": f"The synthetic {name} was measured across three condition sets.",
                    "conditions": {"condition_set": index, "synthetic": True},
                },
                aliases=[f"obs-{index}"],
            )
        )
    records.append(
        capture(
            "claim",
            "claim",
            "Claim linear-scaling-agreement",
            {
                "subkind": "claim",
                "statement": "The extrapolated synthetic value agrees with the reference within 2 sigma.",
                "domain_applicability": {"condition_set": [0, 1, 2]},
                "quantifiers": "for all fixture condition sets",
                "evidence_criteria": [{"criterion": "analytical derivation under fixture assumptions"}],
            },
            aliases=["claim-linear"],
        )
    )
    records.append(
        capture(
            "claim",
            "claim",
            "Claim threshold-consistency",
            {
                "subkind": "claim",
                "statement": "The threshold behavior is consistent across independent replicates.",
                "domain_applicability": {"condition_set": 2},
                "quantifiers": "for fixture replicate identities",
                "evidence_criteria": [{"criterion": "independent reproduction across replicates"}],
            },
            aliases=["claim-threshold"],
        )
    )
    records.append(
        capture(
            "claim",
            "claim",
            "Claim normalization-equivalence",
            {
                "subkind": "claim",
                "statement": "Both synthetic normalization conventions agree after correction.",
                "domain_applicability": {"convention": ["a", "b"]},
                "quantifiers": "for fixture conventions",
                "evidence_criteria": [{"criterion": "pinned definition comparison"}],
            },
            aliases=["claim-normalization"],
        )
    )
    for index, name in enumerate(ISSUE_TERMS):
        records.append(
            capture(
                "knowledge",
                "issue",
                f"Issue {name}",
                {
                    "subkind": "issue",
                    "affected_scope": {"fixture": index},
                    "severity": "critical" if index == 0 else "high",
                    "effect": f"The synthetic {name} blocks paper-ready assessment.",
                    "blocking_operations": ["paper-ready assessment"],
                    "resolution_criterion": f"A pinned source or definition resolves {name}.",
                    "status": "open",
                },
                aliases=[f"issue-{index}"],
            )
        )
    for index, name in enumerate(TASK_TERMS):
        records.append(
            capture(
                "work",
                "task",
                f"Task {name}",
                {
                    "subkind": "task",
                    "objective": f"Complete the synthetic {name}.",
                    "work_state": "open",
                    "priority": "high" if index < 2 else "medium",
                    "priority_reason": f"{name} gates the fixture milestone.",
                    "completion_criteria": [f"evidence for {name} recorded"],
                    "owner": "unassigned",
                    "required_inputs": [],
                },
                aliases=[f"task-{index}"],
            )
        )
    for index, name in enumerate(DECISION_TERMS):
        records.append(
            capture(
                "knowledge",
                "decision",
                f"Decision {name}",
                {
                    "subkind": "decision",
                    "choice": f"Adopt the synthetic {name} convention.",
                    "alternatives": [{"name": "alternative-a"}, {"name": "alternative-b"}],
                    "reasons": [f"stability under {name}"],
                    "decision_maker": "synthetic researcher",
                    "applicability": {"fixture": index},
                    "reconsideration_conditions": ["new source contradicts the convention"],
                },
                aliases=[f"decision-{index}"],
            )
        )
    records.append(
        capture(
            "source",
            "note",
            "Source synthetic methodology note",
            {
                "subkind": "note",
                "source_type": "note",
                "title": "Synthetic methodology note",
                "author": "synthetic author",
                "external_id": "synthetic-note-1",
                "version": "v1",
                "identity_assurance": "metadata_only",
                "preservation": "metadata_only",
                "locator": "fixture://synthetic-note-1",
            },
            body="The synthetic methodology note describes the fixture protocol.",
            aliases=["source-note"],
        )
    )
    records.append(
        capture(
            "artifact",
            "analysis_result",
            "Artifact extrapolation-fit-result",
            {
                "subkind": "analysis_result",
                "role": "fixture fit output",
                "media_type": "application/json",
                "content_identity": {"kind": "manifest", "value": "synthetic-fit-manifest"},
                "assurance": "manifest",
                "availability": "available",
                "locations": [{"kind": "fixture", "location": "fixture://fit-result", "availability": "available"}],
                "results": {
                    "results": [
                        {
                            "name": "exponent",
                            "value": 1.0,
                            "units": "dimensionless",
                            "dimensionless": True,
                            "condition": "fixture condition set 0",
                            "uncertainty": {"type": "std_error", "level": 0.01, "method": "bootstrap"},
                        }
                    ]
                },
            },
            aliases=["artifact-fit"],
        )
    )
    records.append(
        capture(
            "study",
            "numerical",
            "Study fixture-extrapolation",
            {
                "subkind": "numerical",
                "question": "Does the synthetic observable extrapolate consistently?",
                "protocol": {"method_profile": "numerical", "preprocessing": "none"},
                "required_outputs": [{"name": "exponent"}],
                "completion_criteria": ["all fixture slots covered"],
                "study_state": "active",
                "executable": True,
                "trial_design": {"factors": {"condition_set": [0, 1, 2]}},
            },
            aliases=["study-fixture"],
        )
    )
    records.append(
        capture(
            "resource",
            "cpu",
            "Resource fixture-host",
            {
                "subkind": "cpu",
                "machine_id": "fixture-host",
                "capabilities": {"cpu": 4},
                "capacity": 2,
                "admin_state": "enabled",
                "limitations": ["fixture only"],
            },
            aliases=["resource-fixture"],
        )
    )
    return records


def build_links():
    links = [
        ("supports", "obs-0", "claim-linear"),
        ("supports", "obs-1", "claim-linear"),
        ("contradicts", "obs-2", "claim-linear"),
        ("supports", "artifact-fit", "claim-linear"),
        ("supports", "obs-2", "claim-threshold"),
        ("assumes", "claim-linear", "def-zeta"),
        ("assumes", "claim-threshold", "def-eta"),
        ("blocks", "issue-0", "claim-linear"),
        ("blocks", "issue-1", "claim-threshold"),
        ("blocks", "issue-2", "claim-normalization"),
        ("resolves", "task-2", "issue-2"),
        ("depends_on", "task-1", "artifact-fit"),
        ("depends_on", "task-3", "task-1"),
        ("uses", "artifact-fit", "def-kappa"),
        ("derived_from", "claim-linear", "obs-0"),
    ]
    operations = []
    for index, (predicate, src, dst) in enumerate(links):
        qualifiers = {"condition": "done_with_review"} if predicate == "depends_on" else {}
        pin_mode = "tracking" if predicate == "depends_on" else "pinned"
        operations.append(
            {
                "op": "link",
                "payload": {
                    "predicate": predicate,
                    "src_ref": {"alias": src},
                    "dst_ref": {"alias": dst},
                    "pin_mode": pin_mode,
                    "qualifiers": qualifiers,
                    "rationale": f"Synthetic fixture link {index}.",
                },
            }
        )
    return operations


def build_questions():
    questions = []
    counter = 0
    for symbol, meaning in TERMS:
        counter += 1
        questions.append(
            {
                "id": f"q{counter:03d}",
                "query": f"definition {symbol}",
                "expected_aliases": [f"def-{symbol}"],
            }
        )
    for index, name in enumerate(HYPOTHESIS_TERMS):
        counter += 1
        questions.append({"id": f"q{counter:03d}", "query": name, "expected_aliases": [f"hyp-{index}"]})
    for index, name in enumerate(OBSERVABLE_TERMS):
        counter += 1
        questions.append({"id": f"q{counter:03d}", "query": name, "expected_aliases": [f"obs-{index}"]})
    extra = [
        ("linear-scaling-agreement", ["claim-linear"]),
        ("threshold-consistency", ["claim-threshold"]),
        ("normalization-equivalence", ["claim-normalization"]),
        ("normalization-drift-issue", ["issue-0"]),
        ("threshold-ambiguity-issue", ["issue-1"]),
        ("metadata-gap-issue", ["issue-2"]),
        ("coarse-check-task", ["task-0"]),
        ("extrapolation-fit-task", ["task-1"]),
        ("source-locate-task", ["task-2"]),
        ("replicate-run-task", ["task-3"]),
        ("fit-window-decision", ["decision-0"]),
        ("seed-policy-decision", ["decision-1"]),
        ("synthetic methodology note", ["source-note"]),
        ("extrapolation-fit-result", ["artifact-fit"]),
        ("fixture-extrapolation study", ["study-fixture"]),
        ("fixture-host resource", ["resource-fixture"]),
        ("estimator bootstrap uncertainty", ["artifact-fit"]),
        ("convention reconsideration", ["decision-0"]),
        ("cutoff regularization lambda", ["def-lambda"]),
        ("renormalization mu", ["def-mu"]),
        ("spectral density rho", ["def-rho"]),
        ("correlation length nu", ["def-nu"]),
        ("order parameter phi", ["def-phi"]),
        ("statistical uncertainty sigma", ["def-sigma"]),
        ("autocorrelation time tau", ["def-tau"]),
        ("metadata gap resolution", ["issue-2"]),
        ("seed policy", ["decision-1"]),
        ("fit window stability", ["decision-0"]),
    ]
    for query, aliases in extra:
        counter += 1
        questions.append({"id": f"q{counter:03d}", "query": query, "expected_aliases": aliases})
    return questions


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Regenerate synthetic fixtures.")
    parser.parse_args()
    project = {
        "name": "synthetic-project-v1",
        "description": (
            "Synthetic project used to exercise the runtime and the FTS retrieval gold set. "
            "Never import these records into a user project unless the user explicitly requests it."
        ),
        "record_state_default": "active",
        "batches": [
            {"phase": "records", "operations": build_records()},
            {"phase": "links", "operations": build_links()},
        ],
    }
    gold = {
        "name": "retrieval-gold-v1",
        "description": "FTS-only gold set over synthetic-project-v1; expected_aliases must appear in top-10 search results.",
        "corpus": "synthetic-project-v1",
        "questions": build_questions(),
    }
    (FIXTURES / "synthetic_project.json").write_text(
        json.dumps(project, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    (FIXTURES / "retrieval_gold.json").write_text(
        json.dumps(gold, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(f"records={len(project['batches'][0]['operations'])} links={len(project['batches'][1]['operations'])} questions={len(gold['questions'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
