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
* `ish/engines`: Engine protocol, context snapshots, event types, registry, BaseEngine event lifecycle and LiteLLM chunk handling, LiteLLM LoopEngine, and sequential PipelineEngine/PreparationStep
* `ish/components`: generic directory/JSON Component base and registry; Tool, Workflow and Subagent definition CRUD; requested runtime capabilities with a separate Tool adapter; RAG/MCP/skills remain planned
* `ish/providers`: LiteLLM synchronous stream transport, bounded async bridge and request container copying
* `ish/inference`: reusable EmbeddingModel/RerankModel clients without Engine or persistence dependencies
* `ish/services/components.py`: ComponentData lifecycle-checked, workspace-locked access to selected component data
* `ish/services/projects.py`: ProjectRepository and ProjectManager with delegated initialization and Task lifecycle coordination
* `ish/services/tasks.py`: TaskRepository metadata persistence, TaskManager lifecycle/conversation snapshot cloning, and runtime-only TaskRuntime definition
* `ish/services/conversation.py`: append-only JSONL conversation events
* `ish/services/context.py`: shared conversation ordering and Run/clone snapshots
* `ish/services/access.py`: authoritative Project loading and lifecycle/ownership checks
* `ish/services/runs.py`: RunRepository persistence, RunEventPublisher notifications, and RunManager ownership of TaskRuntime queues/orchestration/recovery
* `ish/services/steps.py`: StepRepository metadata persistence, StepManager lifecycle, and StepEventRecorder translation
* `ish/services/results.py`: RunResultQuery, read-only Project/Task views of Run observations
* `ish/services/logging.py`: domain-scoped rotating operational logging
* `ish/services/storage.py`: atomic JSON helpers, validated directory removal, StorageIO background execution, and cancellation draining
* `ish/services/locking.py`: workspace OS ownership, shared attachment guards, and scoped transaction locking
* `tests/support/fake_engine.py`: deterministic test fixture, excluded from the product package
* `ish/demo.py`: a one-request streaming CLI example with an optional arithmetic tool

There is currently no TUI, dedicated SingleEngine, or GraphEngine. Project
configuration holds default_engine and extensible JSON completion, engines,
task_defaults and data sections. Task.config holds overrides. CompletionResult
is persisted in Run metadata; ExecutionResult is a computed query view in core/results.py. Archive import/export, migration, cleanup policies, and subsystem data
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
loop, with one RunManager instance per Task. ProjectRepository creates WorkspaceOwnership, shared by ProjectManager and
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
`aclose`. Engines must propagate cancellation and put JSON-safe
values in event metadata. Execution error details are preserved as strings with
stable Run error codes; there is no exception-message masking.

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
policy: the common Component base copies configuration and records for Tools,
Workflows and Subagents. Record IDs remain stable within the new Project so graph
references still resolve. Other subsystem artifacts are not copied by default.

The test suite is `python -m unittest discover -s tests -v` after installing the
package dependencies. It needs no live API or credentials and includes an
actual subprocess crash/restart test plus an installed LiteLLM SDK test using
mock HTTP SSE and a local test tokenizer. The sections below describe the broader target architecture;
features beyond the scope above remain planned.

## Task-bound Run API and lifecycle notifications

RunManager(tasks, engines, task=task) binds exactly one Task. Its public runtime
methods no longer take Project/Task: start(), submit(content), wait_idle(),
interrupt(), shutdown(). Project ownership and current Task/configuration are
reloaded through TaskManager before use. One manager owns one TaskRuntime and one
conversation store. Different managers share the workspace service container and
OS ownership coordinator; their Tasks still run concurrently. shutdown releases
only the bound Task after draining its worker/storage; other Tasks continue.

New requests validate Engine and selected component registrations before admission.
RunRequestError exposes a stable code for registration errors without a new
message/Run or attachment. Accepted requests retain QUEUED -> COMMITTED durability.
Recovery still never replays stale Runs. Restored queued requests with unavailable
Engines fail with engine_not_registered and retain their Run/conversation history.

RunEvent/RunEventType are separate from EngineEvent and are emitted by RunManager,
not Engines. on_run_event receives STARTED/COMPLETED/FAILED/INTERRUPTED after durable
state transitions, on the event loop, with detached snapshots and callback-error
isolation. Recovery emits INTERRUPTED for each newly recovered stale Run. Run's
optional error_code field is backwards compatible with old metadata; ExecutionResult
also exposes error/error_code. Exception details are preserved, not masked.

