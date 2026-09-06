# Architecture

## Overview

The application is a Linux TUI AI client designed for long-running and concurrent AI work.

A user creates a Project with model and runtime configuration. The Project becomes an independent workspace that can gradually accumulate memory, workflows, tools, tasks, and other project-specific state.

The application deliberately separates persistent domain state from runtime execution state.

## Current Implementation

The initial workspace contained documentation only. The `ish` package now
implements the domain and persistence/runtime foundation described below:

* `ish/core`: Project, Task, Message, Run, and Step dataclasses, persisted enums, and major paths (native slots on Python 3.10+)
* `ish/compat.py`: Python 3.9 compatibility adapters; no standard-library monkeypatching
* `ish/engines`: Engine protocol, context snapshots, event types, registry, and LiteLLM LoopEngine
* `ish/components`: selected Project components, tool catalog/Project tool configuration, and component-owned workflow directory initialization; RAG/MCP/skills/sub-agents remain planned
* `ish/providers`: LiteLLM synchronous stream transport and bounded async bridge
* `ish/services/projects.py`: ProjectRepository and ProjectManager with delegated initialization and Task lifecycle coordination
* `ish/services/tasks.py`: TaskRepository metadata persistence, TaskManager lifecycle/conversation snapshot cloning, and runtime-only TaskRuntime definition
* `ish/services/conversation.py`: append-only JSONL conversation events
* `ish/services/context.py`: shared conversation ordering and Run/clone snapshots
* `ish/services/access.py`: authoritative Project loading and lifecycle/ownership checks
* `ish/services/runs.py`: RunRepository persistence, RunEventPublisher notifications, and RunManager ownership of TaskRuntime queues/orchestration/recovery
* `ish/services/steps.py`: StepRepository metadata persistence, StepManager lifecycle, and StepEventRecorder translation
* `ish/services/secrets.py`: environment-backed credential resolution via `env:NAME` references
* `ish/services/logging.py`: domain-scoped rotating operational logging
* `ish/services/storage.py`: atomic JSON helpers, validated directory removal, StorageIO background execution, and cancellation draining
* `ish/services/locking.py`: workspace OS ownership, shared attachment guards, and scoped transaction locking
* `tests/support/fake_engine.py`: deterministic test fixture, excluded from the product package
* `ish/demo.py`: a one-request streaming CLI example with an optional arithmetic tool

There is currently no TUI, dedicated SingleEngine, or GraphEngine. Project
configuration is intentionally minimal (model, optional temperature/API base,
default engine, and a credential reference); it has no API-key
field. Archive import/export, migration, cleanup policies, and subsystem data
cloning are also deferred.

The supported runtime floor is Python 3.9. The same domain fields and JSON/JSONL
formats apply across versions. Python 3.9 lacks native dataclass slots, so the
compatibility decorator uses ordinary dataclasses there and retains native
slots on 3.10+. Explicit string-valued enums preserve their string/JSON values.
Optional annotations remain resolvable through `typing.get_type_hints` on 3.9.

Python 3.9 installs LiteLLM 1.80.17 and jsonschema 4.25.1; newer interpreters keep
the existing dependency ranges. `async-timeout==5.0.1` supplies timeout scopes
below Python 3.11, and 3.11+ uses `asyncio.timeout`. Async closing has a 3.9
adapter. Windows junction checks below 3.12 inspect the mount-point reparse tag
through `lstat`, so deletion safety is not disabled on older Python versions.
These adapters do not change queue ownership, cancellation intent, Engine
events, or persistence responsibility. Python 3.9 and 3.13 run the same suite,
including actual SDK/mock SSE and real Windows junction tests.

The runtime uses one owning service container per workspace, in one process/event
loop. ProjectRepository creates WorkspaceOwnership, shared by ProjectManager and
TaskManager through ProjectAccess. Synchronous manager transactions acquire an
exclusive nonblocking OS lock on `<projects-root>/.ish.lock`. Each attached Task
retains ownership from before recovery through shutdown and storage drain, even
when idle. Competing repository instances/processes raise WorkspaceBusyError
before recovery or lifecycle mutation. Different workspaces are independent;
Tasks in the owning workspace still execute concurrently.

The lock uses Windows byte-range locking or POSIX flock, releases on handle close
or process exit, and never uses PID age to steal ownership. The permanent lock
file is outside Project trees and must never be deleted while processes might
access the workspace. This is cooperative local-filesystem locking, not a
distributed lease or protection against arbitrary filesystem writers. NFS/SMB and
Linux deployment behavior need platform verification. Create a fresh repository
after process fork; do not share inherited live service instances.

