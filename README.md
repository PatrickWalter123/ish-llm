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

Latest verification: all 91 tests passed on both Python 3.9.13 (59.162 seconds,
LiteLLM 1.80.17) and Python 3.13.7 (53.116 seconds, LiteLLM 1.100.0). Both SDK
versions passed the actual SDK/mock SSE test. These results cover the installed
interpreters; Python 3.9.25 was not separately executed. The test outputs are
`test-results-python39.txt` and `test-results-python313.txt`.

## Run a real streaming request

Set your provider credential in the environment, then run the demo. The command
below assumes `OPENAI_API_KEY` is already set. Replace the model identifier with
one available to your provider account.

```sh
python -m ish.demo --model openai/gpt-4o-mini --credential-ref env:OPENAI_API_KEY --prompt "Use add to calculate 12 + 30, then explain the result." --with-tools
```

Each invocation creates a Project and Task under `workspace/projects/`, prints
the Project path, and prints text deltas as they are persisted. `--with-tools`
registers only an example `add` function. Without it, the engine streams a single
answer unless the provider returns an unsupported tool call. Ctrl+C interrupts
active work through RunManager shutdown. `--api-base` selects an optional
compatible endpoint; `--temperature` defaults to omission so model defaults apply.

To attach the engine to your own services:

```python
from ish.engines.loop import LoopEngine, LoopOptions

engines.register("loop", LoopEngine(options=LoopOptions(max_iterations=8)))
# Select "loop" through ProjectConfig.default_engine, Task.default_engine,
# or await manager.submit(project, task, content, engine="loop").
```

ProjectConfig supplies `model`, optional `temperature`, optional `api_base`, and
`credential_ref`. SecretManager resolves `env:NAME` at execution time; secret
values are not stored in Project JSON. With no reference, provider SDK
environment/default authentication remains available, including keyless local
endpoints. Do not put credentials in endpoint URLs.

LoopEngine passes `stream=True`, a timeout, and `num_retries=0` to
`litellm.completion`. It forwards content deltas immediately, assembles indexed
tool-call fragments, validates the complete batch against registered JSON
schemas, executes async tool handlers serially, and includes their results in
the next completion. Register tools through `ish.components.tools.ToolRegistry`;
handlers receive an argument dictionary and return text or JSON-compatible data.
Handlers must avoid blocking the event loop and propagate cancellation.

Each LLM iteration and tool execution emits Step events. A `stop` finish reason
ends the Run. Truncated streams, provider/tool errors, invalid arguments, and
exhausted iteration limits fail the Run without automatic retries. Tools from a
last iteration are not executed when no follow-up completion can be made.
Output and tool arguments are bounded by LoopOptions, and a bounded stream
bridge applies backpressure. The default total timeout per LLM round is 60s;
each tool has a 30s timeout.

RunManager accepts `on_event(run, event)` for displaying persisted events. The
callback receives snapshots after each event is recorded; keep it quick and
nonblocking. A callback exception fails the active Run. See `ish/demo.py`.

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
        model="openai/gpt-4o-mini", temperature=None,
        credential_ref="env:OPENAI_API_KEY"))
    task = tasks.create(project, "Conversation")
    engines = EngineRegistry()
    engines.register("loop", LoopEngine())
    manager = RunManager(tasks, engines)
    try:
        await manager.submit(project, task, "Hello")
        await manager.wait_idle(project, task)
        for message in ConversationStore(task.paths.conversation).list():
            print(message.role, message.status, message.content)
    finally:
        await manager.shutdown()


asyncio.run(main())
```

On restart, load the Project and Task through their managers, then call
`await manager.start(project, task)`. This interrupts stale state and restores
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
steps = StepManager(repository=StepRepository())
manager = RunManager(tasks, engines, repository=RunRepository(), steps=steps)
```

RunManager is now imported from `ish.services.runs`; `ish.services.run_manager`
has been removed. The previous `runs=` constructor argument and `.runs` attribute
remain aliases for the Run repository. Message events still use ConversationStore.
Persisted formats are unchanged. New Projects default to `loop`; previously
saved `fake` engine selections must be changed explicitly before real execution.
The deterministic fake engine now exists only in `tests/support/fake_engine.py`.

Use one RunManager per workspace, in one process and event loop. Shutdown the
manager before cloning, deleting, or restoring its Tasks/Projects. Metadata
writes and conversation appends flush synchronously; high-volume persistence
optimization and cross-process locking remain future work.

Task clones copy configuration and conversation snapshots with fresh IDs.
Cloned queued inputs become cancelled, Run links are cleared, and execution
history and artifacts are not copied. Project clones copy configuration and
active Task snapshots through TaskManager. Soft deletion marks metadata in
place and retains the stored files.

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
ToolRegistry live in `ish/components/tools`, alongside reserved `rag`, `mcp`,
`skills`, `subagents`, and `workflows` packages for future component CRUD and
runtime adapters. These reserved packages do not yet implement CRUD. Provider
stream transport lives in `ish/providers/litellm.py`. TaskRuntime lives in
`ish/services/tasks.py` and is owned and scheduled by RunManager.

Services write structured operational JSON lines through Python `logging` and
`RotatingFileHandler`. Each Project, Task, Run, and Step owns
`logs/service.log`, with up to three 1 MiB backups. Conversation operations and
runtime scheduling log to Task; Run/Step lifecycle operations log to their
respective domains. Environment credential resolution uses Project logs when
LoopEngine constructs SecretManager, or `SecretManager(log_dir=project.paths.logs)`.
Unscoped/custom secret resolvers are responsible for their own diagnostics.

Only event names, IDs, statuses, counts, and deletion flags are recorded;
prompts, answers, titles, arbitrary metadata, references, and secret values are
excluded. Log files are separate from durable conversation JSONL. Handlers are
closed after writes, and log I/O failures emit a sanitized warning. Operational
logs are best effort, not a transactional audit log. Permanent Task deletion
leaves its final deletion record in Project logs; permanent Project deletion
leaves it in the projects root's `logs/service.log`.

See [architecture](docs/architecture.md) for boundaries and
[handoff](docs/handoff.md) for implemented scope and next steps. See the
[production readiness assessment](docs/production-readiness.md) before deployment.