Codes distinguish engine_not_registered, component_not_registered (admission),
capability_failed, engine_failed, interrupted and process_restart. wait_idle still
means the queue drained; clients inspect terminal Run events/results for failures.
A storage error before durable finalization propagates through wait_idle/shutdown;
no terminal callback claims a terminal save that failed. Notifications are not a
persisted subscription log and a reconnect should query the Run repository.

ProjectConfig is an open dict subclass, not a dataclass. Arbitrary top-level keys
survive save/load/clone and are included in EngineContext.settings with Task
merging. Constructor kwargs, mapping access, existing attribute shortcuts and
JSON to_dict/serialize/deserialize are supported. Reserved execution sections
retain semantic validation; no field-name-specific secret policy remains.

Components declare capability names and implement resolve(project, capability).
ComponentRegistry invokes only matching components; the requested capability is
the only value built. Definition CRUD validates data without runtime handlers.
Tool handler binding occurs only while resolving tools for execution. Subagent
and Workflow JSON CRUD likewise has no runtime registration dependency.

## Service Module Organization

Services are grouped by domain or shared responsibility. There are 12 functional
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
| `results.py` | RunResultQuery: Project/Task-scoped queries of Run-owned observations |

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
Logging retains its operational responsibility.

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

## Flexible Configuration and Project Execution History

ProjectConfig owns default_engine plus completion, engines, task_defaults and data
JSON dictionaries. Configuration validation rejects runtime objects, non-string
keys and non-finite numbers at construction/save; arbitrary field names are allowed.
ProjectRepository migrates old flat model/temperature/api_base fields only when
the completion section is absent; current-format top-level keys remain unchanged. Old Task JSON defaults config to {}. There is no credential resolver.

TaskManager.create copies task_defaults then merges explicit config. Task save and
clone preserve config with independent containers. EngineContext.settings(name)
merges Project completion/engines[name]/data with Task overrides. Arrays/scalars
replace defaults; nested dicts merge. Engine constructor arguments override saved
values. Loop resolves the "loop" section even within Pipeline; its per-Run shallow
instance copy preserves subclass methods and shared SDK handles while replacing
Loop-owned configuration. All execution state stays local. BaseEngine authors
choose a section name and read settings explicitly. RunManager reloads the Project
before a Run, so later edits affect future Runs only.

COMPLETION EngineEvents carry CompletionResult snapshots: a stable call ID,
Step ID, model/response ID, finish reason, status, timestamps, duration, numeric
usage and usage_complete. BaseEngine.stream_completion yields these observations
alongside strings; BaseEngine.execute attaches its Step ID and forwards them.
Text consumers may opt out with include_events=False. Custom Engine-protocol code
must emit observations itself for detailed LLM accounting. Non-LLM/non-reporting
engines still receive a terminal Run summary with unknown usage.

The helper requests include_usage by default and retains only numeric usage data,
including nested token details. Observations update on identity, finish reason or
usage changes and completion/failure, not every text delta. RunManager persists
snapshots in Run metadata by call ID before notifying observers. Cancellation and
generator close never yield final events; the last durable snapshot is retained.
A provider may omit usage; unknown counts remain None, and interrupted streams do
not claim a complete aggregate. Observations are not billing guarantees.

Run metadata is the sole persisted source for completion observations. RunRepository
finalizes pending/running observations when saving terminal Runs, including crash
recovery. ExecutionResult.from_run builds a detached query view and normalizes old
terminal Run snapshots in memory without rewriting them on read. No Project-level
result files or persisted aggregates are created.

RunResultQuery resolves a Project or Task scope under workspace ownership and reads
RunRepository. ProjectManager, TaskManager and RunManager expose it as results;
RunManager supplies its configured Run repository. Custom query repositories can
be injected through the RunReader protocol. Task scope avoids a Project-wide scan.
List defaults to terminal Runs in active Tasks; include_deleted/include_running
make wider queries explicit. Direct loads allow soft-deleted history but verify
owner scope. Permanent Task deletion removes its Runs and results; clones contain
no Run history. Old Project state/executions JSON is ignored and never a fallback
for missing/deleted Runs. It is left untouched rather than deleting historical data
automatically. Existing Run observations already contain the source records.

## Reusable Model Inference

