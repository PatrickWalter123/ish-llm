# Development Handoff

## Goal

Continue development of a production-oriented Linux TUI AI client built with Python, asyncio, LiteLLM, and prompt-toolkit.

Read `AGENTS.md` and `docs/architecture.md` before making architectural changes.

## Latest: generic Project component storage

* Component now declares name/directory and implements JSON codecs, configuration,
  named record CRUD, safe directory initialization/removal and definition cloning.
  Registry no longer imports tools or requires resolve_tools; optional runtime
  exports are generic. ComponentToolResolver is the Tool-specific adapter, and
  RunManager still accepts capabilities=components and custom tool resolvers.
* Tools, Workflows and Subagents inherit the base. tools/component.json retains
  enabled names and extra config; optional records/<tool-name>.json stores native
  function definitions with extension keys, validated against registered handlers.
  Subagents and Workflows store open dict model settings/graph documents. Neither
  class executes nodes, validates a fixed graph format, or adds another Run layer.
* projects.component(project, name) returns ComponentData for locked, lifecycle-
  checked CRUD. create/set_components initialize declared directories. Disable
  retains data; remove_component(permanent=True) requires detached Tasks and
  disables before safe tree removal. Failure can leave disabled partial data.
* Base clones configuration and records only, retaining IDs for graph references.
  Old tools config and empty workflow roots remain readable; legacy graph artifacts
  are retained without implicit import. Python custom components must now declare
  directory and inherit the base or implement the expanded ProjectComponent API.
* Tests cover open-key round trips, update/replace/delete, atomic-write failure,
  cloning, lifecycle/ownership, registry/path rejection, Windows junctions, legacy
  workflows, concurrent locked updates, generic export/import boundaries and
  persisted tool definitions reaching LoopEngine with trusted handler execution.

Final validation: 191 tests passed on Python 3.9.13 (128.472 seconds) and
Python 3.13.7 (116.750 seconds). Both compile checks and the offline custom Engine
example passed. Full output: test-results-python39.txt / test-results-python313.txt.
No live provider calls were made; the existing actual-SDK/mock-SSE tests passed.

## Previous: Run-owned results and reusable model inference

* Removed ProjectExecutionStore and Project summary writes. CompletionResult stays
  in Run metadata; RunRepository finalizes interrupted observations on terminal
  saves/recovery. ExecutionResult is now an in-memory view of one Run.
* RunResultQuery provides Project/Task load/list via projects.results, tasks.results
  and manager.results. Queries hold workspace ownership, support deleted/running
  filters and injectable Run readers. Permanent Task deletion removes Run history;
  cloning does not copy it. Old Project state/executions files are ignored and left
  untouched, never used to resurrect missing Runs.
* Added inference/EmbeddingModel and RerankModel calling LiteLLM aembedding/arerank.
  Open kwargs, SDK response types, errors and cancellation are preserved. Inputs and
  option containers are isolated while SDK handles retain identity. No Run/Step
  creation, engine registration, file writes or automatic usage events occur here.
* Shared request-container copying lives in providers/parameters.py; BaseEngine's
  public helper delegates there. Inference does not import Engines/services/core
  or eagerly import the SDK. ProjectConfig.data.inference can hold optional model
  defaults, with Task overrides; RAG/index lifecycle remains with components.
* Tests cover Run-only persistence, Task/Project query equivalence and filtering,
  legacy summary isolation, recovery, deletion/clone policy, provider dispatch,
  model argument forwarding, concurrency, cancellation and import boundaries.

Final validation: all 176 tests passed on Python 3.9.13 (121.891 seconds) and
Python 3.13.7 (137.201 seconds). Compile checks and the offline custom Engine
example passed on both. Completion SDK/mock-SSE coverage remains; inference
dispatch uses installed SDK entry points with mocked async calls, not live model
requests. Full outputs: test-results-python39.txt and test-results-python313.txt.

## Previous: flexible configuration and Project execution results

