"""Run one real streaming request: python -m ish.demo --help."""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from ish.core.models import ProjectConfig, Run, RunStatus
from ish.engines.base import EngineEvent, EngineEventType, EngineRegistry
from ish.engines.loop import LoopEngine, LoopOptions
from ish.components.tools import Tool, ToolRegistry
from ish.components.tools.component import ToolComponent
from ish.components.registry import ComponentRegistry
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager
from ish.services.tasks import TaskManager


async def add(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"result": arguments["a"] + arguments["b"]}


def print_event(run: Run, event: EngineEvent) -> None:
    if event.type == EngineEventType.TEXT_DELTA:
        print(event.text, end="", flush=True)


async def run_request(args: argparse.Namespace) -> int:
    tasks = TaskManager()
    tools = ToolRegistry()
    components = ComponentRegistry((ToolComponent(tools),))
    projects = ProjectManager(ProjectRepository(args.workspace / "projects"), tasks, components=components)
    project = projects.create("LoopEngine demo", config=ProjectConfig(
        model=args.model, temperature=args.temperature, default_engine="loop",
        credential_ref=args.credential_ref, api_base=args.api_base,
    ), components=("tools",) if args.with_tools else ())
    task = tasks.create(project, "Streaming request")
    if args.with_tools:
        tools.register(Tool("add", "Add two numbers.", {
            "type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"], "additionalProperties": False,
        }, add))
        projects.configure_component(project, "tools", {"enabled": ["add"]})
    engines = EngineRegistry()
    engines.register("loop", LoopEngine(options=LoopOptions(
        max_iterations=args.max_iterations, request_timeout=args.timeout,
    )))
    manager = RunManager(tasks, engines, on_event=print_event, capabilities=components)
    print(f"Project: {project.paths.root.resolve()}", file=sys.stderr)
    try:
        await manager.submit(project, task, args.prompt)
        await manager.wait_idle(project, task)
        run = manager.runs.list(task)[0]
        print()
        if run.status != RunStatus.COMPLETED:
            print(f"Run {run.status}: {run.error or 'interrupted'}", file=sys.stderr)
            return 1
        return 0
    finally:
        await manager.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="LiteLLM provider/model identifier")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--workspace", type=Path, default=Path("workspace"))
    parser.add_argument("--credential-ref", help="Credential reference, e.g. env:OPENAI_API_KEY")
    parser.add_argument("--api-base", help="Optional OpenAI-compatible endpoint URL")
    parser.add_argument("--temperature", type=float, default=None, help="Omit for model default")
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--with-tools", action="store_true", help="Register the example add tool")
    args = parser.parse_args()
    try:
        return asyncio.run(run_request(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