Within the owner, synchronous transactions use an instance-owned threading RLock.
Attached Task IDs are shared across TaskManagers bound to the same repository,
preventing duplicate attachment or lifecycle mutation even while idle. Shutdown
all affected runtimes before removal/cloning. No locks enter persisted models.

Public manager APIs supply ownership scopes automatically. ProjectRepository also
guards reads/writes. Direct lower-level Task/Run/Step repository, ConversationStore,
and component writes require the caller's `repository.ownership.scope()` because
they cannot infer an arbitrary injected workspace root. They remain trusted
storage APIs, not an authorization boundary.

ProjectManager binds its ProjectAccess to TaskManager. Mutation/execution entry
points reload Project state rather than trusting stale handles. Public save
edits existing active Project/Task configuration only; it cannot change managed
deleted/execution state or recreate missing metadata. Task runtime state writes
use RunManager's internal TaskManager path, requiring an attachment. Repositories
remain lower-level storage APIs and are not authorization boundaries.

ConversationContextBuilder owns turn ordering for both Run context and Task
cloning. RunManager and TaskManager receive a ConversationStore factory rather
than choosing storage paths at every call. Defaults retain the existing Task
JSONL layout. RunManager retains one store per attached Task and releases it on
shutdown. ConversationStore projects only newly appended complete events using
a byte offset and file identity/size/mtime. Replacement/truncation resets the
projection; malformed complete records repeatedly fail. Reads return deep copies.
A threading RLock protects each store. External in-place edits to append-only
records are unsupported.

StorageIO runs filesystem transactions with asyncio.to_thread, with one in-flight
operation per RunManager and no unbounded executor submission queue. Recovery,
QUEUED creation, Run begin/finalization, context reads, Step writes, and deltas
await this ordered lane. Engines/tools and UI callbacks stay on the event loop.
Injected synchronous storage factories, repositories, context builders, and
capability resolvers must support worker-thread calls: no required running event
loop or thread-affine connections.

Cancellation drains submitted disk operations before propagating. Accepted
submit/start/shutdown calls finish before forwarding cancellation: a cancelled
submit can still accept and schedule its request. Do not blindly resubmit.
Interrupt during Run preparation records intent and finalizes the claimed Run
as interrupted without executing its Engine. Shutdown holds ownership until
workers and disk work stop. An unresponsive filesystem can delay shutdown.
Synchronous Project/Task APIs remain available; async UI callers can use
`StorageIO(projects.ownership).run(projects.create, ...)`. Do not pass coroutine
APIs such as RunManager.submit to StorageIO.

Engine contexts are snapshots of committed turns through the current request.
Assistant answers are paired with user inputs by Run ID, since queued requests
may be appended before a preceding answer exists. Future queued inputs are
excluded. Engines return async iterators; RunManager closes streams that expose
`aclose`. Engines must propagate cancellation and put only JSON-safe, sanitized
values in event metadata and errors. Raw execution exceptions are represented
by a generic persistent failure message to avoid copying provider credentials.

Run creation is ordered before input commitment. Recovery treats a queued
input already claimed by a persisted Run as committed, even if the process
stopped before the commit event. It interrupts stale pending/running Runs and
Steps and streaming Assistant messages without replaying claimed requests.
The worker owns finalization, including when cancellation arrives before the
Engine coroutine starts. Failed Engines do not discard subsequent queued input.

Shutdown stops new submissions, interrupts active executions, and stops workers
while retaining queued JSONL messages. `wait_idle` reports a stopped worker if
shutdown prevents its queue from draining. Acknowledged writes remain fsynced;
RunManager waits off the event loop. Mutable JSON uses atomic replacement and
POSIX directory fsync. Conversation appends fsync the file each time and sync the
directory on first creation; appending does not change the directory entry.
QUEUED is durable before queue insertion; streaming notifications follow durable
deltas. Per-delta operational logging is omitted because JSONL already records
the event; message lifecycle and domain operation logging remain. Conversation replay ignores a final unterminated record, and the
next append truncates only that incomplete tail. Malformed complete records
raise errors rather than silently discarding history.

