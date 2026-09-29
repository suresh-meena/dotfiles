"""SQLite schema v2.

The §1.2 invariants that can be expressed as constraints are expressed as
constraints, so a code path that forgets to check them fails its transaction
instead of silently recording a broken state:

* invariant 1 — ``gpu_unreleased_unique``: no two unreleased reservations of
  one ``(node, GPU UUID)``;
* invariant 2 — ``attempts_one_possibly_live``: at most one attempt per job
  whose remote execution is live *or possibly live*;
* ``attempts_one_active``: at most one unfinished attempt per job, so a second
  attempt can't even be planned while the first is being staged;
* invariant 11 — enforced in ``engine.state``, which is the only writer of
  job phase and bumps ``version`` and writes an event in one transaction.
"""

from __future__ import annotations

SCHEMA_VERSION = 4

JOB_PHASES = (
    "HELD", "PENDING", "DISPATCHING", "SUBMISSION_UNKNOWN", "SUBMITTED",
    "RUNNING", "CANCELLING", "RECONCILING", "FINALIZING", "BLOCKED", "TERMINAL",
)
DESIRED_STATES = ("RUN", "HOLD", "CANCEL")
EXECUTION_OUTCOMES = (
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
    "NODE_FAIL", "PREEMPTED", "UNKNOWN_EXIT",
)
ARTIFACT_STATES = (
    "NOT_REQUESTED", "PENDING", "COLLECTING", "COMPLETE", "RETRY_WAIT",
    "FAILED", "EXPIRED",
)
# Attempt lifecycle.  Terminal attempt states are the three at the end: an
# attempt that reached them can never execute again.
ATTEMPT_STATES = (
    "PLANNED", "STAGING", "LAUNCHING", "START_UNKNOWN", "SUBMITTING",
    "SUBMISSION_UNKNOWN", "SUBMITTED", "RUNNING", "STOPPING", "STOPPED",
    "REFUSED", "NEVER_STARTED", "RELEASED",
)
ATTEMPT_FINAL_STATES = ("REFUSED", "NEVER_STARTED", "RELEASED")
BACKENDS = ("bare", "slurm", "fake")
RESERVATION_KINDS = ("gpu", "ram", "cpu", "scratch", "slot", "cluster_gpus", "cluster_jobs")
OPERATION_STATES = ("INTENDED", "SENT", "DONE", "FAILED", "UNCERTAIN")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