* ProjectConfig now holds default_engine/completion/engines/task_defaults/data.
  Task.config is persisted, saved, cloned and exposed through EngineContext.settings.
  Loop resolves Project -> Task -> constructor overrides on a per-Run instance.
  Old stored flat provider fields migrate on load; Python callers use the new API.
* CompletionResult/ExecutionResult live in core/results.py. COMPLETION events carry
  stable per-call observations. BaseEngine forwards observations alongside text;
  include_events=False is a standalone text-only opt-out. Raw provider payloads,
  prompts, headers and credentials are not copied into result files.
* RunManager records call snapshots in Run metadata then writes atomic Project
  state/executions/<run-id>.json summaries via ProjectExecutionStore. ProjectManager
  exposes results.load/list. Every terminal Run gets a summary, including fake,
  preparation-only, failed and interrupted Runs. Recovery rebuilds missing summaries
  and interrupts stale observations without replay. Incomplete usage remains unknown.
* Removed the application credential service and its paths/CLI argument/imports.
  SDK environment or runtime-only completion_kwargs handles authentication. Updated
  obsolete documentation/instructions and maintained credential-free JSON/logs.
* Added regression coverage for configuration persistence/precedence/isolation,
  migration, summary aggregation, usage-only chunks, failed/partial completions,
  recovery, clone/delete policy and payload exclusion. Updated old event-count
  assumptions and TaskManager.save to persist the new configuration.

Final validation: all 165 tests passed on Python 3.9.13 (108.792 seconds) and
Python 3.13.7 (84.603 seconds). Additional actual-SDK mock-SSE assertions verified
usage-only chunks, total_tokens=10 across two calls, and finish reasons
tool_calls/stop on both versions. Result model type hints resolve on both.
Compilation, the offline custom Engine example and demo help also passed.
No live provider calls were made. Full outputs: test-results-python39.txt and
test-results-python313.txt; extra SDK checks: sdk-results-python39.txt and
sdk-results-python313.txt.

## Previous: BaseEngine and inherited LiteLLM Loop (2026-09-08)

* Renamed StepEngine to BaseEngine and moved it into engines/base.py. Removed
  engines/step.py and private engines/_completion.py. Public exports, preparation,
  examples, demo, manual API example and automated tests use the new API.
* BaseEngine centralizes Step events/cleanup and reusable stream_completion():
  dict/SDK chunk text, fragmented tool calls, bounded output and finish validation.
  A per-call response dict receives an assistant message after successful assembly.
  Request containers are copied while live SDK handles keep identity.
* Rebuilt loop.py as one LoopEngine(BaseEngine) class. It uses inherited step()
  and stream_completion(). LoopOptions/options=, LoopEngineError, _Turn and
  _ToolCall are removed. All application limits are explicit constructor kwargs;
  unrestricted provider options continue to use completion_kwargs.
* Project -> Task -> Run -> Engine -> Step ownership and persistence are unchanged.
  No tool execution in BaseEngine and no automatic stale-Run retry were introduced.
* Added coverage for minimal inherited completion with persistent Steps, SDK/dict
  chunks, interleaved tool assembly, size/finish errors, simultaneous stream state,
  SDK client identity, request copying and early close. Updated Python 3.9 type-hint
  checks and direct Loop limit validation. No live provider calls are required.

Final validation: all 157 tests passed on Python 3.9.13 (79.259 seconds) and
Python 3.13.7 (92.817 seconds). Compilation and examples/custom_engine.py passed
on both. Outputs: test-results-python39.txt and test-results-python313.txt.
The actual LiteLLM SDK was exercised with mock SSE, without live provider calls.

## Previous: common StepEngine authoring API

* Added engines/step.py::StepEngine, publicly available from ish.engines together
  with EngineContext/EngineRegistry. Developers implement run(context) or pass
  action=, yielding text or awaiting a no-output operation. Step IDs, lifecycle
  events, safe failures, optional timeout and iterator cleanup are centralized.
* LoopEngine completion/tool stages and PreparationStep use the same helper.
  Loop-specific options/parser helpers moved to private engines/_completion.py;
  imports of LoopOptions/LoopEngineError from ish.engines.loop remain valid.
