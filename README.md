# ish

Durable execution foundation for a Python 3.12+ AI TUI client. Includes a
deterministic FakeStreamingEngine and a LiteLLM LoopEngine that streams text,
executes explicitly registered tools, and continues until a final answer.
There is no TUI yet.

## Install

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, activate with `.\.venv\Scripts\Activate.ps1`, or invoke
`.\.venv\Scripts\python.exe` directly. LiteLLM 1.100.0 was used for SDK verification.

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
the next completion. Register tools through `ish.engines.tools.ToolRegistry`;
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

from ish.engines.base import EngineRegistry
from ish.engines.fake import FakeStreamingEngine
from ish.services.conversation import ConversationStore
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.run_manager import RunManager
from ish.services.tasks import TaskManager


async def main() -> None:
    tasks = TaskManager()
    projects = ProjectManager(ProjectRepository(Path("./workspace/projects")), tasks)
    project = projects.create("Example")
    task = tasks.create(project, "Conversation")
    engines = EngineRegistry()
    engines.register("fake", FakeStreamingEngine(delay=0.01))
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

Use one RunManager per workspace, in one process and event loop. Shutdown the
manager before cloning, deleting, or restoring its Tasks/Projects. Metadata
writes and conversation appends flush synchronously; high-volume persistence
optimization and cross-process locking remain future work.

Task clones copy configuration and conversation snapshots with fresh IDs.
Cloned queued inputs become cancelled, Run links are cleared, and execution
history and artifacts are not copied. Project clones copy configuration and
active Task snapshots through TaskManager. Soft deletion marks metadata in
place and retains the stored files.

See [architecture](docs/architecture.md) for boundaries and
[handoff](docs/handoff.md) for implemented scope and next steps.
