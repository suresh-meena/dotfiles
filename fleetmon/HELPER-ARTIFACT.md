# Helper artifact and hash contract (P0 — locked remote deployment)

This file records the artifact/hash contract for the fleetmon helper wheel. It
does **not** record a wheel hash: `dist/` is gitignored and wheels must never
be committed. The hash of a given wheel is recorded at build time in the
uncommitted sidecar and communicated out of band for `--check`.

## Build

```text
.venv310/bin/python scripts/build-helper-wheel [--out DIR]
```

- Output: `<out>/fleetmon-<version>-py3-none-any.whl` (default `<source>/dist`)
  plus the uncommitted sidecar `<wheel>.sha256` containing
  `<sha256>  <wheel name>\n`.
- Version is read from `pyproject.toml` and cross-checked against
  `src/fleetmon/__init__.py` `__version__`; any disagreement fails the build
  (exit 2). Nothing is hardcoded in the script.
- Dependencies are pinned exactly from `requirements-helper.lock` (helper
  lock: psutil and nvidia-ml-py only; hub-only dependencies are never
  installed on compute machines).
- The wheel contains only the helper-relevant subset:
  `fleetmon/__init__.py`, `fleetmon/protocol.py`, `fleetmon/snapshot.py`,
  plus the console script `fleetmon-snapshot = fleetmon.snapshot:main`.
  Hub-only modules (cli/config/database/web/...) are never included.
- Determinism: fixed zip timestamps (SOURCE_DATE_EPOCH, default
  2000-01-01T00:00:00Z), fixed member order, fixed zip metadata, no
  build-path leakage. Building twice with the same interpreter produces
  byte-identical wheels (test: `test_build_wheel_is_reproducible_byte_identical`).
  Cross-interpreter zlib differences may change bytes; the recorded hash is
  always per built artifact.

## Check

```text
.venv310/bin/python scripts/build-helper-wheel --check <wheel> [--expect-sha256 HEX]
```

Without `--expect-sha256`, the `<wheel>.sha256` sidecar is the recorded hash.
`--check` also refuses a wheel whose filename version does not match
`pyproject.toml`. Exit codes: 0 ok, 1 hash mismatch, 2 local
build/configuration error.

## Transfer contract (scripts/install-helper)

- The installer validates the local wheel SHA-256 against the recorded hash
  **before any fleetctl invocation**, refuses on mismatch (exit 4,
  `artifact_hash_mismatch`), refuses any real install without a recorded hash
  (`artifact_hash_record_missing`), and re-validates the staged wheel hash on
  the remote (exit 4, `remote_stage_hash_mismatch`) before installing it.
- Transfer goes only through `fleetctl sync push --target NAME <local>
  <remote_path>`; remote canary steps go only through
  `fleetctl exec --target NAME -- <argv vector>` (argv arrays, no shell
  interpretation). Never raw ssh/scp. The exact, --help-verified fleetctl
  contract is recorded in the comment block at the top of
  `scripts/install-helper`.
- A real install still requires the literal, target-matching flag
  `--i-authorize-target-<NAME>`; the fail-closed default is unchanged. Until
  the authorized canary acceptance gate in `Todo.md` passes on one explicitly
  authorized canary, do not use the flag.

## Wheel hash record (filled at build time, never committed)

```text
wheel: dist/fleetmon-<version>-py3-none-any.whl
sha256: <recompute with scripts/build-helper-wheel --check>
```