ish/inference is a runtime model-operation domain. EmbeddingModel.embed and
RerankModel.rerank can be called by Engines, Tools, preparation callbacks or future
RAG components. They are not execution strategies and have no execute(), registry,
Run ownership or lifecycle persistence. The primary hierarchy is unchanged.

Each client snapshots open-ended LiteLLM kwargs and invokes aembedding/arerank.
Call arguments override constructor defaults; request containers are copied while
live client/callback identities remain intact. Shared defaults do not hold per-call
state. The shared private ModelClient supplies lazy off-thread SDK import and async
dispatch. Only providers.parameters is shared with BaseEngine; inference imports
neither Engines, services nor core domains. Model results and exceptions retain
SDK types; cancellation propagates, subject to provider cleanup behavior.

Model defaults can live in ProjectConfig.data.inference and Task data overrides,
read through context.settings(name). Callers own mapping configuration into clients,
inputs, output/index persistence and observability. Within a Run, self.step or
PreparationStep can record embedding/rerank lifecycle; Tool handlers may call the
same clients. No automatic model-call usage recording is introduced outside the
existing COMPLETION event contract. Inference responses include native metadata;
callers decide which numeric observations to emit. Rerank billing units must not be
silently summed as completion tokens. No vector database, RAG CRUD, model download,
embedding Engine or rerank Engine is added.

## Beginner Engine Authoring

engines/base.py defines BaseEngine alongside the existing Engine protocol,
context, events and registry. It is re-exported from ish.engines. Override
run(context) or supply action=: return an async text iterator, yield strings from
an async generator, forward COMPLETION observations, or perform an async operation
returning None. The inherited
execute() wraps the operation in one Step and maps text to TEXT_DELTA. Engines
with several Steps override execute() and use self.step(context, action, name=...)
for each operation. Consumers forwarding these streams use compat.aclosing.

BaseEngine retains the optional whole-Step timeout, JSON-safe copied metadata,
failure events with exception details, and iterator cleanup. Cancellation/GeneratorExit propagate
without yielding while closing; RunManager finalizes interrupted active Steps.
Cleanup errors do not replace cancellation/close; normal cleanup failures fail the
Step. Execution/assembly state stays local, so one instance can serve several Runs.
Developer actions and lifecycle labels remain trusted code/data.

BaseEngine.stream_completion(request, response=None) calls the existing bounded
thread transport in providers/litellm.py, whose worker invokes litellm.completion.
The method enforces stream=True and one choice, reads dict/SDK chunks, yields text,
assembles indexed tool-call fragments in ordinary dictionaries, and validates
termination, duplicate IDs and size limits. An optional fresh response dictionary
receives a completion-format assistant message only after success; it remains
unchanged on failure/cancellation. The method emits COMPLETION observations alongside strings, but no Step
lifecycle events and no application deadline. Use run()/step() for lifecycle and deadline handling.
It handles text/tool completion streams and numeric usage observations;
reasoning/multimodal content is not surfaced. It executes no tools and performs no domain persistence.

copy_params() retains live SDK clients/callbacks while copying builtin containers.
Per-call transcripts are isolated even when a cancelled provider thread is still
finishing its network read. Base imports the lightweight transport only; LiteLLM
is lazily imported in the worker. Non-LLM actions do not invoke/import the SDK.
BaseEngine remains completion-specific. Reusable embedding/rerank operations
live in inference and do not inherit it.

LoopEngine is the only class in engines/loop.py and inherits BaseEngine. Its
responsibility is Project/history/prompt configuration, batch validation before
side effects, tool execution and bounded iteration. Each completion/tool uses
self.step(), and every completion uses the inherited stream_completion().
PreparationStep also inherits BaseEngine; PipelineEngine composition is unchanged.
StepManager/StepEventRecorder still persist lifecycle events, and RunManager still
owns queues, Runs, streaming conversation writes, cancellation and recovery.

This is a public API rename: engines/step.py and engines/_completion.py were removed.
Use BaseEngine instead of StepEngine, and direct LoopEngine constructor limits
instead of LoopOptions/options=. LoopEngineError/_Turn/_ToolCall are removed;
validation uses ValueError/TypeError and wrapped execution retains contextual
RuntimeError. LLM Step errors stay 'LLM iteration failed' and tool errors stay
'Tool execution failed'. The Engine protocol remains usable without inheritance.
The offline examples/custom_engine.py demonstrates minimal authoring/registration;
README also shows a minimal inherited LiteLLM completion engine.

