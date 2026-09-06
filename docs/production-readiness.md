# Production readiness assessment

Assessed 2026-09-06 after the component, lifecycle, and logging refactor.

## Decision

Suitable as a development foundation and for a controlled, single-process pilot
with trusted tools and disposable or backed-up data. Not ready for general,
unattended production deployment or multi-user operation. Passing functional
tests establishes the tested behavior; it does not establish operational
capacity, hostile-input isolation, or deployment reliability.

## Verification completed

On Windows / Python 3.13.7 with installed LiteLLM 1.100.0:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q ish tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ish.demo --help
```

All 81 tests passed in 47.356 seconds. Compilation, dependency consistency, and
demo import/argument parsing passed. The full suite output was captured in
`test-results.txt` at the repository root. Tests use temporary directories and
do not delete real application workspaces.

Coverage includes queue durability, streaming, serial/concurrent Tasks,
interrupt, subprocess crash/restart, stale Run/Step recovery, cloning, reversible
and permanent deletion, runtime attachment guards, deletion failure propagation,
domain log routing, sensitive-content exclusion, rotation, and log I/O failure.
Linked-path rejection is tested with mocked symlink/junction detection so the
suite needs no Windows link-creation privileges; real link/race behavior on
deployment filesystems still requires platform testing.

The actual LiteLLM SDK is exercised with mock HTTP SSE and a local test
tokenizer. No live model API was called. Linux, Python 3.12, sustained load,
disk-full faults, and power-loss durability were not verified here. Asyncio
debug output reported slow callbacks during this run, consistent with the
synchronous filesystem work described below; no performance target was measured.

## Deployment blockers and limitations

| Area | Current behavior and consequence | Required next work |
| --- | --- | --- |
| Workspace ownership | `services/tasks.py` tracks attached IDs inside one TaskManager only. Separate TaskManager instances/processes can bypass that guard and race JSON/JSONL writes, recovery, or deletion. | Process-level workspace ownership/locking, one shared service container, conflict tests and explicit lock-loss behavior. |
| Persistence throughput | `ConversationStore.get()` replays the entire JSONL file for every delta; appends and metadata writes flush synchronously. Logging also performs synchronous filesystem checks and opens/closes a handler per event. Long conversations and concurrent Tasks can stall the event loop. | Indexed/incremental replay and an ordered persistence worker with explicit durability barriers; retain durable QUEUED semantics. Measure latency, memory, and throughput with realistic histories. |
| Lifecycle trust | Core objects are mutable handles. Generic save operations can recreate missing metadata; callers holding stale Project objects can bypass some deleted-state checks. Repositories are storage APIs, not authorization boundaries. | Reload/validate lifecycle state at all public mutation boundaries, version/conflict checks, and a defined application authorization layer. |
| Permanent deletion | Complete preflight rejects linked/escaping paths, but validation and recursive removal are separate. Another writer can change the tree, and an I/O error can leave a partially removed tree. | Exclusive ownership first; deletion journal/tombstone strategy and recovery tests for partial failures. Back up valuable data before using irreversible removal. |
| Provider cancellation | Cancelling a Run stops delta delivery; Python cannot forcibly cancel a synchronous network read. Daemon cleanup threads survive until reads return/time out. | Verify each deployed provider's timeout behavior, bound outstanding cleanup work, and add long-running cancellation/resource tests. |
| Tool safety and history | Tools are trusted registered Python handlers. Schema validation does not provide permissions or a sandbox. Structured tool calls/results exist only during the Run, limiting later conversation context and auditing. | Durable structured conversation events, least-privilege tool policy and isolation appropriate to the application, side-effect/idempotency tests. Never automatically replay stale Runs. |
| Logs and secrets | Logs exclude conversation/credential content through allowlisted fields, but are best effort and share the data filesystem. Environment references are the only SecretManager backend. | Decide on centralized logs/metrics, storage retention and access controls, alerting, and a secret backend. These service logs do not govern the provider SDK's own diagnostics. |
| Release validation | Dependency ranges exist, but no deployment lockfile or Linux/live-provider verification was performed. Backup, migrations, and schema version coordination are deferred. | Reproducible dependencies, Linux CI, supported-provider smoke tests, backup/restore and upgrade tests, operational runbooks. |

## Scope still planned

LoopEngine is implemented. SingleEngine, GraphEngine, and a TUI are not.
`components/rag`, `mcp`, `skills`, `subagents`, and `workflows` reserve locations
for future CRUD classes/adapters; they currently provide no such behavior.
`components/tools` contains the existing runtime registry, not persistent tool
CRUD. Missing optional engines/components do not prevent a LoopEngine pilot,
but must not be presented as shipped features.

Prioritize workspace ownership and stale-handle lifecycle checks, persistence
performance, structured tool history/policy, then Linux/provider and failure-mode
validation before reconsidering production deployment.
