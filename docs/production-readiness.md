# Production readiness assessment

Assessed 2026-09-07 after service boundary and component selection changes.

## Decision

Suitable as a development foundation and for a controlled, single-process pilot
with trusted tools and disposable or backed-up data. Not ready for general,
unattended production deployment or multi-user operation. Passing functional
tests establishes the tested behavior; it does not establish operational
capacity, hostile-input isolation, or deployment reliability.

## Verification completed

On Windows, using both Python 3.9.13 / LiteLLM 1.80.17 and
Python 3.13.7 / LiteLLM 1.100.0:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q ish tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ish.demo --help
.\.venv39\Scripts\python.exe -m unittest discover -s tests -v
.\.venv39\Scripts\python.exe -m compileall -q ish tests
.\.venv39\Scripts\python.exe -m pip check
.\.venv39\Scripts\python.exe -m ish.demo --help
```

All 111 tests passed on Python 3.9.13 in 93.263 seconds and on Python 3.13.7 in
93.552 seconds. Compilation, dependency consistency, and demo import/argument
parsing passed. Full suite outputs are `test-results-python39.txt` and
`test-results-python313.txt` at the repository root. Tests use temporary directories and
do not delete real application workspaces.

Coverage includes queue durability, streaming, serial/concurrent Tasks,
interrupt, subprocess crash/restart, stale Run/Step recovery, cloning, reversible
and permanent deletion, runtime attachment guards, deletion failure propagation,
domain log routing, sensitive-content exclusion, rotation, and log I/O failure.
Linked-path rejection includes mocked checks and a real Windows junction in a
temporary workspace on both interpreters. Deployment filesystem races still
require platform testing. Additional compatibility tests cover enum formatting,
dataclass behavior, type-hint resolution, nested timeout scopes, external
cancellation, and asynchronous stream cleanup.

The 20 newest boundary/component cases cover stale deleted Project/Task handles,
managed-state save guards, observer failure isolation, optional directory creation,
selection/configuration persistence and validation, initialization failure,
component clone policy, old metadata loading, Project tool isolation through one
LoopEngine, per-Run capability snapshots, and injected conversation storage/context.

The actual LiteLLM SDK is exercised with mock HTTP SSE and a local test
tokenizer on both dependency versions. No live model API was called. Linux,
Python 3.9.25/3.10/3.11/3.12, sustained load,
disk-full faults, and power-loss durability were not verified here. Asyncio
debug output reported slow callbacks during this run, consistent with the
synchronous filesystem work described below; no performance target was measured.

## Deployment blockers and limitations

| Area | Current behavior and consequence | Required next work |
| --- | --- | --- |
| Workspace ownership | `services/tasks.py` tracks attached IDs inside one TaskManager only. Separate TaskManager instances/processes can bypass that guard and race JSON/JSONL writes, recovery, or deletion. | Process-level workspace ownership/locking, one shared service container, conflict tests and explicit lock-loss behavior. |
| Persistence throughput | `ConversationStore.get()` replays the entire JSONL file for every delta; appends and metadata writes flush synchronously. Logging also performs synchronous filesystem checks and opens/closes a handler per event. Long conversations and concurrent Tasks can stall the event loop. | Indexed/incremental replay and an ordered persistence worker with explicit durability barriers; retain durable QUEUED semantics. Measure latency, memory, and throughput with realistic histories. |
| Lifecycle trust | Public Project/Task save and execution now reload lifecycle/ownership state, reject stale deleted handles, and separate runtime state writes. Repositories remain lower-level storage APIs. There is no version/conflict checking across competing writers. | Process ownership, optimistic concurrency/version checks if needed, and an application authorization layer. |
| Permanent deletion | Complete preflight rejects linked/escaping paths, but validation and recursive removal are separate. Another writer can change the tree, and an I/O error can leave a partially removed tree. | Exclusive ownership first; deletion journal/tombstone strategy and recovery tests for partial failures. Back up valuable data before using irreversible removal. |
| Provider cancellation | Cancelling a Run stops delta delivery; Python cannot forcibly cancel a synchronous network read. Daemon cleanup threads survive until reads return/time out. | Verify each deployed provider's timeout behavior, bound outstanding cleanup work, and add long-running cancellation/resource tests. |
| Tool safety and history | Project-specific enabled tool names now constrain each Run, including Projects sharing a LoopEngine. Registered Python handlers remain trusted and have no OS sandbox. Structured calls/results still exist only during the Run. | Durable structured conversation events, OS/resource permission policy and isolation, side-effect/idempotency tests. Never automatically replay stale Runs. |
| Logs and secrets | Logs exclude conversation/credential content through allowlisted fields, but are best effort and share the data filesystem. Environment references are the only SecretManager backend. | Decide on centralized logs/metrics, storage retention and access controls, alerting, and a secret backend. These service logs do not govern the provider SDK's own diagnostics. |
| Release validation | Python 3.9 runtime versions are captured in `constraints-python39.txt`; newer interpreters still use dependency ranges. Linux/live-provider verification was not performed. Backup, migrations, and schema version coordination are deferred. | Validate and maintain dependencies for each deployment target, Linux CI, supported-provider smoke tests, backup/restore and upgrade tests, operational runbooks. |

## Scope still planned

LoopEngine is implemented. SingleEngine, GraphEngine, and a TUI are not.
Python 3.9 compatibility does not change the production decision above. Its
older LiteLLM version needs its own provider-compatibility and dependency
maintenance plan. Python 3.9 dataclasses also have a `__dict__` instead of native
slots; persistence still serializes only declared fields.
`components/rag`, `mcp`, `skills`, and `subagents` reserve locations for future
CRUD/adapters. Workflows has a directory initializer, and tools has Project
enabled-name configuration plus a runtime handler catalog; general definition
CRUD and structured tool transcripts are still planned. Missing optional engines/components do not prevent a LoopEngine pilot,
but must not be presented as shipped features.

UI observer exceptions are isolated through RunEventPublisher, though a blocking
synchronous UI callback can still stall the event loop. Component initialization
is not a cross-file transaction: caught create/clone failures leave a soft-deleted
Project and partial directories may remain. These changes strengthen service
boundaries without solving multi-process ownership or filesystem throughput.

Prioritize workspace ownership, persistence performance, structured tool history/policy, then Linux/provider and failure-mode
validation before reconsidering production deployment.
