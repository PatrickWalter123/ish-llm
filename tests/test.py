from pathlib import Path

from ish.core.models import ProjectConfig
from ish.engines.base import EngineEventType, EngineRegistry
from ish.engines.loop import LoopEngine
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.tasks import TaskManager
from ish.services.runs import RunManager


task_manager = TaskManager()
project_manager = ProjectManager(
    repository=ProjectRepository(Path("./workspace/projects")),
    tasks=task_manager,
)
engine_registry = EngineRegistry()
engine_registry.register(
    "loop",
    LoopEngine(max_iterations=8),
)


# Engine 이벤트가 저장된 직후 호출됩니다.
def on_engine_event(run, event):
    if event.type == EngineEventType.TEXT_DELTA:
        # 나중에는 이 부분을 UI의 응답 추가 함수로 연결합니다.
        # ui.append_assistant_text(run.task_id, event.text)
        print(event.text, end="", flush=True)


# Task마다 RunManager를 생성합니다.
async def on_user_input(run_manager, text: str):
    return await run_manager.submit(text, engine="loop")


async def main():
    project = project_manager.create(
        "개발 프로젝트", config=ProjectConfig(completion={"model": "openai/gpt-4o-mini"}))
    task = task_manager.create(project, "로그인 기능 개발")
    run_manager = RunManager(task_manager, engine_registry, task=task, on_event=on_engine_event)
    try:
        await on_user_input(run_manager, "Python asyncio를 간단히 설명해줘.")
        await run_manager.wait_idle()
        print()
    finally:
        await run_manager.shutdown()


def main2():
    project = project_manager.load("9a14118077334da1a8efc8852705c3f5")
    task = task_manager.load(project, "ed914952410b43528fde90cd9f8c9019")
    print(task)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
    # main2()
