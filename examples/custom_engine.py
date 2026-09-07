"""Run an offline custom Engine: python examples/custom_engine.py (after pip install -e .)."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from ish.core.models import ProjectConfig
from ish.engines import EngineContext, EngineRegistry, BaseEngine
from ish.engines.base import EngineEventType
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager
from ish.services.tasks import TaskManager


class EchoEngine(BaseEngine):
    # Implement only your operation. No Step IDs, lifecycle events, or file writes.
    async def run(self, context: EngineContext):
        yield "Echo: "
        yield context.messages[-1].content


async def main():
    # This example uses disposable storage and needs no API key or network.
    with TemporaryDirectory(prefix="ish-example-") as directory:
        tasks = TaskManager()
        projects = ProjectManager(ProjectRepository(Path(directory)), tasks)
        project = projects.create("Example", config=ProjectConfig(default_engine="echo"))
        task = tasks.create(project, "Conversation")
        engines = EngineRegistry()
        engines.register("echo", EchoEngine("Echo response", kind="text"))

        def display(run, event):
            if event.type == EngineEventType.TEXT_DELTA:
                print(event.text, end="", flush=True)

        manager = RunManager(tasks, engines, on_event=display)
        try:
            await manager.submit(project, task, "hello")
            await manager.wait_idle(project, task)
            print()
        finally:
            await manager.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
