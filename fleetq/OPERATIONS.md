# fleetq operator runbook

These procedures preserve uncertainty instead of converting it into a retry. Keep the affected job, target, or service paused until the evidence required below is available. Browser pages are local control-plane views; they do not query a remote scheduler or node on page load.

## Unknown submission

1. Keep dispatch blocked for the affected job and retain its reservation, bundle, and logs.
2. Record the job ID, attempt ID, target/profile, submit timestamp, idempotency key, and controller log correlation. Do not copy bearer credentials into notes or logs.
3. Inspect the durable submission attempt and receipt/claim on the exact target. Use the configured profile's bounded, exact-identity query within the remote-call budget; do not search by job name alone.
4. If the scheduler proves one submission exists, attach its scheduler identity and reconcile normally. If evidence proves no submission was accepted, resolve the attempt using the supported operator procedure before any new attempt.
5. If evidence is incomplete or contradictory, leave the job unknown and escalate to the site/operator owner. Never resubmit “to see what happens,” switch targets, or release the reservation while a submission may be live.

## Stuck cancellation

1. Preserve the cancellation intent, reservation, output references, and the exact execution identity. A cancellation request does not mean the workload has stopped.
2. Query the recorded scheduler job ID or local execution unit/cgroup using bounded observation. Check descendants and finalizer state where the backend supports them.
3. Retry cancellation only against that same identity and within the profile's operation budget. Do not move the job to another target or submit a replacement.
4. If teardown cannot be confirmed, keep the job in cancelling/unknown state, block conflicting allocation as policy requires, and escalate to the workstation or site administrator. Record the evidence and the remaining uncertainty.
5. Release resources only after backend evidence confirms the workload and descendants are stopped and the controller has durably recorded the terminal result.

## State volume or database failure and restore

1. Stop new dispatch and admission if state integrity or durability is uncertain. Preserve remote workloads; stopping fleetqd alone does not stop detached jobs.
2. Do not initialize an empty database over the failed state. Stop the service cleanly if possible, preserve the failed database, WAL/SHM files, bundles, logs, and a copy of the latest backup. Record timestamps and checksums.
3. Restore the selected backup to a separate state path and validate it before activation. Follow the deployment's SQLite backup procedure; do not copy a live database file without its required WAL state.
4. Activate the restored database only through `fleetqd serve --restored-from-backup`. For the packaged user service, stop it, install a temporary `fleetq.service` drop-in that clears `ExecStart` and sets `ExecStart=%h/.local/share/fleetq/current/venv/bin/fleetqd serve --restored-from-backup`, reload systemd, then start the service. Keep its normal environment/configuration and other drop-in settings intact. The persistent restore gate makes subsequent ordinary service restarts remain gated if discovery has not completed.
5. Confirm database/config/auth health and that restore discovery completes. The controller discovers currently enabled targets plus disabled targets with a recorded accepted fence epoch or quarantine record; never-fenced disabled configuration entries are not contacted. If a target is unreachable or an orphan is found, keep the service gated/quarantined and resolve it before resuming.
6. Once restore discovery is complete, remove only the temporary `ExecStart` override, reload systemd, and restart the ordinary service. A restart is safe after the persistent restore gate is clear.
7. Reconcile remote state against durable attempts and receipts. Discover possible orphans and preserve idempotency tombstones. Keep ambiguous jobs blocked; do not replay uncertain submissions.
8. Verify bundles referenced by nonterminal jobs are present and readable. Review the recovery inventory and audit events, then resume only explicitly enabled targets.
9. Record which backup was used, its age, any lost control-plane history, and every manual resolution. If independent backup media or a recent backup is unavailable, report that recovery point limitation to the owner.

## Budget exhaustion

1. Identify whether the exhausted budget is a token admission quota, local remote-call budget, central permit, storage quota, or a configured target capacity. Read the status and audit records; do not infer a shared account limit from one user's quota.
2. For remote-call or permit exhaustion, allow deferred work to remain deferred. Do not increase limits, bypass the authority, or create a new token to multiply owner capacity without the authorized policy change.
3. For storage pressure, stop new data admission first and preserve control-plane reserve. Inspect pinned bundles, active attempts, logs, and artifact references.
4. Stop fleetqd, then run `fleetqd --config /path/to/fleetqd.toml gc --json` for a dry-run. Review every candidate and ownership/reference proof. Run `fleetqd --config /path/to/fleetqd.toml gc --apply --json` only after that review, then restart fleetqd. The command takes the controller lock and refuses concurrent operation. It removes old unreferenced local bundles and generated upload `.part` files at least seven days old. For collected artifacts, the first apply marks artifacts EXPIRED and records a job event while retaining the original execution outcome; preview and apply again to purge the now-unavailable files. Only artifacts for terminal jobs older than 180 days are considered, and jobs with active or uncertain attempts or active dependents are retained. For events older than 90 days, GC replaces detailed JSON with `{}` only when the event's job is terminal and has no active or uncertain attempts or active dependents; event rows, IDs, kinds, versions, and timeline order remain. Failed artifacts, Slurm remote caches, and job history are not removed by this local command. Never remove an active or uncertain pin to satisfy a watermark.
5. For admission quotas, finish or cancel eligible work through normal state transitions; never edit quota counters or database rows by hand. Escalate a limit change to the quota owner and record the approved scope and value.

## Bare-node remote bundle cache retention