* One StepEngine call creates one Step inside the existing Run. Pipeline composes
  multiple stages. No changes to persistence, managers, OS ownership, or providers.
  LLM step failures now use the generic safe 'LLM iteration failed' message.
* Added examples/custom_engine.py (offline Echo Engine with full service setup),
  a README authoring guide, and regression tests for subclass/callback actions,
  persistent events, metadata/state isolation, failure/cancellation/timeout,
  generic iterators, invalid output, and early-close/cleanup failures.

Final validation: all 151 tests passed on Python 3.9.13 (102.761 seconds) and
Python 3.13.7 (117.204 seconds). Compilation of ish/tests/examples and the offline
custom_engine.py example passed on both. Full outputs: test-results-python39.txt
and test-results-python313.txt. No live provider requests were made.

## Previous: open LiteLLM parameters and preparation pipelines

* LoopEngine stays LiteLLM-specific; completion_kwargs accepts a mapping or sync
  context factory. Project values are defaults. SDK client/callback identities are
  retained; option containers are copied per execution/request. Unknown LiteLLM
  options pass through, while Loop messages/tools/stream/single-choice ownership
  remains enforced. system_prompt accepts a string or sync context factory.
* LoopOptions now contains application limits only. max_tokens moved to
  completion_kwargs. Provider timeout/retries can be set there independently of
  the application deadline; no automatic tool/Run replay is introduced.
* PipelineEngine composes stages inside one Run. PreparationStep wraps an async
  callback with observable Step events and timeout. EngineContext.state holds
  Run-local outputs. Preparation failure/cancellation stops later stages; queues,
  persistence and recovery stay with the existing managers.
* No embedding/rerank or generic provider abstraction was added. No RAG or shell
  execution implementation is claimed; developers supply callbacks/components.
* README has runnable API patterns, including document preparation and prompt
  factories. tests/test_pipeline.py covers order, state, failure, cancellation,
  timeouts, concurrency, nested stages and iterator closing. The actual LiteLLM
  mock-SSE test now passes a live client and completion options through kwargs.

Final validation: all 138 tests passed on Python 3.9.13 (92.398 seconds) and
Python 3.13.7 (75.395 seconds). Compile checks and demo help passed on both.
Full outputs: test-results-python39.txt and test-results-python313.txt.
No live provider requests were made.

## Previous: service module consolidation (2026-09-07)

* Renamed conversation_context.py to context.py, retaining ConversationContextBuilder.
* Merged io.py (StorageIO/drain_on_cancel) and deletion.py (remove_owned_tree)
  into storage.py. Sections distinguish metadata primitives, deletion, and async I/O.
* Moved RunEventPublisher from events.py into runs.py as a separate class.
* Updated implementation/test imports, patch targets, and README examples. Removed
  the old modules; there are 11 functional modules plus services/__init__.py.
* Kept access.py to avoid a ProjectManager/TaskManager dependency cycle, and kept
  locking/logging/secrets separate because they have distinct shared responsibilities.
* AST comparison verified that all 10 moved/existing storage/context/publisher
  definitions retain identical implementations. Execution and persistence formats
  are unchanged; use the new import paths documented in architecture.md.

Final validation: 125 tests passed on Python 3.9.13 (62.723 seconds) and Python
3.13.7 (74.111 seconds). Compile checks and fresh imports passed on both versions.
Outputs are test-results-python39.txt and test-results-python313.txt.

## Previous: workspace locking and storage I/O (2026-09-07)

* WorkspaceOwnership uses stable `<projects-root>/.ish.lock` with nonblocking OS
  locks (Windows byte-range/POSIX flock). Lifecycle transactions are guarded;
  runtime leases span recovery through shutdown/drain, including idle workers.
  Process death releases the lock without PID timeouts or deleting the file.
* Share one repository/service container per workspace. Attached Task IDs are
  coordinated across its TaskManagers; other repository instances fail fast with
  WorkspaceBusyError. This allows one owning process, not distributed workers.