## Developer Engine Composition and Completion Parameters

LoopEngine remains specifically coupled to LiteLLM completion and its chat/tool
stream format. No cross-library model abstraction, embedding, or rerank operation
is introduced. providers/litellm.py is the existing thread/stream transport; its
worker directly calls litellm.completion so synchronous network reads do not block
the event loop.

LoopEngine directly accepts max_iterations, request_timeout, tool_timeout,
buffer_size, max_tool_calls, max_argument_chars and max_output_chars. LiteLLM
options such as max_tokens belong in the open-ended completion_kwargs mapping. A synchronous context factory may supply that mapping.
Project config supplies defaults; explicit kwargs override them. Builtin option
containers are copied once per Loop execution and again per provider request,
while SDK clients/callbacks retain identity. These objects are runtime configuration,
not persisted JSON. Runtime api_key or SDK environment authentication is used.
No parameter allowlist attempts to mirror every LiteLLM release. Loop transcript
and tool-registry ownership remain reserved (messages/tools/functions/function_call),
and stream=True/n=1 are required. All other options are passed to LiteLLM; newer
response formats may still require Engine changes. Provider timeout is independent
of the application per-round deadline. system_prompt is a string or synchronous
context factory, prepended once to the in-memory provider transcript.

EngineContext.state is a fresh dictionary per Run for preparation outputs and
runtime handles. RunManager's context construction creates it; neither core
models nor repositories acquire this field. Do not copy these outputs to event
metadata by default, because they may contain documents or runtime-only values.

engines/pipeline.py supplies PipelineEngine(stages) and PreparationStep(name,
action, kind=..., timeout_seconds=...). PreparationStep emits lifecycle events
around an async action(context), with a 60-second default deadline (None disables
it). Pipeline passes one context through its sequential stages, including nested
pipelines. A failure, cancellation event, or unfinished Step prevents the next
stage. Iterator cleanup is propagated; RunManager still owns Run cancellation,
StepEventRecorder persistence, and queue/recovery semantics. Pipeline creates no
extra Run. Actual cancellation propagates so RunManager records INTERRUPTED;
non-success terminal events without cancellation fail the pipeline.

Preparation runs after Run creation, before the Loop. Document/RAG/environment
work delegates to developer callbacks and component services. Per-Run values use
context.state and can be consumed by Loop parameter/prompt factories. Callbacks
must cooperate with cancellation and offload blocking work; arbitrary side effects
cannot be rolled back. Never mutate process-global environment for one Task.
Use a Run-local env dictionary for subprocesses. Existing stale Runs, including
interrupted preparation, are never replayed automatically.

## LiteLLM Loop Execution

LoopEngine calls synchronous `litellm.completion(stream=True, ...)` through
`providers/litellm.py`. Each request has a dedicated daemon thread that
creates, reads, and closes its provider stream. A bounded queue bridges chunks
to the async event loop with backpressure. Cancellation stops delivery at once;
Python cannot forcibly interrupt an in-progress synchronous network read, so
the worker closes the stream after the read returns or its provider timeout
expires. These cleanup threads never execute tools or touch persistence.

The Engine resolves Project/Task configuration, emits an LLM Step,
and forwards each `delta.content` as TEXT_DELTA. Indexed `delta.tool_calls`
fragments are accumulated into complete call IDs, function names, and JSON
arguments. Only registered tools are callable. The entire batch is validated
against JSON Schema before any tool executes, and calls are executed serially
as individual tool Steps. Their observations are added to the next LLM request.

A normal `stop` response ends the Run. A `tool_calls` response starts another
round when budget remains. Missing/abnormal finish reasons, malformed tools,
tool failures, timeouts, and exhausted iteration budgets fail the Run. No
engine retries are performed. LiteLLM defaults to `num_retries=0`; developers may
override SDK request retries through completion_kwargs without enabling tool or
stale-Run replay. Calls on the
last permitted iteration are not executed without a follow-up LLM round.
Defaults are eight LLM rounds, 60 seconds per whole LLM round, and 30 seconds
per async tool. Tool handlers must propagate cancellation and avoid blocking.