Remote cache cleanup is a separate, explicit two-pass operation on an enrolled bare node. The controller's `fleetqd gc` does not contact nodes. An attempt's bundle remains pinned after allocation release while terminal artifacts are being collected. Once terminal artifact finalization is complete, the controller calls the exact-fenced `cache-release --fleet-id <fleet-id> --epoch <current-fenced-epoch> <attempt-id>` command; this records a durable `cache.released` marker without changing allocation release. Use the approved fleetctl admin execution path to invoke `fq-node --root <pinned-control-root> cache-gc --fleet-id <fleet-id> --epoch <current-fenced-epoch>` on exactly one node. This inspects canonical owned cache files older than the seven-day grace and reports blocked metadata. Review the returned digest and references. Purge one reviewed digest with the same command plus `--purge sha256:<64-lowercase-hex>`. Purge rechecks the fence, root, all attempt and inbox references, and age under the node lock. Live, uncertain, staged, unreleased, malformed, or unreadable references pin data or block the operation. The grace period starts at the later of cache-file modification and cache pin release. This command never runs automatically; Slurm shared-root cache retention is not covered by it.

## Slurm shared-root cache retention

Slurm cache cleanup is a separate, explicit two-pass operation through the budgeted `fleetqd` command on one enrolled cluster target and its pinned control root. Stop fleetqd first; the command takes the controller lock, so it refuses to run beside a live daemon. Inspect using `fleetqd --config /path/to/fleetqd.toml slurm-cache-gc TARGET`; review the eligible digest and blocked references, then purge one reviewed digest using `fleetqd --config /path/to/fleetqd.toml slurm-cache-gc TARGET --purge sha256:<64-lowercase-hex> --i-authorize-target-TARGET`. Both commands use the current fleet ID and epoch from the local database and the central remote-call permit authority. The remote wrapper takes the control lock and requires the exact current fence on both passes. It inventories only canonical cache archives; `.stage-*` transfer directories are intentionally not swept. Any malformed attempt metadata blocks purge. Attempts without a durable `cache.released` fact pin their bundle. The controller may write that fact only after terminal finalization records artifacts `COMPLETE` or `NOT_REQUESTED`. `FAILED` and `EXPIRED` artifact outcomes remain pinned pending an explicit discard workflow. Marker creation verifies the exact receipt and checks that the Slurm job is no longer in `squeue`; an ambiguous submit claim without a receipt stays pinned. Staging and submission wrappers reject an attempt after its marker is written. Released references retain a seven-day grace, measured from the later of cache mtime and latest release fact. Purge repeats metadata, pin, fence, age, and file checks under the lock, and removes only the reviewed regular, private, owned cache file. This operation never runs automatically and does not delete attempt history, manifests, outputs, or staged transfer directories.

## Corrupt bundle or conflicting artifact publication

1. Pause new dispatch for affected jobs. Preserve the failed path, expected digest, database reference, and error event before changing any file.
2. For a corrupt bundle object, stop fleetqd and back up the state directory. Verify that the filename is the recorded content digest and that the path is inside the private bundle root. Move only that corrupt object aside for evidence; do not delete its database row or active references. Restart fleetqd, then reupload the same digest with an authorized token. The server validates the bytes and refreshes compressed metadata; check authenticated HEAD before resuming dispatch.
3. For a conflicting published artifact tree, keep the original tree. Compare every expected path, size, and hash against the staged manifest. If the tree is correct, collection can reuse it. If it differs, resolve the conflict from preserved evidence with the job owner; do not overwrite the existing result during an automatic retry. If the remote output has expired, record finalization failure without rerunning compute.
4. If verified source bytes or an approved recovery decision are unavailable, leave the affected job blocked. Do not weaken content checks or release uncertain execution references to make the queue move.

## Upgrade and rollback

1. Review the release notes and compatibility matrix for the current and target versions, database migrations, API/client versions, node shim versions, and enabled backend profiles. Back up the database and required bundles; confirm the restore path is usable.
2. Pause new dispatch and admission as needed for the migration. Keep observation and cancellation available where supported. Record active and uncertain attempts and do not treat a software restart as cancellation.
3. Upgrade the controller in the documented order. Run its local health/doctor checks and allow startup discovery to finish before resuming dispatch.
4. Upgrade node shims one approved node at a time. Preserve the runner version required by every active attempt until those jobs finish. Do not use a controller upgrade as implicit fleet-wide node deployment.
5. If migration or readiness fails, keep dispatch paused and use the documented compatible rollback or restore procedure. Never downgrade by pointing an older binary at a migrated database unless compatibility is explicitly documented.
6. Verify target reconciliation, pending/unknown attempt counts, bundle availability, clock health, and remote-call budget. Resume only approved targets and document the release, migration, checks, and any unresolved incidents.

## Clock provider health and recovery

Production configuration must explicitly set `[daemon] clock_provider` to
`chrony` or `systemd-timesyncd`, matching the service actually running on the
host. Startup rejects a missing provider, and a missing, stale, or unhealthy
cached probe sample fails closed. The timesyncd probe requires its system
service to be active and `timedatectl` to report `NTPSynchronized=yes`.
When the probe rejects clock health, fleetq stops new dispatch and rejects
expiring-token authentication. Cancellation and recovery operations remain
available so operators can resolve existing work without waiting for normal
dispatch health.

1. Keep dispatch paused while investigating. Check the configured provider and
   its host service/status and determine whether its report is current and
   healthy. Do not change the clock provider setting to bypass a failed probe.
2. Restore the configured provider's service/report using the host's approved
   procedure. Confirm the report is fresh and healthy, then inspect fleetq
   readiness and authentication health.
3. Resume dispatch only after the health probe accepts the provider and normal
   reconciliation is complete. Record the fault and recovery evidence.

The read-only numpi check on 2026-09-28 found `systemd-timesyncd` active and
`NTPSynchronized=yes`; `chronyc` was absent. Recheck on the deployment host
and record the actual provider in its fleetqd config before enabling dispatch.