* StorageIO offloads ordered persistence/context work with one in-flight thread
  operation per RunManager. Cancellation drains writes. Accepted submit/start/
  shutdown calls finish before forwarding cancellation. Run preparation supports
  interrupt before the Engine child exists.
* ConversationStore caches incremental replay, detects replacement/truncation,
  protects its projection with an instance lock, and returns detached snapshots.
  RunManager reuses one store per attached Task. Per-delta operational logs and
  repeated directory sync/tail checks are removed; every delta still fsyncs.
* JSON/JSONL domain formats and hierarchy remain unchanged. The infrastructure
  lock file lives outside Project trees. Lower-level writes bypassing managers
  need an ownership scope. Async UIs can use StorageIO for synchronous CRUD.
  Custom synchronous storage/context/capability adapters run on worker threads.
* All 125 tests passed: Python 3.9.13 in 90.655 seconds, Python 3.13.7 in 86.721
  seconds. Full output: test-results-python39.txt / test-results-python313.txt.
  Fourteen new tests cover actual subprocess conflicts/crash release, shared
  attachment, incremental decode work, cache invalidation, fsync failure, loop
  responsiveness, cancellation drain, accepted input and shutdown ordering.

Older sections describe historical milestones. Tool permissions, structured tool
history, Linux/network-filesystem validation, live-provider testing, backup,
migration and production-load verification remain outstanding.

## First Task Progress (2026-09-06)

The workspace initially contained only `AGENTS.md`, this handoff, and the
architecture document, with no implementation, tests, or Git metadata.
The Suggested First Codex Task below has now been implemented as a foundation:

* persistent domain models and major paths with the documented service boundaries
* atomic JSON metadata and durable append-only conversation events
* deterministic FakeStreamingEngine with chunks, delay/gate controls, failure injection, and cancellation handling
* RunManager durable queues, per-Task serial execution, cross-Task concurrency, streaming, interrupt, graceful shutdown, and restart recovery
* StepManager and StepEventRecorder with terminal-state validation
* basic Project/Task lifecycle, soft-delete/restore, and safe conversation snapshot cloning
* standard-library integration tests, including abrupt subprocess termination and restart

Regression tests also cover cancellation before the Engine starts, burst queue
context ordering, the crash gap between Run creation and input commitment,
cloned conversation order, generic async iterator Engines, and shutdown while
a caller waits for the queue to drain. The architecture document describes the
implemented scope and its single-process ownership contract. `README.md`
contains a runnable usage example.

Run all tests with:

```sh
python -m unittest discover -s tests -v
```

## LiteLLM LoopEngine Progress (2026-09-06)

The user explicitly requested LoopEngine next, using synchronous
`litellm.completion(stream=True, ...)`. That request supersedes the original
SingleEngine-first sequencing below.

Implemented:

* LoopEngine: streaming content, fragmented tool calls, registered async tools, and bounded iteration
* dedicated stream thread and bounded async bridge, with late-delta suppression on cancellation
* per-request/tool timeouts, no automatic retries, and validation before tool batches execute
* provider configuration (superseded by the extensible settings API above)
* RunManager `on_event` callback after persistence, and `python -m ish.demo` for streaming output
* regression tests for real-time delivery, tools, failure, cancellation, timeouts, and cross-Task concurrency
* an installed LiteLLM SDK integration test with mock SSE transport; no live model request

Install dependencies with `python -m pip install -e .`, then run the full suite
using `python -m unittest discover -s tests -v`. The implementation was checked
with LiteLLM 1.100.0. See README for usage and the architecture document for
thread cleanup and transient tool-transcript limitations.

Validation: all 56 tests passed on Python 3.13.7 / Windows, including the real
SDK with mock SSE transport. Compilation and `pip check` also passed. No live
model API was called; Linux execution has not been verified in this workspace.

Next work: durable structured tool messages through ConversationStore,
provider-specific compatibility tests, and TUI integration. A dedicated
SingleEngine and GraphEngine remain unimplemented. The broader original
milestone below therefore remains partially outstanding.

## Repository Consistency Refactor