`ProjectManager.delete` and `TaskManager.delete` mark metadata in place by
default. Their keyword-only `permanent=True` option removes the complete owned
tree after runtime, identity, path containment, and symlink/junction checks.
Project deletion checks all Tasks, including soft-deleted Tasks, before removal.
Restore reloads metadata, so permanently deleted objects cannot be resurrected
by restore. Workspace ownership excludes cooperating writers during removal; arbitrary
filesystem mutation remains unsupported; recursive
deletion is not transactional and an I/O failure may leave a partial tree.
The old `soft_delete` API was removed. Task clones normalize turn
order and copy conversation/configuration with fresh Message and Task IDs,
clear Run links, and cancel copied queued requests. They do not copy Runs,
Steps, or artifacts. Project cloning delegates Task snapshots to TaskManager
and copies Project configuration. Selected components define their own clone
policy: ToolComponent copies enabled tool names, WorkflowComponent starts with
an empty directory. Secrets and other subsystem artifacts are not copied by default.

The test suite is `python -m unittest discover -s tests -v` after installing the
package dependencies. It needs no live API or credentials and includes an
actual subprocess crash/restart test plus an installed LiteLLM SDK test using
mock HTTP SSE and a local test tokenizer. The sections below describe the broader target architecture;
features beyond the scope above remain planned.

## Service Module Organization

Services are grouped by domain or shared responsibility. There are 11 functional
modules plus `__init__.py`:

| Module | Responsibility |
| --- | --- |
| `projects.py` | ProjectRepository, ProjectManager, component initialization coordination |
| `tasks.py` | TaskRepository, TaskManager, runtime-only TaskRuntime definition |
| `runs.py` | RunRepository, RunManager, separate RunEventPublisher class |
| `steps.py` | StepRepository, StepManager, StepEventRecorder |
| `conversation.py` | Append-only conversation persistence and its incremental projection |
| `context.py` | ConversationContextBuilder: Run history selection and clone ordering |
| `access.py` | ProjectReader protocol and shared ProjectAccess lifecycle checks |
| `storage.py` | JSON serialization/fsync, validated removal, ordered background I/O |
| `locking.py` | OS workspace ownership and local synchronization |
| `logging.py` | Safe operational log routing and rotation |
| `secrets.py` | SecretResolver and environment-backed SecretManager |

`conversation_context.py` was renamed to `context.py`; the explicit class name
ConversationContextBuilder is retained, distinct from engines.base.EngineContext.
Conversation projection remains in conversation.py; context selection policy has
no storage dependency.

The former io.py and deletion.py are consolidated into storage.py, with separate
sections for primitives, owned-tree removal, and background execution. The former
events.py contained only RunEventPublisher, which now lives in runs.py as its own
class; notification handling is not folded into RunManager methods. These moves
do not change domain responsibilities, lock lifetimes, cancellation, or formats.

Keep access.py independent: both ProjectManager and TaskManager use it, and moving
it into projects.py would introduce a Task-to-Project service dependency cycle.
Keep locking.py independent of storage.py so OS ownership remains usable without
coupling its implementation to disk serialization or async I/O orchestration.
Logging and secrets also retain their independent consumers and responsibilities.

Imports must use the new modules; no forwarding files remain for removed modules:

```python
from ish.services.context import ConversationContextBuilder
from ish.services.storage import StorageIO, remove_owned_tree
from ish.services.runs import RunEventPublisher
```

## Repository and Manager Responsibilities

Each mutable metadata domain has a repository and a manager in the same service
module:

| Module | Repository | Manager |
| --- | --- | --- |
| `projects.py` | ProjectRepository | ProjectManager |
| `tasks.py` | TaskRepository | TaskManager |
| `runs.py` | RunRepository | RunManager |
| `steps.py` | StepRepository | StepManager |

Repositories own metadata serialization, atomic writes, path resolution, loading,
listing, and ID/parent ownership checks. Managers own lifecycle policy and
delegate storage to their repositories. TaskRepository also initializes the
Task subsystem root and reloads persisted state for TaskManager's active-Run
guard. StepRepository handles the existence check used by StepManager to reject
duplicate Step IDs. ProjectManager continues to delegate subsystem initialization
and Task lifecycle work without knowing Task storage internals.

Managers expose their repository through `repository`. TaskManager and
StepManager can be constructed without arguments or with an injected repository.
RunManager accepts `repository=RunRepository()`; its previous `runs=` argument
and `.runs` access remain compatibility aliases. Passing both constructor
arguments is rejected. RunManager retains its execution-oriented API while
ProjectManager and TaskManager retain workspace/session lifecycle APIs.

