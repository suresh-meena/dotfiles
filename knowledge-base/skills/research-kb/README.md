# research-kb skill package

This folder is the agent-facing entry point for the Research Knowledge Base. `SKILL.md` is what a host
agent loads; the `references/` files are loaded only when the immediate operation needs them.

## What this package is

- A short, testable procedure for retrieving, capturing, correcting, and reviewing durable research
  knowledge: sources, definitions, assumptions, derivations, claims, evidence, decisions, negative
  results, tasks, and progress.
- Bundled offline schemas and examples under `assets/`, used only for local preflight and validation.
- Deterministic helper scripts under `scripts/` that inspect local prerequisites and validate payloads.

## What this package is not

Installing this skill does **not**:

- install or start the Research KB runtime, `rkb` CLI, or MCP server;
- create a project database or provision a controller;
- connect an MCP transport or grant any permission;
- make research records persistent.

Live persistence requires the separately implemented runtime, reached through trusted CLI or MCP tools.
When no compatible runtime is available, the skill may analyze explicitly provided sources or a dated
export and prepare an **uncommitted** proposal; it must state that nothing was saved.

## Installation

Install one authored copy, linked deliberately into a host discovery path:

- Codex: `.agents/skills/research-kb` in the project, or the host user skill location.
- Claude Code: `.claude/skills/research-kb` in the project.

Do not maintain divergent copies by hand. The package root is the directory containing this README and
`SKILL.md`; helpers should be invoked by their actual absolute path.

## Local helpers

- `scripts/doctor.py --project-root PATH` — reports Python/SQLite capabilities, optional routing
  discovery, and whether an `rkb` executable is on `PATH`. It does not open a project database, contact
  a controller, execute the discovered binary, or assert backend readiness.
- `scripts/validate_payload.py --schema capture --input FILE` — validates a draft against a bundled
  offline schema. Schema names: `capture`, `proposal`, `context`, `handoff`.
- `scripts/lint_skill.py` — checks frontmatter, required files, relative links, schema JSON, and size
  budgets.
- `tests/test_bundle.py` — runnable tests of this starter package. They validate the package and its
  scripts only; they do not test a live runtime, concurrency, restore, or agent behavior.

Dependencies are declared in `requirements-validation.txt`. Missing dependencies produce a clear error;
nothing is installed automatically.

## State location

Project state belongs to the configured project root, never a hidden central directory by default. The
runtime's default state root is `.research/state/<project-id>/` inside the project repository; routing
is committed at `.research/project.toml`. A copied repository must not silently create a second writable
canonical store.

## Design basis

The implementation specification is `research_kb_plan_v3.md` in the application repository. See
`references/01-design-and-audit.md` for the architecture and audit summary and
`references/10-sources.md` for the external references used by the design.