RunManager now lives alongside RunRepository in `ish/services/runs.py`.
TaskRuntime is defined in `ish/services/tasks.py`, with scheduling still owned
by RunManager. The former `run_manager.py` file was removed, and source,
test, demo, and README imports use `from ish.services.runs import RunManager`.

TaskRepository and StepRepository were added to their respective service modules.
TaskManager and StepManager delegate JSON persistence, path resolution, and
listing to these repositories. Lifecycle behavior remains in the managers;
TaskManager's active-Run guard reloads through TaskRepository. All four metadata
domains now have a Repository/Manager pair. ConversationStore keeps its
append-only Message event format, and Engine remains persistence-independent.

TaskManager/StepManager accept optional `repository=` injection. RunManager also
exposes `repository=`, while retaining `runs=` and `.runs` for existing callers.
The persisted formats, paths, and runtime/recovery behavior are unchanged.
Additional tests cover repository injection without metadata files, ownership
checks, round trips, task filtering, and duplicate Step IDs.

## Current Architecture

Service boundary/component update (2026-09-07):

* ProjectAccess is shared by ProjectManager/TaskManager/RunManager through service composition. Current persisted Project state is checked at mutation and execution entry points. Public save cannot resurrect deleted records or overwrite managed Task runtime state.
* TaskManager must be bound by ProjectManager or receive `project_access=`. RunManager still owns runtime scheduling; TaskRuntime remains in tasks.py.
* RunEventPublisher isolates UI observer failures and logs them safely; callbacks are still synchronous/nonblocking.
* ConversationContextBuilder handles Run and clone turn ordering. Both managers accept an injectable conversation factory; RunManager defaults to TaskManager's factory/builder.
* Project has persisted component names (old metadata defaults to none). ProjectManager.create accepts selected names and delegates directory creation to registered components.
* ProjectPaths no longer exposes tools/workflows. ToolPaths and WorkflowPaths own their respective directories.
* ToolComponent persists enabled tool names; WorkflowComponent initializes a directory only. RunManager accepts `capabilities=components` and injects fresh Project-scoped tools per Run. LoopEngine no longer accepts global `tools=`.
* Save Project configuration before execution; stale caller configuration is no longer the runtime source of truth. Task public edits require a detached runtime.
* Register implementations/handlers again on startup; persisted names do not dynamically load code. Component initialization is idempotent and failures can leave retained partial directories; see architecture/README for failure and clone policies.

Final verification for this update: all 111 tests passed on Python 3.9.13
(93.263 seconds) and Python 3.13.7 (93.552 seconds). This includes the previous
91 regression cases plus 20 service-boundary/component cases. Both SDK/mock SSE
tests passed without a live provider call. Full outputs are in
`test-results-python39.txt` and `test-results-python313.txt`.

Python 3.9 compatibility update (2026-09-06):

* User-provided `D:\Program Files\Python39\python.exe` is Python 3.9.13. It was used to create `.venv39`, separately from the existing Python 3.13 `.venv`.
* Supported Python floor changed to 3.9. `ish.compat` handles dataclass slots, string enums, async timeout/closing, and junction detection. Optional annotations and Step dispatch use 3.9-compatible forms.
* Python 3.9 installs pinned LiteLLM 1.80.17/jsonschema 4.25.1; Python 3.10+ keeps newer dependency ranges. `constraints-python39.txt` captures 59 tested runtime dependency versions.
* Python 3.9 uses regular dataclasses; 3.10+ retains slots. Declared fields, frozen behavior, enum values, and JSON/JSONL persistence are unchanged.
* All 91 tests passed on Python 3.9.13 (59.162 seconds) and Python 3.13.7 (53.116 seconds). Both use actual LiteLLM SDKs with mock SSE. New tests cover type-hint resolution, enum formatting, timeout/cancellation, async cleanup, and real Windows junction deletion rejection.
* Python 3.9.25 and Linux were not directly run. No live model API was called. Production ownership, I/O performance, tool policy/history limitations remain outstanding.

Latest organization/lifecycle changes:

* Removed `ish/engines/fake.py`; deterministic tests use `tests/support/fake_engine.py`.
* Moved Tool/ToolRegistry to `ish/components/tools` and provider transport to `ish/providers/litellm.py`.
* Reserved `components/rag`, `mcp`, `skills`, `subagents`, and `workflows` for future CRUD/adapters; no CRUD implementations are claimed for these packages.
* Categorized comments in core models; new ProjectConfig defaults to `loop`.
* Renamed Project/Task `soft_delete` to `delete(..., permanent=False)`, with guarded permanent removal and parent-scope deletion logging.
* Shared TaskManager tracks runtime attachment; shut down RunManager before lifecycle deletion/cloning. This is not a cross-process lock.
* Services write safe structured, rotating `logs/service.log` files in Project/Task/Run/Step scopes using standard-library logging.
* Added regression coverage for permanent deletion, stale restore, ownership and linked-path rejection, runtime attachment, safe log content, rotation, and logging failures.

Persisted formats are unchanged. Explicit old `fake` engine selections must be
updated by the caller for real execution. Production limitations and next steps
are recorded in `docs/production-readiness.md`.

Final validation: 81 tests passed in 47.356 seconds on Windows / Python 3.13.7.
Compilation, `pip check`, and demo help/import checks passed. No live model API
was used; Linux and production-load verification remain outstanding.

The intended hierarchy is:

Project
→ Task
→ Run
→ Engine
→ Step

Conversation belongs to Task.

Persistent state and runtime state are intentionally separated.

## Implemented or Designed Components

### Core

* Project
* ProjectPaths
* Task
* TaskPaths
* TaskStatus
* Message
* MessageRole
* MessageStatus
* Run
* RunPaths
* RunStatus
* Step
* StepPaths
* StepStatus
* Engine protocol
* EngineContext
* EngineEvent
* EngineEventType
* EngineRegistry

### Services

* ProjectManager
* TaskManager
* ConversationStore
* RunManager
* StepManager
* StepEventRecorder

### Persistence Behavior

Project, Task, Run, and Step metadata use JSON files with atomic replacement.

Conversation history uses append-only JSONL events.

Conversation events currently include concepts equivalent to:

* message.create
* message.delta
* message.status
* message.metadata
* message.run

## Execution Flow

User input follows this path:

TUI
→ RunManager.submit()
→ ConversationStore persists Message as QUEUED
→ TaskRuntime asyncio.Queue receives message ID
→ Task worker selects queued Message
→ Run created
→ Message QUEUED → COMMITTED
→ Task binds current Run
→ EngineRegistry resolves Engine
→ Engine.execute()
→ EngineEvent stream

Text events:

EngineEvent(TEXT_DELTA)
→ ConversationStore appends Assistant delta
→ TUI receives streaming update

Step events:

EngineEvent(STEP_*)
→ StepEventRecorder
→ StepManager
→ persistent Step state

Run completion:

Assistant STREAMING → COMPLETED
Run RUNNING → COMPLETED
Task RUNNING → IDLE

## Concurrency Model

One Task has one serial Run worker.

Multiple Tasks may have independent workers and execute concurrently.

This is intentional.

Do not introduce concurrent Runs inside a single Task unless the architecture is explicitly redesigned.

GraphEngine may internally execute independent graph nodes concurrently, but those executions must remain Steps belonging to the same Run.

## Queue Semantics

Every incoming user request is first persisted as QUEUED.

If the Task is idle, the worker consumes it quickly.

If the Task already has a running Run, the request remains QUEUED.

When execution begins:

QUEUED → COMMITTED

The persisted conversation is the source of truth.

The asyncio.Queue is runtime acceleration only.

## Interrupt Semantics

Normal user input does not interrupt the current Run.

Explicit interrupt cancels only the active Run.

Queued requests are preserved.

Partial Assistant output becomes INTERRUPTED.

The Task worker remains alive and proceeds with the next queued request.

## Recovery Policy

On application restart:

* stale RUNNING/PENDING Run → INTERRUPTED
* stale RUNNING/PENDING Step → INTERRUPTED
* stale STREAMING Assistant Message → INTERRUPTED
* queued user messages → restored into runtime queue