RunManager lives in `runs.py`; TaskRuntime is defined in `tasks.py` and imported
by RunManager, which still owns all runtime scheduling. Import RunManager from
`ish.services.runs`. The previous `run_manager.py` module has been removed.
This is module consolidation only: RunRepository and RunManager are still
separate classes, and persisted Task objects still contain no runtime objects.

Message persistence intentionally remains in ConversationStore, whose append-only
event log differs from mutable domain metadata. Engine remains an execution
protocol and does not acquire a persistence repository. JSON/JSONL formats,
enum values, and queue/recovery semantics remain unchanged. Project metadata
adds a backward-compatible `components` list; older workspaces default to no
enabled components, regardless of which directories already exist.

## LiteLLM Loop Execution

LoopEngine calls synchronous `litellm.completion(stream=True, ...)` through
`providers/litellm.py`. Each request has a dedicated daemon thread that
creates, reads, and closes its provider stream. A bounded queue bridges chunks
to the async event loop with backpressure. Cancellation stops delivery at once;
Python cannot forcibly interrupt an in-progress synchronous network read, so
the worker closes the stream after the read returns or its provider timeout
expires. These cleanup threads never execute tools or touch persistence.

The Engine uses Project configuration and SecretResolver, emits an LLM Step,
and forwards each `delta.content` as TEXT_DELTA. Indexed `delta.tool_calls`
fragments are accumulated into complete call IDs, function names, and JSON
arguments. Only registered tools are callable. The entire batch is validated
against JSON Schema before any tool executes, and calls are executed serially
as individual tool Steps. Their observations are added to the next LLM request.

A normal `stop` response ends the Run. A `tool_calls` response starts another
round when budget remains. Missing/abnormal finish reasons, malformed tools,
tool failures, timeouts, and exhausted iteration budgets fail the Run. No
engine retries are performed, and LiteLLM receives `num_retries=0`. Calls on the
last permitted iteration are not executed without a follow-up LLM round.
Defaults are eight LLM rounds, 60 seconds per whole LLM round, and 30 seconds
per async tool. Tool handlers must propagate cancellation and avoid blocking.

The Engine still writes no files. RunManager and StepEventRecorder persist its
events through the existing services. RunManager's optional synchronous
`on_event(run, event)` observer receives snapshots after persistence, enabling
immediate display. RunEventPublisher isolates observer exceptions, recording a
sanitized `observer.failed` event without failing the Run. Callbacks must remain
synchronous and nonblocking. Engine/storage exceptions retain their execution
failure semantics; display errors are not execution errors.

All visible text in a Run accumulates in its existing single Assistant Message.
The structured assistant tool calls and tool results are currently a transient
per-Run transcript. Only Step identity, iteration, call ID, status, and safe
failure descriptions are persisted for tools. Raw arguments/results and
provider response objects are not copied into Step metadata. Future support
for a durable structured tool transcript must go through ConversationStore;
stale Runs remain interrupted and are never automatically resumed.

SecretManager currently resolves environment references only. It does not
implement a secret vault or write secrets beneath ProjectPaths.secrets. If no
credential reference is set, LiteLLM can use its provider's normal environment
authentication or a keyless local endpoint. No global SDK key/logging settings
are mutated by the Engine.

Reusable Tool, ToolRegistry, ToolComponent, and ToolPaths are exported from
`ish.components.tools`. ComponentRegistry validates selected identities and
delegates initialization/configuration/cloning to each component. ProjectManager
accepts the registry, and `create(..., components=("tools", "workflows"))` stores
the selection in Project metadata. It does not know component directory layouts.
`ProjectPaths.tools` and `.workflows` are removed; ToolPaths and WorkflowPaths
own these roots. The default selection is empty, and core Task initialization
is mandatory independently of optional components.

ToolComponent persists enabled names in `tools/component.json`. Its application
catalog contains trusted handlers, which are not persisted. RunManager receives
the registry as CapabilityResolver and resolves a fresh Project-scoped tool
snapshot into EngineContext for each Run. LoopEngine consumes this snapshot;
it no longer owns a global tool registry. Configuration changes affect later
Runs. Missing registered components fail closed before provider execution.
Project selection is not inferred from existing directories, and saved component
names never trigger arbitrary dynamic imports.

Component initializers must be idempotent. Disabling a component retains data;
re-enabling initializes only missing state. A failed create/clone is soft-deleted,
and restore retries initialization. Failed selection changes retain the previous
manifest but can leave partial directories; initialization is not transactional
across components or process crashes. Future component packages own their CRUD
and connection lifecycles. WorkflowComponent only initializes its directory;
SingleEngine/GraphEngine and workflow/RAG/MCP/Skill/sub-agent CRUD remain planned.
New ProjectConfig instances default to `loop`; old persisted `fake` selections
are preserved and require an explicit configuration change.

