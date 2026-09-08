# ish

Durable execution foundation for a Python 3.9+ AI TUI client. Includes a
LiteLLM LoopEngine that streams text,
executes explicitly registered tools, and continues until a final answer.
There is no TUI yet.

## Install

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, activate with `.\.venv\Scripts\Activate.ps1`, or invoke
`.\.venv\Scripts\python.exe` directly.

For the installed Python 3.9.13 on this machine, use a separate environment:

```powershell
& 'D:\Program Files\Python39\python.exe' -m venv .venv39
.\.venv39\Scripts\python.exe -m pip install 'pip==25.3'
.\.venv39\Scripts\python.exe -m pip install -c constraints-python39.txt -e .
.\.venv39\Scripts\python.exe -m unittest discover -s tests -v
```

If `.venv39` already exists from this refactor, run its Python directly.
The global Python installation and existing `.venv` are not replaced.
`constraints-python39.txt` pins the 59 runtime dependency versions resolved and
tested on Windows/Python 3.9.13. Use those constraints only with Python 3.9;
platform-specific wheel availability on Linux has not been tested.

Python 3.9 installs LiteLLM 1.80.17 and jsonschema 4.25.1 through environment
markers in `pyproject.toml`; Python 3.10+ retains the existing newer dependency
ranges. These older releases explicitly support Python 3.9:
[LiteLLM package metadata](https://pypi.org/project/litellm/1.80.17/),
[jsonschema package metadata](https://pypi.org/project/jsonschema/4.25.1/).
Python 3.9/3.10 use the
[async-timeout 5.0.1 backport](https://pypi.org/project/async-timeout/5.0.1/).

`ish.compat` provides string enums, timeout/async closing, dataclass options,
and Windows junction detection without patching the standard library. Python
3.9 uses ordinary dataclasses with the same fields/defaults/frozen behavior;
native slots remain enabled on Python 3.10+. Persistence formats are unchanged.

## Run the tests

From the repository root:

```sh
python -m unittest discover -s tests -v
```

After installing dependencies, the suite uses temporary workspaces and no live
model API or credentials. Its SDK test replaces HTTP with in-memory SSE and
token accounting with a local test vocabulary. It covers
streaming, serial queues, concurrent Tasks, cancellation, engine failures,
shutdown, recovery after an abruptly terminated subprocess, and lifecycle
operations.

Latest verification: all 204 tests passed on both Python 3.9.13 (168.825 seconds,
LiteLLM 1.80.17) and Python 3.13.7 (152.722 seconds, LiteLLM 1.100.0). Both SDK
versions passed the actual SDK/mock SSE test. These results cover the installed
interpreters; Python 3.9.25 was not separately executed. The test outputs are
`test-results-python39.txt` and `test-results-python313.txt`.

## Run a real streaming request

Set your provider credential in the environment, then run the demo. The command
below assumes `OPENAI_API_KEY` is already set. Replace the model identifier with
one available to your provider account.

```sh
python -m ish.demo --model openai/gpt-4o-mini --prompt "Use add to calculate 12 + 30, then explain the result." --with-tools
```

Each invocation creates a Project and Task under `workspace/projects/`, prints
the Project path, and prints text deltas as they are persisted. `--with-tools`
registers only an example `add` function. Without it, the engine streams a single
answer unless the provider returns an unsupported tool call. Ctrl+C interrupts
active work through RunManager shutdown. `--api-base` selects an optional
compatible endpoint; `--temperature` defaults to omission so model defaults apply.

To attach the engine to your own services:

```python
from ish.engines.loop import LoopEngine

engines.register("loop", LoopEngine(
    max_iterations=8,
    completion_kwargs={"top_p": 0.9, "max_tokens": 4096},
    system_prompt="Answer accurately and concisely.",
))
# Select "loop" through ProjectConfig.default_engine, Task.default_engine,
# or await manager.submit(content, engine="loop").
```

ProjectConfig.completion stores JSON-compatible LiteLLM defaults, including
model, temperature, api_base and provider-specific options. Authentication uses
the provider SDK environment or runtime-only completion_kwargs. There is no application credential resolver or field-name blocking.

LoopEngine calls `litellm.completion` with `stream=True`; timeout and
`num_retries=0` are defaults that completion_kwargs can override. It forwards content deltas immediately, assembles indexed
tool-call fragments, validates the complete batch against registered JSON
schemas, executes async tool handlers serially, and includes their results in
the next completion. Register tools through `ish.components.tools.ToolRegistry`;
handlers receive an argument dictionary and return text or JSON-compatible data.
Handlers must avoid blocking the event loop and propagate cancellation.

Each LLM iteration and tool execution emits Step events. A `stop` finish reason
ends the Run. Truncated streams, provider/tool errors, invalid arguments, and
exhausted iteration limits fail the Run without automatic retries. Tools from a
last iteration are not executed when no follow-up completion can be made.
Output and tool arguments are bounded by LoopEngine constructor limits, and a bounded stream
bridge applies backpressure. The default total timeout per LLM round is 60s;
each tool has a 30s timeout.

`completion_kwargs` is a mapping of LiteLLM completion arguments or a synchronous
`factory(context) -> mapping`. Values override Project model/temperature/API base
and default provider options. New provider-specific parameters pass through
without a new dataclass field; unsupported values are reported by LiteLLM.
Runtime SDK clients and callbacks are supported and retained by reference. Plain
dict/list/tuple containers are copied at configuration/snapshot/request boundaries.
These runtime parameters are not serialized to Project/Run metadata or service
logs. SDK environment and completion arguments remain available for provider options;
there is no application key-name filter or masking.

Application limits are direct keyword arguments: `max_iterations=8`,
`request_timeout=60.0`, `tool_timeout=30.0`, `buffer_size=8`, `max_tool_calls=16`,
`max_argument_chars=65536`, and `max_output_chars=1_000_000`. `LoopOptions` and
`options=` were removed. For model options use `completion_kwargs`, for example
`completion_kwargs={"max_tokens": 100}`.
Provider `timeout` is separate from the total per-round `request_timeout` limit;
configure both when necessary. Explicit SDK num_retries affects completion calls
only, never automatic tool or stale-Run replay. The Loop requires streaming and
one choice: stream=False/n other than 1 are rejected. messages and tools are built
from the Task transcript and Project registry; overriding messages/tools or legacy
functions/function_call is rejected. tool_choice and parallel_tool_calls are
forwarded, but actual tool execution remains serial.

## Project settings, Task overrides and Run results

```python
config = ProjectConfig(
    default_engine="loop",
    completion={"model": "openai/my-model", "top_p": 0.9, "max_tokens": 4096},
    engines={"loop": {"max_iterations": 8, "request_timeout": 60,
                      "system_prompt": "Answer clearly."}},
    task_defaults={"data": {"language": "ko"}},
    data={"documents": ["manual.md"], "application": {"theme": "dark"}},
)
project = projects.create("Workspace", config=config)
task = tasks.create(project, "Research", config={
    "completion": {"max_tokens": 2048},
    "engines": {"loop": {"system_prompt": "Explain with examples."}},
    "data": {"topic": "Python"},
})
```

Project settings live in project.json; Task overrides live in task.json.config.
ProjectConfig is a dict subclass, not a dataclass. Add arbitrary top-level keys with
`config["editor"] = {"font_size": 14}` or constructor kwargs. Existing attribute
shortcuts such as config.completion remain available; use mapping syntax for keys
that collide with dict methods. to_dict()/serialize()/deserialize() round-trip all
JSON fields. JSON type/finite-number checks remain, with no field-name blacklist.
Call projects.save(project) or tasks.save(task) after editing. Task saves require
a detached runtime as before. task_defaults is copied when a new Task is created;
changing it later does not rewrite existing Tasks. Clones copy configuration,
with independent containers, and start without execution history.

Every Engine receives the full Project/Task snapshots and can call
`context.settings("engine-name")` to get isolated settings, including arbitrary
workspace/Task keys and the selected engine section.
Nested Project and Task dictionaries merge recursively; lists/scalars replace.
Loop reads the "loop" section even inside a pipeline or under a registry alias.
Its explicit constructor limits, completion_kwargs and system_prompt override
saved settings. Omitted constructor limits inherit saved values then builtin
defaults. Runtime completion_kwargs replaces supplied argument values at the top
level, retaining client/callback identities. Project changes are reloaded before
the next Run; they do not alter an active Run's context.

Legacy project.json files without a completion section migrate flat
model/temperature/api_base into completion on load. New-format files retain
identically named top-level application keys unchanged. Unknown fields stay at their original top-level location; no special keys
are removed. Existing nested data remains nested.
Old Task files without config load with an empty override. Python callers use the
open ProjectConfig mapping; completion options should be placed under completion.

```python
await manager.submit("Explain the design")
await manager.wait_idle()

result = projects.results.list(project)[-1]  # ExecutionResult
print(result.run_id, result.engine, result.status)
print(result.total_tokens, result.finish_reasons)
for completion in result.completions:         # CompletionResult
    print(completion.step_id, completion.model, completion.finish_reason,
          completion.usage, completion.duration_seconds)
```

Run is the owner of execution observations. Each completion snapshot is persisted
by stable call ID in run.json.metadata.completions. Finishing or recovering a Run
also finalizes interrupted observations in that same file. No second result file
is written under Project or Task.

RunResultQuery exposes load/list through projects.results, tasks.results and
manager.results. ExecutionResult is an in-memory view, computed from the Run:

```python
result = tasks.results.load(task, run_id)       # direct lookup within a Task
project_runs = projects.results.list(project) # terminal Runs from active Tasks
all_runs = projects.results.list(project, include_deleted=True, include_running=True)
```

load accepts either a Project or Task; a Project lookup searches its Tasks.
Lists exclude soft-deleted Tasks and pending/running Runs by default; direct load
can inspect a soft-deleted Task's history. Reads hold workspace ownership but
never write result metadata. RunManager's query uses its injected RunRepository;
for custom storage, RunResultQuery(tasks, custom_run_repository) is also available.
Async UI callers can offload these synchronous queries through StorageIO.

Soft deletion retains Run history; permanent Task deletion removes its Runs and
therefore its query results. Project/Task clones start without Runs. Old Project
state/executions/*.json files are ignored and left untouched, including orphaned
summaries from deleted Tasks; they are not a fallback source. Existing Run metadata
needs no data move. There is no cached aggregate to synchronize or rebuild.
Project-wide queries scan Task/Run metadata; a disposable index can be added later
if measurements justify it without becoming the source of truth.

CompletionResult contains call/Step IDs, model, response ID, finish reason, status,
timestamps, elapsed seconds, numeric usage and usage_complete. ExecutionResult
contains Run/Task/Project IDs, Engine name, Run timing/status, completion results
and summed prompt_tokens/completion_tokens/total_tokens. Summaries aggregate all
LLM calls in Loops and pipelines. No cost estimate is inferred.

The shared completion helper requests stream_options.include_usage=True by
default (explicit False is honored). It captures SDK or dict usage-only chunks,
including numeric cached/reasoning token details. Totals are None if any call
lacks a count or has an uncompleted stream; partial observed usage remains on the
individual result. Unknown usage is not zero. Provider-reported counters are not
an independently verified billing ledger. No raw responses, headers, prompts,
API keys or tool arguments/results enter the execution summaries.

stream_completion now yields strings plus COMPLETION EngineEvents by default.
BaseEngine.execute() and Loop forward/persist them automatically. Use
include_events=False only for standalone text consumers that do not need usage
persistence. Custom Engine-protocol implementations can emit COMPLETION events
with CompletionResult themselves; non-reporting engines still get Run summaries
with unknown LLM usage. Cancellation never yields from a closing generator;
RunManager finalizes the last durable observation.

## Reusable embedding and rerank inference

Engine decides the execution sequence of a Run. Embedding and reranking are model
operations that Engines, Tools and RAG can share, so they live in ish/inference
rather than the Engine registry or a new child in Project -> Task -> Run -> Step.
The current reusable clients call LiteLLM's
[aembedding](https://docs.litellm.ai/docs/embedding/supported_embedding) and
[arerank](https://docs.litellm.ai/docs/rerank) APIs:

```python
from ish.inference import EmbeddingModel, RerankModel

embedding = EmbeddingModel(model="openai/text-embedding-3-small", dimensions=256)
reranker = RerankModel(model="cohere/rerank-english-v3.0", top_n=3)

vectors = await embedding.embed(["first document", "second document"])
ranked = await reranker.rerank("the query", ["first document", "second document"], top_n=1)
# Native SDK responses: vectors.data / vectors.usage, ranked.results / ranked.meta.
```

Constructor kwargs are runtime defaults; call kwargs override them. Provider-specific
options pass through without a dataclass allowlist. Builtin request containers are
copied per instance/call while SDK clients/callbacks retain identity. Model clients
can be shared across concurrent Tasks. Defaults are timeout=60 and num_retries=0;
callers may override SDK options. Native responses, exceptions and cancellation
propagate to the caller. The injected embedding_fn/rerank_fn must be asynchronous.
SDK import is lazy and offloaded. Importing ish.inference loads no Engines,
services, core models or LiteLLM SDK.

Save optional model defaults in ProjectConfig.data, for example:

```python
config.data["inference"] = {
    "embedding": {"model": "openai/text-embedding-3-small"},
    "rerank": {"model": "cohere/rerank-english-v3.0", "top_n": 3},
}
# Task.config["data"]["inference"] may override these JSON defaults.

async def prepare(context):
    settings = context.settings("retrieval")["data"]["inference"]
    result = await EmbeddingModel(**settings["embedding"]).embed(["document text"])
    context.state["embeddings"] = result.data

# Wrap prepare with PreparationStep(..., kind="embedding") or self.step(...).
# A Tool handler can await the same model methods without creating another Run.
```

Inference clients create no Steps/Runs, register no tools and write no files.
Callers own preparation, input selection, vector/index storage and lifecycle events.
Vectors/document results should stay in runtime state or component-owned storage,
not ordinary execution logs. Native model usage is returned, but inference calls
do not automatically emit COMPLETION events or enter the completion token aggregate;
a calling Engine must explicitly report observations if needed. Rerank billing
units are not assumed to be tokens. RAG collection/index CRUD remains planned.

## Write your own Engine

Start with BaseEngine. Implement only `run(context)`: yield strings for response
text, or use an async function returning None for work with no visible output.
The base class creates Step IDs and emits start/text/completion/failure events.
RunManager and StepManager still own all persistence and interruption handling.

```python
from ish.engines import EngineContext, BaseEngine

class EchoEngine(BaseEngine):
    async def run(self, context: EngineContext):
        yield "Echo: "
        yield context.messages[-1].content

engines.register("echo", EchoEngine("Echo response", kind="text"))
await manager.submit("hello", engine="echo")
```

A complete offline example with service setup is `examples/custom_engine.py`:

```powershell
.\.venv39\Scripts\python.exe examples/custom_engine.py
.\.venv\Scripts\python.exe examples/custom_engine.py
```

It prints `Echo: hello`, uses a temporary workspace, and makes no model requests.
You can also pass `BaseEngine("Name", action=async_function_or_generator)` without
writing a subclass. A generator yields strings and may forward COMPLETION events;
a coroutine must return None.
Store private/intermediate results in `context.state`, not in yielded dictionaries
or Step metadata. Use `timeout_seconds=` for an optional whole-Step deadline.

BaseEngine instances can be shared across Runs; execution state is local to each
execute call. Keep your own per-Run state in context.state/local variables rather
than on the Engine instance. Names, kind, metadata, and error_message are public,
persisted labels: use safe developer constants. Metadata is checked for JSON
serialization at construction. Raw action/SDK exceptions are replaced with a safe
failure event. Cancellation propagates, and async iterators are closed even on
failure/early close; no terminal event is yielded while the consumer is closing.
The existing Run worker finalizes an interrupted active Step. Blocking work must
still be offloaded by your action, and cancellation must not be swallowed.

The inherited `execute()` wraps `run()` in one Step. To implement multiple Steps,
override `execute(context)` and forward events from `self.step(context, action,
name=..., kind=...)`, closing each iterator with `ish.compat.aclosing`. LoopEngine
uses this pattern for every LLM round and tool operation. PreparationStep also
inherits BaseEngine. PipelineEngine composes engines inside the same Run.

BaseEngine also supplies `stream_completion(request, response=None)`. It calls
LiteLLM's synchronous `completion(..., stream=True)` through the existing bounded
thread bridge, yielding text from dictionary or SDK chunks as it arrives. This
matches the [LiteLLM streaming format](https://docs.litellm.ai/docs/completion/stream).
A minimal text-completion engine can return that async iterator directly:

```python
from ish.engines import BaseEngine

class AnswerEngine(BaseEngine):
    def run(self, context):
        return self.stream_completion({
            "model": context.project.config.completion["model"],
            "messages": [{"role": "user", "content": context.messages[-1].content}],
            "max_tokens": 1024,
            "timeout": 30,
        })

engines.register("answer", AnswerEngine("Answer", kind="llm", timeout_seconds=35))
```

`run()` here is an ordinary function returning an async iterator; BaseEngine
consumes and closes it. This example sends the current input and uses the SDK's
environment authentication. Custom engines choose their own history, system prompt,
Project options; LoopEngine resolves Project/Task defaults automatically.
For preparation or text transformation use `async def run()` with
`async with aclosing(self.stream_completion(request)) as items` and forward each
item. Text arrives as str; completion observations arrive as EngineEvent. The
inherited execute() handles both. If transforming text, first check isinstance(item, str).

Pass a fresh `response={}` to receive the assembled assistant message after normal
stream completion. It contains `role`, `content`, and optional `tool_calls` in
completion message format. Fragmented tool calls are ordered by index and
validated before success; the helper does not execute them. The output dictionary
is unchanged on failure/cancellation and must remain local to that request.
Completion observations separately report finish reason, model, response ID,
usage and timing. Reasoning/multimodal content is not surfaced by this helper. It
requires one choice and normal `stop` or `tool_calls` termination.

Each call copies builtin request containers while retaining live SDK handles.
Assembly never lives on the Engine instance. The helper emits COMPLETION observations but has no Step
lifecycle/deadline: use it inside `run()` or `self.step(...)` for those guarantees.
BaseEngine provides a LiteLLM convenience, not a cross-provider abstraction;
non-LLM engines can use only its Step event support without invoking LiteLLM.

Migration: import `BaseEngine` from `ish.engines` or `ish.engines.base` instead of
`StepEngine`/`ish.engines.step`. The old module, `_completion.py`, `LoopOptions`,
`LoopEngineError`, `_Turn`, and `_ToolCall` are removed. LoopEngine is a single
BaseEngine subclass with direct constructor options. Validation raises standard
ValueError/TypeError; execution failures retain Step error details and
RuntimeError through the common lifecycle. Existing event/persistence formats and
RunManager ownership are unchanged.

## Prepare a Run before its Loop

PipelineEngine runs Engine stages in order inside the same Run. PreparationStep
wraps an async callback as an observable Step. All stages receive the same
EngineContext and its fresh, runtime-only state dictionary. The Loop evaluates
completion_kwargs/system_prompt factories after preparation, once per execution.
A system prompt is prepended to the provider transcript and is not appended as a
new durable conversation message each round.

```python
import asyncio
import os
from pathlib import Path
from ish.engines.loop import LoopEngine
from ish.engines.pipeline import PipelineEngine, PreparationStep

async def prepare(context):
    # Offload blocking reads. This example loads context, not a RAG implementation.
    path = context.project.paths.root / "instructions.txt"
    context.state["instructions"] = await asyncio.to_thread(
        path.read_text, encoding="utf-8"
    )
    context.state["shell_env"] = dict(os.environ)  # for subprocess env=, not logs

engines.register("prepared_loop", PipelineEngine(stages=[
    PreparationStep("Read instructions", prepare, kind="retrieval"),
    LoopEngine(
        max_iterations=8,
        completion_kwargs={"top_p": 0.9, "max_tokens": 4096},
        system_prompt=lambda context: context.state["instructions"],
    ),
]))

# The caller creates instructions.txt in the Project before submitting.
await manager.submit("Help with this workspace", engine="prepared_loop")
```

PreparationStep defaults to a 60-second timeout; set timeout_seconds explicitly
or use None for no deadline. Callbacks/factories are developer code on the event
loop: avoid blocking calls, propagate cancellation, keep per-Run values in
context.state, and do not mutate global os.environ. A copied environment reflects
this process's current environment; it cannot automatically read changes from an
unrelated shell. RAG/Shell work belongs to component services; this feature does
not implement RAG synchronization or Shell execution. Engine stages never write
Task/Run/Step/conversation persistence directly.

A failed/timed-out preparation prevents subsequent stages; interruption marks the
Run and active Step interrupted and preserves queued input. Pipeline also stops
on non-success Step terminal events or unfinished Steps, and closes each stage's
iterator. A crash uses the existing stale-Run recovery policy: no preparation or
Loop side effects are automatically replayed. Pipelines may be nested; this is
sequential composition, not GraphEngine or an arbitrary-code loader. Shared
clients and developer component state still need their own concurrency handling.

RunManager accepts `on_event(run, event)` for displaying persisted events. The
callback receives snapshots after each event is recorded; keep it synchronous,
quick, and nonblocking. RunEventPublisher catches display exceptions and logs
`observer.failed` without failing the Run or dropping queued requests. Engine
and persistence errors still fail execution. See `ish/demo.py`.

The synchronous provider iterator lives in a dedicated daemon thread. Cancel
stops delivery immediately; a blocked provider read cannot be forcibly stopped
and closes in its owning thread after it returns or times out. Repeated
cancellations can temporarily leave multiple cleanup threads. They cannot
execute tools or write persistence, and their late deltas are discarded.

Visible text from all iterations is appended to the Run's single Assistant
message. Tool-call/result messages are used in memory within the Run; they are
not yet a durable structured tool transcript. Step metadata records iteration
and call IDs, not raw arguments/results or provider response objects. A restart
never resumes the tool loop of a stale Run.

API references: [LiteLLM streaming](https://docs.litellm.ai/docs/completion/stream),
[tool calling](https://docs.litellm.ai/docs/completion/function_call), and
[completion parameters](https://docs.litellm.ai/docs/completion/input).

## Use the foundation

```python
import asyncio
from pathlib import Path

from ish.core.models import ProjectConfig
from ish.engines.base import EngineRegistry
from ish.engines.loop import LoopEngine
from ish.services.conversation import ConversationStore
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager
from ish.services.tasks import TaskManager


async def main() -> None:
    tasks = TaskManager()
    projects = ProjectManager(ProjectRepository(Path("./workspace/projects")), tasks)
    project = projects.create("Example", config=ProjectConfig(
        completion={"model": "openai/gpt-4o-mini"}))
    task = tasks.create(project, "Conversation")
    engines = EngineRegistry()
    engines.register("loop", LoopEngine())
    manager = RunManager(tasks, engines, task=task)
    try:
        await manager.submit("Hello")
        await manager.wait_idle()
        for message in ConversationStore(task.paths.conversation).list():
            print(message.role, message.status, message.content)
    finally:
        await manager.shutdown()


asyncio.run(main())
```

On restart, load the Project and Task through their managers, then call
`await manager.start()`. This interrupts stale state and restores
only queued requests. Calling `start` again on an already attached Task is safe.

Project, Task, Run, and Step metadata each use a Repository/Manager pair in
`ish.services.projects`, `tasks`, `runs`, and `steps`. Repositories handle storage;
managers handle lifecycle or execution. TaskManager and StepManager create a
default repository unless one is injected:

```python
from ish.services.tasks import TaskManager, TaskRepository
from ish.services.steps import StepManager, StepRepository
from ish.services.runs import RunManager, RunRepository

tasks = TaskManager(repository=TaskRepository())
projects = ProjectManager(ProjectRepository(Path("./workspace/projects")), tasks)
steps = StepManager(repository=StepRepository())
manager = RunManager(tasks, engines, task=task, repository=RunRepository(), steps=steps)
```

Service modules are grouped by responsibility. Conversation context now uses
`ish.services.context.ConversationContextBuilder`. `StorageIO` and
`remove_owned_tree` are in `ish.services.storage`; `RunEventPublisher` is a separate
class in `ish.services.runs`. The old conversation_context.py, io.py, deletion.py,
and events.py import paths were removed. Domain managers/repositories, access,
locking and logging retain their own modules. See the service module
table in `docs/architecture.md` for the complete layout.

RunManager is now imported from `ish.services.runs`; `ish.services.run_manager`
has been removed. The previous `runs=` constructor argument and `.runs` attribute
remain aliases for the Run repository. Message events still use ConversationStore.
Project metadata now includes a `components` list. Older metadata loads with an
empty selection; existing directories are never implicitly enabled. New Projects default to `loop`; previously
saved `fake` engine selections must be changed explicitly before real execution.
The deterministic fake engine now exists only in `tests/support/fake_engine.py`.

Use one shared ProjectRepository/service container per workspace and one event
loop for its RunManager. Manager calls automatically acquire an OS lock at
`<projects-root>/.ish.lock`. Attached Tasks retain it until `await manager.shutdown()`
has drained execution/storage, including when idle. A competing repository
instance/process raises `ish.services.locking.WorkspaceBusyError` before recovery
or mutation. Process exit releases ownership. Never delete the lock file to bypass
ownership. Different workspace roots are independent.

Shutdown affected runtimes before cloning, deleting, or restoring Tasks/Projects.
RunManager performs ordered disk work in background threads and caches incremental
conversation replay. QUEUED and streaming deltas still fsync before scheduling or
notification. Cancellation waits for in-flight writes; a cancelled `submit` can
still accept and schedule a request, so do not blindly retry. Per-delta operational
logging is omitted; the delta remains in conversation JSONL.

Project/Task CRUD methods are synchronous. From an async UI, offload them through
the ownership-aware adapter:

```python
from ish.services.storage import StorageIO

storage = StorageIO(projects.ownership)
project = await storage.run(projects.create, "My project")
task = await storage.run(tasks.create, project, "My task")
await manager.submit("Hello")
```

Injected synchronous storage/context/capability adapters run on worker threads
and must not require a running event loop. Engines and UI callbacks remain on
the event loop. Direct lower-level repository/component or ConversationStore
writes need `with projects.ownership.scope():`; manager APIs already supply it.
Locks coordinate local service processes; network filesystems and arbitrary
external writers are outside this contract. See architecture/production docs.

Task clones copy configuration and conversation snapshots with fresh IDs.
Cloned queued inputs become cancelled, Run links are cleared, and execution
history and artifacts are not copied. Project clones copy configuration and
active Task snapshots through TaskManager. Soft deletion marks metadata in
place and retains the stored files. Public Project/Task `save` methods edit
configuration only: they reload current ownership/lifecycle state and cannot
resurrect deleted records or set Task execution status. Task edits require a
detached runtime. RunManager's internal transitions remain separate. Save Project
configuration changes before submitting requests; caller-owned snapshots are
not used as the source of current configuration.

## Select Project components

ProjectManager coordinates selected components; each component owns its paths,
initialization, configuration, and clone policy. `ProjectPaths.tools` and
`ProjectPaths.workflows` have been removed. Use `ToolPaths.for_project(project)`
and `WorkflowPaths.for_project(project)` when the respective subsystem needs paths.

```python
from pathlib import Path
from ish.components.registry import ComponentRegistry
from ish.components.tools import Tool, ToolRegistry, ToolComponent
from ish.components.workflows import WorkflowComponent
from ish.engines.base import EngineRegistry
from ish.engines.loop import LoopEngine
from ish.core.models import ProjectConfig
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.tasks import TaskManager
from ish.services.runs import RunManager

async def add(arguments):
    return arguments["a"] + arguments["b"]

catalog = ToolRegistry((Tool("add", "Add two numbers", {
    "type": "object",
    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
    "required": ["a", "b"], "additionalProperties": False,
}, add),))
components = ComponentRegistry((ToolComponent(catalog), WorkflowComponent()))
tasks = TaskManager()
projects = ProjectManager(ProjectRepository(Path("workspace/projects")), tasks,
                          components=components)
project = projects.create("Example", components=("tools", "workflows"),
                          config=ProjectConfig(completion={"model": "openai/gpt-4o-mini"}))
projects.configure_component(project, "tools", {"enabled": ["add"]})
task = tasks.create(project, "Conversation")
engines = EngineRegistry()
engines.register("loop", LoopEngine())
manager = RunManager(tasks, engines, task=task, capabilities=components)
```

Call `await manager.submit(text)` from an async UI handler.
Pass the same configured component registry to ProjectManager and RunManager.
Registered components are available to select; they are not automatically enabled.
`create(..., components=())` creates no tool/workflow directories. Selecting tools
creates `<project>/tools/component.json` with an empty `enabled` list; selecting
workflows creates `<project>/workflows/component.json` and `records/`. Tool handlers remain in the application
catalog and are never serialized into Project JSON.

`projects.set_components(project, ("tools",))` changes selection after creation.
Disabling a component preserves its data. Initializers must be idempotent;
re-enabling tools keeps their previous configuration. Unknown/duplicate component
identities are rejected before Project creation. Initialization/clone failures
leave the new Project soft-deleted; restore retries initialization. A failed
selection update leaves the old selection in place, though partial directories
may remain. This is not a transactional plugin installer.

LoopEngine no longer accepts `tools=`. A Run resolves a fresh tool registry from
the saved Project selection/configuration into `EngineContext.tools`. This
snapshot remains fixed through that Run; later Runs see saved changes. A Project
without enabled tools cannot invoke another Project's tools, even through the
same LoopEngine instance. Missing component implementations fail execution before
the provider is called. After restart, reconstruct the registered Python handlers
and components; the saved names do not automatically import executable code.

The legacy constructor `initializers=` still runs mandatory application
initializers for each Project. Use the component registry for optional features
that users can select; core Task initialization is always performed.

Project cloning copies component configuration and named JSON definitions, including
Tool overrides, workflow graphs and subagent settings. Record IDs remain stable
inside the new Project for graph references; arbitrary artifacts/indexes and live
handlers are not copied. Graph execution and RAG/MCP/Skill adapters remain planned.

### Component data and custom components

The base and registry are independent of Tools. Subclass `Component`, explicitly
declare `name` and `directory`, and register the instance. Configuration and records
are open JSON dicts; you can add keys without changing a dataclass.

```python
from ish.components import Component
from ish.components.subagents import SubagentComponent

class NotesComponent(Component):
    name = "notes"
    directory = "knowledge"

components.register(NotesComponent())
components.register(SubagentComponent())
projects.set_components(project, ("tools", "workflows", "notes", "subagents"))

agents = projects.component(project, "subagents")
agents.create({
    "completion": {"model": "openai/gpt-4o-mini", "temperature": 0.2},
    "system_prompt": "Review code carefully.",
    "custom_option": True,
}, identifier="reviewer")

graphs = projects.component(project, "workflows")
graphs.create({
    "nodes": [{"id": "review", "subagent": "reviewer"}],
    "edges": [],
}, identifier="code_review")
graphs.update("code_review", {"description": "Review workflow"})
graph = graphs.load("code_review")
encoded = Component.serialize(graph)
restored = Component.deserialize(encoded)
all_graphs = graphs.list()  # {"code_review": {...}}

# Replace a record with save(id, data); update applies a shallow key patch.
# Delete one record with graphs.delete("code_review").
projects.remove_component(project, "notes")  # Disable and keep knowledge/.
projects.remove_component(project, "notes", permanent=True)  # Delete knowledge/.
```

Every selected component owns `<project>/<directory>/component.json` and
`records/<id>.json`. Creation and `set_components` create missing directories.
Base initialization is idempotent. Permanent removal requires detached Tasks and
publishes disabled selection first; an I/O failure can leave partial data for retry.

`projects.component` returns a locked handle that rechecks Project state/selection
on every call. In async UI code, use `StorageIO(projects.ownership).run` for its
synchronous CRUD. Direct component methods require a workspace ownership scope.
JSON must have string keys and JSON-compatible values; live SDK objects remain
runtime-only. There is no application key-name filter. Subagent/graph definitions are data only; they
are not automatically executed or interpreted as a fixed graph schema.

ToolComponent additionally stores native function-tool definitions in
`tools/records/<tool-name>.json`. Create one with the usual LiteLLM `type/function`
dict. Reading, editing and cloning definitions needs no registered handler;
execution binds the same name to an application handler. Extra provider keys such as
`function.strict` survive persistence and appear in the LoopEngine tools argument.
The enabled list still controls availability. Disable a tool before deleting its
override. Existing enabled-name-only configuration keeps working.

RunManager still accepts `capabilities=components`. Tool-specific resolution now
lives in `ish.components.tools.resolver.ComponentToolResolver`; for direct access,
use `ComponentToolResolver(components).resolve_tools(project)`. Generic components
declare `capabilities = ("retriever",)` and implement
`resolve(project, capability_name)`. The registry calls only components declaring
the requested capability, and each component builds only that requested value.
See [component API and persistence details](ish/components/README.md).

TaskManager is bound to authoritative ProjectAccess when constructed with
ProjectManager. A standalone TaskManager must instead receive `project_access=`.
ConversationStore creation is injectable through `conversations=`, and
ConversationContextBuilder centralizes Run context and clone ordering. RunManager
defaults to the same factory/builder as TaskManager. For UI-to-storage separation,
configure these services once in the application's composition code.

## Task-bound execution and Run notifications

Each RunManager requires one Task at construction. Share ProjectRepository,
TaskManager and registries across managers to retain workspace coordination;
create a separate RunManager for each Task. A manager cannot switch Tasks.

```python
from ish.services.runs import RunManager, RunRequestError

# task was created with tasks.create(project, "Conversation").
def on_run_event(event):
    print(event.type, event.run.id, event.run.status, event.run.error_code)

manager = RunManager(tasks, engines, task=task,
                     capabilities=components, on_run_event=on_run_event)
try:
    await manager.submit("Explain this workspace", engine="loop")
    await manager.wait_idle()
except RunRequestError as error:
    print(error.code, str(error))
finally:
    await manager.shutdown()
```

`start()`, `submit(text)`, `wait_idle()`, `interrupt()` and `shutdown()` operate on
that bound Task; the old Project/Task positional arguments are removed.
shutdown interrupts only that Task, preserves its queued messages, drains writes
and releases its attachment. Other managers continue. Create a new manager for
the same Task and call start() to recover/resume queued requests after shutdown.

Missing Engine or component registrations raise RunRequestError **before queue
admission**, without creating messages/Runs or attaching a worker. Accepted input
is still fsynced as QUEUED before runtime scheduling. Restored queued requests
whose Engine is no longer registered become failed Runs without an Engine call.

`on_event(run, engine_event)` remains the streaming/Step observer. The separate
`on_run_event(event)` observes STARTED, COMPLETED, FAILED and INTERRUPTED, after
the corresponding durable state writes. Callbacks run synchronously on the event
loop, receive detached snapshots, and cannot fail execution by raising. Recovery
also emits INTERRUPTED for newly recovered stale Runs. These are in-process
notifications, not a durable event-subscription log; query Runs after reconnect.

Run.error_code contains a stable machine-readable string: engine_not_registered,
component_not_registered (request rejection), capability_failed, engine_failed,
interrupted or process_restart. Run.error contains the exception detail for a
failed execution; there is no exception-message masking. Error/code are also in
ExecutionResult query views. wait_idle means the queue drained, not that every Run
succeeded: use the terminal notification or query the Run status. Storage failure
that prevents finalization propagates through wait_idle/shutdown; no terminal
notification is emitted before a successful terminal save.

## Delete and restore

After `await manager.shutdown()`, use the lifecycle APIs:

```python
tasks.delete(task)                         # reversible, history retained
tasks.restore(task)
projects.delete(project)                   # reversible, Tasks retained
projects.restore(project)

tasks.delete(task, permanent=True)         # removes Task + history/Runs/Steps/files
projects.delete(project, permanent=True)   # removes Project + all its Tasks/files
```

`soft_delete` was renamed to `delete`. `permanent` is keyword-only and defaults
to `False`. Permanent deletion cannot be restored through `restore`. Active
persisted Runs and runtimes attached through the shared TaskManager block
deletion, including idle workers and queued input. Paths and ownership are
checked before removal; linked paths/descendants are rejected. This assumes
exclusive filesystem ownership; it is not protection against concurrent writers.

## Package boundaries and logs

`ish/engines` contains execution strategies and the Engine contract. LoopEngine
is implemented; SingleEngine and GraphEngine remain planned. Reusable Tool and
ToolRegistry and Project tool selection live in `ish/components/tools`.
`workflows` and `subagents` use shared definition CRUD; `rag`, `mcp`, and `skills`
remain reserved for future component adapters. Provider
stream transport lives in `ish/providers/litellm.py`. TaskRuntime lives in
`ish/services/tasks.py` and is owned and scheduled by RunManager.

Services write structured operational JSON lines through Python `logging` and
`RotatingFileHandler`. Each Project, Task, Run, and Step owns
`logs/service.log`, with up to three 1 MiB backups. Conversation operations and
runtime scheduling log to Task; Run/Step lifecycle operations log to their
respective domains. Model observations use the existing Run persistence path and
Run logs. SDK authentication and diagnostics are managed by the provider library.

Only event names, IDs, statuses, counts, and deletion flags are recorded;
prompts, answers, titles, arbitrary metadata and references are
excluded. Log files are separate from durable conversation JSONL. Handlers are
closed after writes, and log I/O failures emit an operational warning. Operational
logs are best effort, not a transactional audit log. Permanent Task deletion
leaves its final deletion record in Project logs; permanent Project deletion
leaves it in the projects root's `logs/service.log`.

See [architecture](docs/architecture.md) for boundaries and
[handoff](docs/handoff.md) for implemented scope and next steps. See the
[production readiness assessment](docs/production-readiness.md) before deployment.
