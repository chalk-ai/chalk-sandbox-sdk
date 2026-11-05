# Sharded SDK fixture validation

This internal fixture builder uses the existing `WeightStream._commit` and Volume
grouped upload path. It creates a sharded baseline, commits one patch payload with
its manifest and `CURRENT` in one atomic upload group, and constructs independent
target shards from persisted remote patch decoding. An independent scalar XOR
oracle, exact changed counts, and trainer tensor hashes verify every target byte.
Fresh immutable remote reference shards are downloaded and checked individually.
The actual SDK materializer accepts both streams in CPU tests.

Each commit attempt has an exclusive local marker. This fences harness replay;
it does not provide backend exactly-once execution or disable native upload
retries. A lost response fails the phase. The caller must own two pre-created,
empty volumes and one isolated writer. This adds no public publishing API.

The builder accepts ordinary BF16 safetensors shards in an owned writable guest
directory. It validates the complete tensor catalog, architecture, config/index
mapping, payload checksums and bounds, and rejects source tensor changes during
construction. It generates a canonical HF shard index and copies root auxiliary
files into the reference. Symlinked HF cache layouts require separate reviewed
staging; arbitrary nested metadata directories are not supported.

## Separate frozen remote candidate — unexecuted

`sharded-synthetic-frozen-v4` is separate from the unchanged full-8B frozen v4.
Its plan SHA-256 is
`35869f0d1e99e25ab8572a24b57dbc39e59b4e2e5b58547390ec948c7d5a013d`.
The local artifact is under
`chalk-private/.artifacts/weight-sync-p0/sharded-synthetic-frozen-v4/`.
It contains 28 frozen helper sources, 239 controller SDK sources, the reviewed
runtime overlay and pinned prebuilt image, and a compiled 91,439-byte entrypoint.
`preview.json` contains exact validation, execution, cleanup and verification
commands. No execution authorization or resource allocation has been created.

The proposed phase uses the singleton Oracle HOST pool, **4 CPU, 8 GiB RAM,
zero GPU**, with an absolute 600-second phase deadline and 120-second cleanup
reserve. Its relative sandbox TTL is at most the remaining work time, reserving
the complete lost-create/reconciliation window. The watchdog is the sole cleanup API owner. The controller creates an owned
session before work, requests cleanup in finally, and waits without issuing
release calls. At cutoff or launcher death the watchdog stops and proves absence
of the complete controller group, then retries cleanup using bounded,
inert-until-registered worker groups. A kernel lock prevents overlapping cleanup
supervisors. Each worker starts an independent OS monitor before authorization;
it watches the full supervisor and worker identities and the absolute deadline
while API calls run, and kills the entire group on owner death, timeout or exit.
Durable per-attempt files retain identities independently of mutable ledger
updates. Teardown and the verifier enumerate nonzombie group descendants even
when their leader has exited. Every
attempt reloads canonical UUID/owner identities and retains phase-specific error
history. Unknown UUIDs or failed native/metadata absence proofs never become
successful release claims. A deadline failure remains explicit for parent
intervention; elapsed lease time alone does not prove resource release.
A separate `recover_release_only` preview command allows at most 120 seconds of
release-only recovery after expiry. It stops exact owned groups and retries
compute/data release while respecting ambiguous-create horizons, without new
creates or changes to the original lease. Execution failure is permanent even
when release recovery succeeds. Frozen v1, v2 and v3 remain immutable, superseded, and
unexecuted. Before-ready spawn failures retain durable inert resolution so later
release attempts can proceed. An unknown never-authorized bootstrap permits
resource release but remains explicitly unproved for strict local-process
verification; no guessed PID is signaled. Parent process discovery occurs before
attempt directory publication. A crash or request-write failure leaving a
requestless, never-authorized directory also permits resource release while
strict local-process verification remains failed. Missing requests with an
authorization marker remain a hard failure. Post-readiness monitor failures stop
the controller and enter release cleanup while retaining canonical late UUIDs
and create markers.

Before creating metadata and again before sandbox submission, the launcher
checks live host capacity. The existing membership helper retains its stronger
16 CPU / 128 GiB free-placement admission check; the sandbox request is 4 / 8.
The separate small-fixture physical gate requires 2 GiB free host disk, and the
guest must report 512 MiB available and prove an actual non-sparse 128 MiB quota
reservation and release before generation or uploads. This does not change the
full-8B package's 120 GiB host disk gate or its authorization requirements.

The remote fixture is generated synthetic data: 16 tensors, 8,388,608 BF16
elements, 16 MiB source data, four shards, and exactly 83,886 changed elements.
Owned guest files plus a conservative 48 MiB remote publication reserve are
checked against 256 MiB. The quota probe is included separately in peak accounting
and released before source generation. Remote publication byte totals are checked
against their reserve. No HF/model or image bytes are downloaded to the workstation.

Measurements include whole-process peak RSS, owned file logical/allocated bytes,
owned HOME/XDG_CACHE_HOME/TMPDIR cache bytes, and statvfs free bytes at controlled
stages before/after all three upload groups. The exact owned runtime's ancestor
cgroups supply `memory.current`, `memory.peak`, memory limits and CPU limits before
and after proof. Missing enforced limits or kernel counters fail validation.
Guest-visible cgroup observations are retained separately.

File peaks are **observed samples**, not a continuous filesystem quota guarantee.
Unlinked/open cache files and backend storage object overhead can affect statvfs
without appearing in directory totals; filesystem free-byte deltas are not
exclusive attribution. The source-size/reserve checks bound known fixture writes.
These limitations must be resolved conservatively before proposing a full-size
resource change.

## Local validation and remaining work

All **187 weight-sync harness tests** pass, including 36 focused sharded tests.
They cover actual SDK batch staging, atomic pointer visibility, both sides of
lost patch/reference responses, baseline/patch corruption, wrong index mappings,
changing source bytes, transient cleanup retries and late identity recovery,
unknown UUID deadline failure, launcher death during create, reused-PID refusal,
and enforced runtime limits/peak counters. Six real local process tests exercise
post-authorization blocked work during supervisor death, deadline expiry, inert
startup death, worker success/failure with inherited children, and descendants
after leader exit. Recovery tests preserve execution failure and metadata
reconciliation horizons; a kernel-lock test rejects a second cleanup API owner.
Additional fault regressions exercise actual Popen failure and child exit before
readiness, a never-authorized identity gap, and post-ready ps/readiness/ledger
errors. The latter prove sole-owner cleanup and preserve late canonical
UUID/owner/create markers. Parent-identity timeout and request-publication faults
exercise retry in both normal cleanup and release-only recovery; recovery keeps
the original lease and permanent execution failure. Fresh read-only checks of 24 retained test ledgers
prove all 21 registered groups have no nonzombie members.
Added Python sources pass Ruff and ty.

Actual remote SDK cache/upload/cgroup/disk measurements remain **unmeasured**.
The candidate requires parent source/command review before any allocation.
Only after genuine small-pipeline evidence and independently proved release may
a conservative full-8B streaming resource proposal be prepared. Full-8B peaks,
native fused mapping, inference behavior and performance remain unmeasured here.