## Domain Hierarchy

The primary execution hierarchy is:

Project
→ Task
→ Run
→ Engine
→ Step

Conversation belongs to Task rather than Run.

Conceptually:

Project
├── Configuration
├── Memory
├── Tools
├── Workflows
└── Tasks
└── Task
├── Conversation
└── Runs
└── Run
├── Engine
└── Steps

## Project

Project represents a persistent workspace.

It contains long-lived configuration such as:

* primary LLM
* API base
* temperature
* reasoning configuration
* embedding model
* reranker model
* SLM model
* default Engine
* metadata

ProjectPaths exposes major Project paths such as:

* root
* memory
* tasks
* secrets
* state
* logs
* cache

ProjectPaths intentionally does not expose every nested subsystem path.

For example, ProjectPaths may expose `memory`, while MemoryManager owns everything beneath that directory.

## ProjectManager

ProjectManager is a lifecycle orchestration service rather than a persistence implementation.

Its responsibilities include:

* Project creation
* loading and saving through ProjectRepository
* cloning
* archive import/export
* soft deletion
* restoration
* backup
* migration coordination
* subsystem initialization coordination
* Task cleanup coordination

Subsystem initialization is delegated through initializer objects.

ProjectManager does not know how Memory, Workflow, Secrets, Tools, or Tasks internally structure their directories.

## Task

Task represents one long-lived independent AI session inside a Project.

A Task may continue across many user messages and many Runs.

A Task can be:

* created
* cloned
* soft deleted
* restored

Task stores persistent state such as:

* Task ID
* Project ID
* title
* status
* default Engine override
* current Run ID
* metadata

Runtime objects are intentionally excluded.

## Task Runtime

RunManager maintains runtime-only Task state.

Conceptually:

TaskRuntime
├── asyncio.Queue[message_id]
├── worker asyncio.Task
├── current execution asyncio.Task
└── closed state

These objects are not serialized.

The durable Message queue remains the source of truth.

## Conversation

ConversationStore owns conversation persistence.

Storage format is append-only JSONL.

Instead of rewriting complete Assistant messages during streaming, events are appended.

Example:

message.create
message.delta
message.delta
message.delta
message.status

Loading a conversation replays the event stream to reconstruct current Message objects.

This allows partial streaming output to survive unexpected process termination.

## Message Lifecycle

Typical user input:

QUEUED
→ COMMITTED

Typical Assistant response:

STREAMING
→ COMPLETED

Interrupted Assistant response:

STREAMING
→ INTERRUPTED

Failed response:

STREAMING
→ FAILED

Every user request is initially stored as QUEUED.

Only when RunManager actually starts a Run is the corresponding Message promoted to COMMITTED.

This prevents requests from disappearing if the process terminates before execution.

## Run

Run represents one Engine execution for one user request.

A Run contains:

* Run ID
* Task ID
* input Message ID
* Assistant Message ID
* Engine name
* status
* timestamps
* error
* metadata

A Task can contain many Runs over its lifetime.

Run is intentionally separate from Task so that interrupt, failure, retry policy, diagnostics, and execution history remain precise.

## Scheduling

Runs inside one Task execute serially.

Example:

Task A:
Run 1 → Run 2 → Run 3

Different Tasks have independent workers and may execute concurrently.

Example:

Task A: ──────────────
Task B: ────────
Task C: ───────────────────

This model preserves conversation ordering while supporting parallel AI work.

## Interrupt Semantics

Normal input during a Run does not interrupt that Run.

It is persisted as QUEUED and processed later.

Explicit interrupt:

1. stores the new request durably if one was supplied
2. cancels the current execution
3. marks the Run INTERRUPTED
4. marks partial Assistant output INTERRUPTED
5. preserves all queued user requests
6. lets the Task worker continue

## Engine

Engine defines the execution strategy.

Engine strategies (LoopEngine is implemented; SingleEngine and GraphEngine remain planned):

### SingleEngine

One direct completion-style request.

Typical use:

* short questions
* translation
* summarization
* simple generation

### LoopEngine

Agent-style iterative execution.

Typical pattern:

reason
→ action
→ observation
→ reason
→ action
→ observation

Typical use:

* coding
* debugging
* investigation
* multi-step tool use

### GraphEngine