DDL = f"""
CREATE TABLE controller_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE principals (
    name TEXT PRIMARY KEY CHECK (length(name) BETWEEN 1 AND 64),
    quota_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL
);

CREATE TABLE tokens (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL REFERENCES principals(name),
    kind TEXT NOT NULL CHECK (kind IN ('human','agent','service')),
    label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 128),
    secret_sha256 TEXT NOT NULL,
    scopes TEXT NOT NULL,
    quota_json TEXT NOT NULL DEFAULT '{{}}',
    allow_clusters INTEGER NOT NULL DEFAULT 0 CHECK (allow_clusters IN (0,1)),
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    last_used_at TEXT
);

CREATE TABLE bundles (
    digest TEXT PRIMARY KEY CHECK (digest LIKE 'sha256:%'),
    compressed_bytes INTEGER NOT NULL CHECK (compressed_bytes >= 0),
    expanded_bytes INTEGER NOT NULL CHECK (expanded_bytes >= 0),
    members INTEGER NOT NULL CHECK (members >= 0),
    format_version INTEGER NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Physical dedup (one bundle row) sits behind per-owner logical references
-- (§6.3): knowing a digest grants no access; only a ref row does.
CREATE TABLE bundle_refs (
    digest TEXT NOT NULL REFERENCES bundles(digest),
    owner TEXT NOT NULL REFERENCES principals(name),
    ref_kind TEXT NOT NULL CHECK (ref_kind IN ('upload','job')),
    ref_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY (digest, owner, ref_kind, ref_id)
);

CREATE TABLE groups (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('each','array')),
    owner TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL REFERENCES principals(name),
    token_id TEXT NOT NULL REFERENCES tokens(id),
    name TEXT NOT NULL,
    group_id TEXT REFERENCES groups(id),
    array_index INTEGER,
    spec_json TEXT NOT NULL,
    spec_digest TEXT NOT NULL,
    desired_state TEXT NOT NULL CHECK (desired_state IN {_in(DESIRED_STATES)}),
    phase TEXT NOT NULL CHECK (phase IN {_in(JOB_PHASES)}),
    reason TEXT,
    execution_outcome TEXT CHECK (execution_outcome IS NULL OR execution_outcome IN {_in(EXECUTION_OUTCOMES)}),
    exit_code INTEGER,
    exit_signal INTEGER,
    artifacts_state TEXT NOT NULL DEFAULT 'NOT_REQUESTED' CHECK (artifacts_state IN {_in(ARTIFACT_STATES)}),
    success INTEGER CHECK (success IS NULL OR success IN (0,1)),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
    priority INTEGER NOT NULL DEFAULT 0,
    bundle_digest TEXT REFERENCES bundles(digest),
    executions_used INTEGER NOT NULL DEFAULT 0 CHECK (executions_used >= 0),
    -- Backoff: placement skips the job until this time. Gate refusals and
    -- failed staging don't consume an execution retry, so without this they
    -- could loop as a dispatch storm (§2.3).
    not_before TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    submitted_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    updated_at TEXT NOT NULL,
    -- A terminal job always says how it ended; success is only known then.
    CHECK (phase <> 'TERMINAL' OR execution_outcome IS NOT NULL),
    CHECK (success IS NULL OR phase = 'TERMINAL')
);
CREATE INDEX jobs_phase ON jobs(phase);
CREATE INDEX jobs_owner ON jobs(owner, id);

-- Idempotency (§6.1): one key per token maps to one request, forever while
-- its jobs exist.  Retention is tied to job history, never a short TTL.
CREATE TABLE idempotency (
    token_id TEXT NOT NULL REFERENCES tokens(id),
    key TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (token_id, key)
);

CREATE TABLE deps (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    parent_id INTEGER NOT NULL REFERENCES jobs(id),
    type TEXT NOT NULL CHECK (type IN ('afterok','afterany','afternotok','after')),
    PRIMARY KEY (job_id, parent_id, type),
    CHECK (job_id <> parent_id)
);

CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    n INTEGER NOT NULL CHECK (n >= 1),
    backend TEXT NOT NULL CHECK (backend IN {_in(BACKENDS)}),
    target TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN {_in(ATTEMPT_STATES)}),
    remote_may_be_live INTEGER NOT NULL CHECK (remote_may_be_live IN (0,1)),
    remote_id TEXT,
    launch_op_id TEXT NOT NULL UNIQUE,
    spec_digest TEXT NOT NULL,
    profile_digest TEXT,
    boot_id TEXT,
    outcome TEXT CHECK (outcome IS NULL OR outcome IN {_in(EXECUTION_OUTCOMES)}),
    exit_code INTEGER,
    exit_signal INTEGER,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, n),
    -- An attempt that can never run again can't be possibly live.
    CHECK (state NOT IN {_in(ATTEMPT_FINAL_STATES)} OR remote_may_be_live = 0)
);
CREATE UNIQUE INDEX attempts_one_possibly_live ON attempts(job_id) WHERE remote_may_be_live = 1;
CREATE UNIQUE INDEX attempts_one_active ON attempts(job_id) WHERE state NOT IN {_in(ATTEMPT_FINAL_STATES)};
CREATE INDEX attempts_target ON attempts(target, state);

CREATE TABLE resource_reservations (
    id INTEGER PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN {_in(RESERVATION_KINDS)}),
    gpu_uuid TEXT,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    created_at TEXT NOT NULL,
    released_at TEXT,
    CHECK ((kind = 'gpu') = (gpu_uuid IS NOT NULL))
);
CREATE UNIQUE INDEX gpu_unreleased_unique ON resource_reservations(node_id, gpu_uuid)
    WHERE kind = 'gpu' AND released_at IS NULL;
CREATE INDEX reservations_attempt ON resource_reservations(attempt_id);
CREATE INDEX reservations_node_open ON resource_reservations(node_id) WHERE released_at IS NULL;

-- Durable intents (§1.2 invariant 4): a remote mutation is INTENDED and
-- committed before it is sent, so a crash leaves a record of what may have
-- happened, not a silent gap.
CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    attempt_id TEXT REFERENCES attempts(id),
    target TEXT,
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN {_in(OPERATION_STATES)}),
    epoch INTEGER NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX operations_open ON operations(state) WHERE state IN ('INTENDED','SENT','UNCERTAIN');

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    job_id INTEGER,
    attempt_id TEXT,
    target TEXT,
    kind TEXT NOT NULL,
    job_version INTEGER,
    actor TEXT,
    detail_json TEXT NOT NULL DEFAULT '{{}}'
);
CREATE INDEX events_job ON events(job_id, id);

CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    backend TEXT NOT NULL CHECK (backend IN {_in(BACKENDS)}),
    mode TEXT CHECK (mode IS NULL OR mode IN ('exclusive','shared')),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
    config_json TEXT NOT NULL DEFAULT '{{}}',
    state TEXT NOT NULL DEFAULT 'UNKNOWN',
    drain_kind TEXT,
    drain_reason TEXT,
    drain_by TEXT,
    drain_at TEXT,
    fence_epoch INTEGER,
    reconciled_epoch INTEGER,
    last_probe_json TEXT,
    last_probe_at TEXT,
    boot_id TEXT,
    updated_at TEXT NOT NULL
);

-- fleetqd owns one complete latest all-user queue snapshot per opted-in site.
-- Failed polls preserve the last complete jobs_json and mark it incomplete.
CREATE TABLE managed_slurm_snapshots (
    site_id TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    attempted_at TEXT NOT NULL,
    last_success_at TEXT,
    complete INTEGER NOT NULL CHECK (complete IN (0,1)),
    error TEXT,
    jobs_json TEXT NOT NULL DEFAULT '[]' CHECK (length(jobs_json) <= 524288),
    row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    output_bytes INTEGER NOT NULL DEFAULT 0 CHECK (output_bytes >= 0)
);

CREATE TABLE node_gpus (
    node_id TEXT NOT NULL REFERENCES nodes(id),
    uuid TEXT NOT NULL,
    model TEXT,
    vram_total INTEGER,
    pci_bus TEXT,
    idx INTEGER,
    reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved IN (0,1)),
    drained INTEGER NOT NULL DEFAULT 0 CHECK (drained IN (0,1)),
    drain_reason TEXT,
    cooldown_until TEXT,
    baseline_mem INTEGER,
    PRIMARY KEY (node_id, uuid)
);

CREATE TABLE observations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    sample_time TEXT NOT NULL,
    boot_id TEXT,
    complete INTEGER NOT NULL CHECK (complete IN (0,1)),
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (source, target, sample_id)
);

CREATE TABLE placement_decisions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    attempt_id TEXT,
    decision TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{{}}'
);
CREATE INDEX placement_job ON placement_decisions(job_id, id);

CREATE TABLE site_profiles (
    id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
    approved_by TEXT,
    approved_at TEXT
);

CREATE TABLE outbox (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','SENT','DROPPED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE budget_buckets (
    cluster TEXT NOT NULL,
    op_class TEXT NOT NULL CHECK (op_class IN ('monitor','action','transfer')),
    tokens REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cluster, op_class)
);

-- Separate aggregate control-session and transfer-byte token buckets. RPC
-- counts continue to use the per-operation-class budget_buckets above.
CREATE TABLE budget_dimension_buckets (
    cluster TEXT NOT NULL,
    dimension TEXT NOT NULL CHECK (dimension IN ('sessions','bytes')),
    tokens REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cluster, dimension)
);

-- A Slurm cache pin is released remotely only after the attempt's durable
-- terminal/artifact state allows it. Record the remote acknowledgement so
-- retries after a crash are bounded and idempotent.
CREATE TABLE remote_cache_pin_release_acks (
    attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),
    acknowledged_at TEXT NOT NULL,
    controller_epoch INTEGER NOT NULL
);

CREATE TABLE permits (
    id TEXT PRIMARY KEY,
    cluster TEXT NOT NULL,
    op_class TEXT NOT NULL,
    cost_json TEXT NOT NULL,
    caller TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    redeemed_at TEXT
);

CREATE TABLE remote_calls (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    target TEXT NOT NULL,
    op_class TEXT NOT NULL,
    rpc INTEGER NOT NULL DEFAULT 0,
    sessions INTEGER NOT NULL DEFAULT 1,
    bytes INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL
);
CREATE INDEX remote_calls_target ON remote_calls(target, ts);

CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    relpath TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0,1)),
    state TEXT NOT NULL CHECK (state IN ('PENDING','COLLECTING','COMPLETE','FAILED','EXPIRED')),
    size INTEGER,
    sha256 TEXT,
    local_path TEXT,
    tries INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE (attempt_id, relpath)
);

-- Minimal identity kept after bulky history expires (§1.3), so a restored or
-- replaying request can never quietly re-execute old work.
CREATE TABLE recovery_tombstones (
    attempt_id TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL,
    spec_digest TEXT NOT NULL,
    request_key_digest TEXT,
    final_state TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""