The Engine still writes no files. RunManager and StepEventRecorder persist its
events through the existing services. RunManager's optional synchronous
`on_event(run, event)` observer receives snapshots after persistence, enabling
immediate display. RunEventPublisher isolates observer exceptions, recording a
operational `observer.failed` event without failing the Run. Callbacks must remain
synchronous and nonblocking. Engine/storage exceptions retain their execution
failure semantics; display errors are not execution errors.

All visible text in a Run accumulates in its existing single Assistant Message.
The structured assistant tool calls and tool results are currently a transient
per-Run transcript. Only Step identity, iteration, call ID, status, and safe
failure descriptions are persisted for tools. Raw arguments/results and
provider response objects are not copied into Step metadata. Future support
for a durable structured tool transcript must go through ConversationStore;
stale Runs remain interrupted and are never automatically resumed.

Authentication is delegated to the SDK environment or runtime completion_kwargs.
There is no application credential service, reserved secret directory, key-name
blocking or error-message masking. Runtime objects are rejected by JSON validation.

Project components inherit `Component` from `ish/components/base.py`, declaring a
name and a direct-child directory. The base owns safe directory creation/removal,
open JSON codecs, configuration, definition CRUD and cloning. ComponentRegistry
validates identity and directory ownership; its declared-capability resolution mechanism
is generic and has no Tool dependency. Component paths are not in ProjectPaths.
Core Task initialization remains mandatory, independent of optional selection.

Default layout is `<project>/<directory>/component.json` plus `records/<id>.json`.
All mutable JSON uses atomic replacement. Unknown keys survive round trips;
runtime objects, non-string keys and non-finite numbers are rejected.
Component-specific validation/codecs can evolve without a central fixed dataclass.
ToolComponent stores enabled names and optional native function-tool definitions;
SubagentComponent stores open specialized model/settings/prompt definitions;
WorkflowComponent stores open graph data. Graph semantics, node/reference validation
and subagent execution belong to developer execution code, not this storage base.

ProjectManager.create/set_components coordinate idempotent initialization.
ComponentData returned by projects.component reloads Project state and selected
components under workspace ownership for each CRUD/configuration call. Updates
hold the lock through read/modify/write. Direct lower-level component access needs
an explicit ownership scope; async UIs use StorageIO for synchronous APIs.

Disabling retains data. ProjectManager.remove_component(permanent=True) requires
inactive/detached Tasks, publishes disabled selection, then delegates recursive
removal to the component. Failed removal stays disabled and may be retried. Linked
ancestors/entries and escaping paths are rejected. Core-owned tasks/logs/state/cache
and duplicate component directories cannot be claimed. Developer components are
trusted code, not a sandbox. Removal and initialization are not cross-file atomic.
Failed create/clone is soft-deleted; failed additions leave old selection intact
but may leave partial directories. Cloning copies JSON config/records, not artifacts.

ToolComponent declares tools in its capabilities tuple and resolve(project, "tools")
returns a ToolRegistry. Other capability values are not created by that call. The separate
ComponentToolResolver adapts those resolved values; RunManager wraps ComponentRegistry
passed as capabilities for existing callers. Custom resolve_tools adapters remain
supported. Each Run gets a fresh fixed tool snapshot, using persisted overrides or
legacy catalog definitions with application-registered handlers. Unknown handlers
fail during runtime binding, not data CRUD/configuration/clone; saved names never
import code, and deleting an enabled override is
rejected. Provider extension keys survive into the completion tools argument.

Existing tools/component.json works without migration. Legacy directory-only
workflows read/clone with empty configuration without source writes; arbitrary old
files remain untouched. Re-enabling creates missing common storage. SingleEngine,
GraphEngine, RAG/MCP/Skill adapters and structured tool transcripts remain planned.
See `ish/components/README.md` for authoring and CRUD examples.
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

ProjectManager does not know how Memory, Workflow, Tools, or Tasks internally structure their directories.

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

Operational logs follow the lifecycle field schema.

The current implementation uses standard-library `logging.Logger` and
`RotatingFileHandler` to append structured JSON to each domain's
`logs/service.log` (1 MiB with three backups). Project/Task/Run/Step repositories
log persistence operations; managers log lifecycle and runtime operations.
Conversation mutations log event type and identity under Task, never text.
Completion observations use RunRepository and its existing Run logs.
Result queries create no separate persistence or result-log hierarchy.

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
* MemoryManager
* WorkflowManager
* centralized log export and durable audit requirements if needed
* prompt-toolkit UI
* additional live-provider and Linux integration tests