Do not automatically replay stale Runs.

A stale Run may already have produced side effects.

## ProjectManager Boundary

ProjectManager is an orchestration service.

It delegates:

* persistence to ProjectRepository
* component initialization to initializer services
* migration to ProjectMigrator
* Task cleanup to TaskManager or TaskMaintenance

Do not move Memory, Workflow, Tool, or Task directory internals into ProjectManager.

## TaskManager Boundary

TaskManager owns Task lifecycle and Task-level directory structure.

It does not execute Runs or Engines.

## ConversationStore Boundary

ConversationStore owns conversation persistence.

Do not store conversation content in ProjectManager logs.

Do not rewrite the complete conversation file for every streaming chunk.

## RunManager Boundary

RunManager owns:

* runtime Task workers
* durable queue restoration
* Run lifecycle orchestration
* Engine execution
* interrupt handling
* Assistant streaming persistence

RunManager should remain independent of prompt-toolkit.

## StepManager Boundary

StepManager owns persistent Step lifecycle.

StepManager must not execute the action represented by a Step.

Actual work belongs to Engine/tool implementations.

## Immediate Recommended Work

### 1. Add tests before expanding Engine complexity

Implement tests covering:

* Task creation
* Message persistence
* queued input
* Run creation
* streaming Assistant response
* second request during active Run
* explicit interrupt
* stale Run recovery
* stale Step recovery
* concurrent independent Tasks

Use a deterministic fake Engine before using a live LLM API.

### 2. Implement a FakeStreamingEngine

Create a test Engine that:

* yields deterministic text chunks
* emits Step events
* can sleep between chunks
* can intentionally raise an error
* can react to cancellation

This should validate RunManager without external APIs.

### 3. Implement production SingleEngine

After the fake-engine tests are stable, implement SingleEngine using LiteLLM async streaming.

SingleEngine should:

* construct model messages from EngineContext
* use Project configuration
* use SDK environment or runtime-only credentials
* emit an LLM Step
* emit TEXT_DELTA events while streaming
* propagate cancellation correctly
* avoid storing provider response objects directly in Step metadata

### 4. Add graceful shutdown

On application shutdown:

* stop accepting new submissions
* preserve queued Messages
* interrupt active Runs safely
* flush persistence
* stop Task workers
* close provider resources where needed

### 5. Implement LoopEngine only after SingleEngine is stable

LoopEngine should use the same RunManager and StepManager infrastructure rather than creating a parallel execution architecture.

Likely Step sequence:

LLM reason
→ tool action
→ observation
→ LLM reason
→ tool action
→ observation

### 6. Implement GraphEngine after LoopEngine boundaries are proven

GraphEngine executes predefined workflow graphs.

Graph nodes should normally map to Steps.

Independent graph nodes may execute concurrently inside one Run.

## Suggested First Codex Task

Inspect the repository before changing code.

Then:

1. verify the current Project/Task/Message/Run/Step implementations against `AGENTS.md`
2. add a deterministic FakeStreamingEngine
3. add integration tests for RunManager queue, streaming, interrupt, and recovery
4. fix implementation defects revealed by those tests without collapsing the existing service boundaries
5. run the full test suite

Do not implement LoopEngine or GraphEngine during this first task unless explicitly requested.

## Important Constraints

Do not:

* put asyncio.Queue inside Task
* put asyncio.Task inside persisted domain objects
* store API keys in project.json
* log Authorization headers
* automatically retry stale Runs
* expose queued future user Messages to the currently running Engine context
* directly couple Engine implementations to prompt-toolkit
* let ProjectManager become a general application God class

## Definition of Done for the Next Milestone

The next milestone is complete when:

* fake-engine integration tests pass
* streaming works
* multiple Task workers can run concurrently
* one Task still executes Runs serially
* queued input survives restart
* interrupt preserves queued input
* stale Run and Step state recover safely
* SingleEngine works through LiteLLM
* no secret values are persisted in ordinary project configuration