Executes a predefined workflow graph.

Typical use:

* repeatable workflows
* retrieval pipelines
* structured research
* multi-stage processing

Engine implementations emit EngineEvent objects instead of directly mutating persistence layers.

## Step

Step is the smallest persistent observable execution unit inside a Run.

Possible Step kinds include:

* llm
* tool
* shell
* retrieval
* rerank
* graph_node

A Run may therefore look like:

Run
├── Step: llm / reason
├── Step: filesystem / read
├── Step: llm / reason
├── Step: shell / pytest
└── Step: llm / analyze

Step statuses:

* pending
* running
* completed
* failed
* interrupted
* cancelled

## StepManager

StepManager owns Step lifecycle and delegates metadata persistence to StepRepository.

It does not perform the actual work represented by a Step.

Engine emits events such as:

* STEP_STARTED
* STEP_COMPLETED
* STEP_FAILED
* STEP_INTERRUPTED
* STEP_CANCELLED

StepEventRecorder translates those events into StepManager operations.

This keeps Engine execution independent from filesystem persistence.

## Recovery

The application assumes that execution may stop unexpectedly.

On restart:

A stale Run that was PENDING or RUNNING becomes INTERRUPTED.

A stale Step that was PENDING or RUNNING becomes INTERRUPTED.

A stale Assistant Message that was STREAMING becomes INTERRUPTED.

QUEUED user messages are restored into the runtime scheduling queue.

Stale Runs are not automatically retried.

This is intentional because prior Steps may have executed side effects such as:

* modifying files
* running shell commands
* sending API requests
* writing external data
* creating commits

Automatic Run replay could execute those effects twice.

## Persistence Layout

A conceptual Project layout is:

projects/
└── <project-id>/
├── project.json
├── memory/
├── tools/
├── workflows/
├── secrets/
├── state/
├── logs/
└── tasks/
├── .trash/
└── <task-id>/
├── task.json
├── conversation.jsonl
├── attachments/
├── state/
├── logs/
└── runs/
└── <run-id>/
├── run.json
├── state/
├── logs/
└── steps/
└── <step-id>/
├── step.json
├── state/
├── logs/
└── artifacts/

Subsystem implementations are free to evolve below their owned root directory.

## Logging

Conversation storage and application logging are separate concerns.

Conversation contains user-visible communication.

Logs contain operational diagnostics.

Recommended logging hierarchy:

application logs
+
Project logs
+
Task/Run/Step logs when necessary

Sensitive credentials must never be written to normal logs.

The current implementation uses standard-library `logging.Logger` and
`RotatingFileHandler` to append structured JSON to each domain's
`logs/service.log` (1 MiB with three backups). Project/Task/Run/Step repositories
log persistence operations; managers log lifecycle and runtime operations.
Conversation mutations log event type and identity under Task, never text.
SecretManager can log resolution success/failure under Project, excluding both
reference and value. LoopEngine supplies that Project scope to its default resolver.

Log fields are allowlisted: UTC time, event, IDs, status, count, permanent flag.
No raw exceptions, arbitrary metadata, prompts, answers, or provider objects are
logged. Domain directories are not recreated solely for logging. Handlers close
after each write so permanent deletion works on Windows. Log write errors emit
a generic warning and do not normally prevent domain persistence. Logs are best
effort operational diagnostics, not a crash-durable or transactional audit trail.

Permanent deletion removes the deleted domain's own logs. The final Task
deletion record is written to Project logs; the final Project deletion record
goes to `<projects-root>/logs/service.log`. Default soft deletion retains all
domain logs. Linked log files/directories are rejected; all filesystem access
still assumes trusted paths and exclusive process ownership.

## UI Model

The UI should expose complexity gradually.

Primary concepts visible to ordinary users:

Project
→ Task

Run, Engine, and Step may be shown as execution details.

A Task view can show:

* conversation
* current Engine
* active Run
* current Step
* queued requests
* other active Tasks

A separate Task navigator should provide parallel-work visibility while allowing the current Task to use most terminal width.

## Future Direction

Major remaining components include:

* production LiteLLM SingleEngine
* durable structured tool transcripts and broader LoopEngine provider compatibility
* GraphEngine
* Project-scoped tool discovery and configuration beyond the runtime ToolRegistry
* filesystem tools
* shell tools
* SecretManager backends beyond environment references
* MemoryManager
* WorkflowManager
* centralized log export and durable audit requirements if needed
* prompt-toolkit UI
* additional live-provider and Linux integration tests
