from pathlib import Path

from ish.core.models import ProjectConfig
from ish.engines.base import EngineEventType, EngineRegistry
from ish.engines.loop import LoopEngine, LoopOptions
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
    LoopEngine(options=LoopOptions(max_iterations=8)),
)


# Engine 이벤트가 저장된 직후 호출됩니다.
def on_engine_event(run, event):
    if event.type == EngineEventType.TEXT_DELTA:
        # 나중에는 이 부분을 UI의 응답 추가 함수로 연결합니다.
        # ui.append_assistant_text(run.task_id, event.text)
        print(event.text, end="", flush=True)


# 애플리케이션에서 하나를 만들어 유지합니다.
run_manager = RunManager(
    tasks=task_manager,
    engines=engine_registry,
    on_event=on_engine_event,
)


# UI의 전송 버튼 / Enter 이벤트에서 호출
async def on_user_input(project, task, text: str):
    message = await run_manager.submit(
        project,
        task,
        text,
        engine="loop",
    )
    return message


# 애플리케이션 종료 시 호출
async def on_app_shutdown():
    await run_manager.shutdown()


async def main():
    try:
        project = project_manager.create(
			"개발 프로젝트",
			config=ProjectConfig(
				model="openai/gpt-4o-mini",
				default_engine="loop",
				credential_ref="env:OPENAI_API_KEY",
				temperature=None,
			),
		)
        task = task_manager.create(
			project,
			"로그인 기능 개발",
		)

        await on_user_input(
            project,
            task,
            "Python asyncio를 간단히 설명해줘.",
        )
        # 예제 프로그램이 답변 도중 종료되지 않도록 기다립니다.
        # UI의 전송 이벤트에서는 이 대기를 넣지 않아도 됩니다.
        await run_manager.wait_idle(project, task)
        print()
    finally:
        await on_app_shutdown()


def main2():
    project = project_manager.load("9a14118077334da1a8efc8852705c3f5")
    task = task_manager.load(project, "ed914952410b43528fde90cd9f8c9019")
    print(task)


if __name__ == "__main__":
    # import asyncio
    # asyncio.run(main())
    main2()
