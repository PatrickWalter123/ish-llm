import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from ish.core.models import ProjectConfig, RunStatus
from ish.engines import EngineRegistry
from ish.engines.loop import LoopEngine
from ish.engines.pipeline import PipelineEngine
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager
from ish.services.storage import atomic_json, read_json
from ish.services.tasks import TaskManager
from tests.support.fake_engine import FakeStreamingEngine
from tests.test_loop import ScriptedCompletion, chunk


class ConfigurationResultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tasks = TaskManager()
        self.projects = ProjectManager(ProjectRepository(Path(temp.name)), self.tasks)
        self.project = self.projects.create("Project", config=ProjectConfig(
            completion={"model": "openai/test", "top_p": 0.8},
            engines={"loop": {"max_iterations": 2, "system_prompt": "project prompt"}},
            task_defaults={"data": {"language": "ko"}}, data={"project_label": "test"},
        ))
        self.task = self.tasks.create(self.project, "Task")
        self.engines = EngineRegistry()
        self.manager = RunManager(self.tasks, self.engines, task=self.task)
        self.addAsyncCleanup(self.manager.shutdown)

    async def submit(self, engine="loop", task=None):
        task = task or self.task
        manager = self.manager if task.id == self.task.id else RunManager(self.tasks, self.engines, task=task)
        try:
            await manager.submit("hello", engine=engine)
            await asyncio.wait_for(manager.wait_idle(), 15)
            return manager.runs.list(task)[-1]
        finally:
            if manager is not self.manager:
                await manager.shutdown()

    def usage_chunk(self, prompt=2, answer=3):
        return {"choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": answer,
                "total_tokens": prompt + answer, "prompt_tokens_details": {"cached_tokens": 1}}}

    async def test_flexible_config_persists_inherits_and_runtime_options_win(self):
        self.task.config["completion"] = {"top_p": 0.6, "extra_body": {"setting": 1}}
        self.task.config["engines"] = {"loop": {"system_prompt": "task prompt", "max_iterations": 1}}
        self.tasks.save(self.task)
        provider = ScriptedCompletion([chunk("answer", finish="stop")])
        self.engines.register("loop", LoopEngine(completion_fn=provider, completion_kwargs={"top_p": 0.4}))
        self.assertEqual(self.tasks.load(self.project, self.task.id).config, self.task.config)
        self.assertEqual(self.project.config, self.projects.load(self.project.id).config)
        run = await self.submit()
        request = provider.requests[0]
        self.assertEqual(request["top_p"], 0.4)
        self.assertEqual(request["extra_body"], {"setting": 1})
        self.assertEqual(request["messages"][0]["content"], "task prompt")
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(self.task.config["data"]["language"], "ko")
        settings = self.project.config.for_engine("loop", self.task.config)
        settings["completion"]["extra_body"]["setting"] = 99
        self.assertEqual(self.task.config["completion"]["extra_body"]["setting"], 1)

    async def test_project_updates_reach_later_runs_without_mutating_active_snapshot(self):
        provider = ScriptedCompletion([chunk("one", finish="stop")], [chunk("two", finish="stop")])
        self.engines.register("loop", LoopEngine(completion_fn=provider))
        await self.submit()
        self.project.config.completion["top_p"] = 0.2
        self.projects.save(self.project)
        await self.submit()
        self.assertEqual([request["top_p"] for request in provider.requests], [0.8, 0.2])

    async def test_completion_metadata_usage_only_chunks_and_no_payload_leak(self):
        usage = self.usage_chunk()
        usage["usage"]["private"] = "do-not-save"
        provider = ScriptedCompletion([
            {**chunk("answer", finish="stop"), "id": "response-1", "model": "served-model",
             "headers": {"Authorization": "do-not-save"}}, usage,
        ])
        self.engines.register("loop", LoopEngine(completion_fn=provider))
        run = await self.submit()
        result = self.projects.results.load(self.project, run.id)
        completion, = result.completions
        self.assertEqual(result.total_tokens, 5)
        self.assertEqual(result.finish_reasons, ["stop"])
        self.assertEqual(completion.response_id, "response-1")
        self.assertEqual(completion.model, "served-model")
        self.assertEqual(completion.usage["prompt_tokens_details"], {"cached_tokens": 1})
        self.assertEqual(completion.step_id, self.manager.steps.list(run)[0].id)
        self.assertGreaterEqual(completion.duration_seconds, 0)
        self.assertTrue(provider.requests[0]["stream_options"]["include_usage"])
        self.assertEqual(len(run.metadata["completions"]), 1)
        for path in self.project.paths.root.rglob("*.json"):
            self.assertNotIn("do-not-save", path.read_text(encoding="utf-8"))

    async def test_pipeline_aggregates_all_completion_calls_and_missing_usage_is_unknown(self):
        provider = ScriptedCompletion([chunk("one", finish="stop"), self.usage_chunk()],
                                      [chunk("two", finish="stop"), self.usage_chunk(4, 6)])
        self.engines.register("pipeline", PipelineEngine([LoopEngine(completion_fn=provider),
                                                          LoopEngine(completion_fn=provider)]))
        run = await self.submit("pipeline")
        result = self.projects.results.load(self.project, run.id)
        self.assertEqual(result.engine, "pipeline")
        self.assertEqual(result.total_tokens, 15)
        self.assertEqual(result.finish_reasons, ["stop", "stop"])
        self.assertEqual(len({c.id for c in result.completions}), 2)
        self.engines.register("missing", LoopEngine(completion_fn=ScriptedCompletion([chunk("none", finish="stop")])))
        missing = self.projects.results.load(self.project, (await self.submit("missing")).id)
        self.assertIsNone(missing.total_tokens)
        self.assertFalse(missing.completions[0].usage_complete)

    async def test_failure_keeps_finish_reason_and_non_llm_engines_have_summary(self):
        provider = ScriptedCompletion([chunk("partial", finish="length"), self.usage_chunk()])
        self.engines.register("loop", LoopEngine(completion_fn=provider))
        result = self.projects.results.load(self.project, (await self.submit()).id)
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.finish_reasons, ["length"])
        self.assertEqual(result.total_tokens, 5)
        self.engines.register("fake", FakeStreamingEngine())
        fake = self.projects.results.load(self.project, (await self.submit("fake")).id)
        self.assertEqual(fake.status, RunStatus.COMPLETED)
        self.assertEqual(fake.completions, [])
        self.assertIsNone(fake.total_tokens)

    async def test_interrupt_preserves_observed_usage_without_claiming_final_total(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def provider(**request):
            yield chunk("partial")
            yield self.usage_chunk()
            release.wait(5)
            yield chunk(finish="stop")
        self.engines.register("loop", LoopEngine(completion_fn=provider))
        await self.manager.submit("hello")
        async def observed():
            while True:
                runs = self.manager.runs.list(self.task)
                if runs and runs[0].metadata.get("completions", [{}])[0].get("usage"):
                    return
                await asyncio.sleep(0.005)
        await asyncio.wait_for(observed(), 10)
        await self.manager.interrupt()
        await self.manager.wait_idle()
        run = self.manager.runs.list(self.task)[0]
        result = self.projects.results.load(self.project, run.id)
        self.assertEqual(result.status, RunStatus.INTERRUPTED)
        self.assertEqual(result.completions[0].status, RunStatus.INTERRUPTED)
        self.assertEqual(result.completions[0].usage["total_tokens"], 5)
        self.assertIsNone(result.total_tokens)
        release.set()

    async def test_recovery_finalizes_stale_observations_in_run(self):
        self.engines.register("loop", LoopEngine(completion_fn=ScriptedCompletion([
            chunk("ok", finish="stop"), self.usage_chunk()])))
        run = await self.submit()
        await self.manager.shutdown()
        self.assertFalse((self.project.paths.state / "executions").exists())
        run.status = RunStatus.RUNNING
        run.metadata["completions"][0]["status"] = RunStatus.RUNNING
        run.metadata["completions"][0]["usage_complete"] = False
        self.manager.runs.save(run)
        other = RunManager(self.tasks, self.engines, task=self.task)
        try:
            await other.start()
            result = self.projects.results.load(self.project, run.id)
            self.assertEqual(result.status, RunStatus.INTERRUPTED)
            self.assertEqual(result.completions[0].status, RunStatus.INTERRUPTED)
            self.assertIsNone(result.total_tokens)
            self.assertEqual(len(self.projects.results.list(self.project)), 1)
            persisted = other.runs.load(self.task, run.id)
            self.assertEqual(persisted.metadata["completions"][0]["status"], "interrupted")
            self.assertFalse((self.project.paths.state / "executions").exists())
        finally:
            await other.shutdown()

    async def test_clone_copies_config_but_does_not_duplicate_execution_history(self):
        self.engines.register("fake", FakeStreamingEngine())
        await self.submit("fake")
        await self.manager.shutdown()
        cloned = self.projects.clone(self.project)
        task, = self.tasks.list(cloned)
        self.assertEqual(task.config, self.task.config)
        self.assertEqual(cloned.config, self.project.config)
        self.assertEqual(self.projects.results.list(cloned), [])
        self.tasks.delete(self.task, permanent=True)
        self.assertEqual(self.projects.results.list(self.project), [])

    async def test_project_and_task_queries_read_the_same_run_without_result_files(self):
        self.engines.register("loop", LoopEngine(completion_fn=ScriptedCompletion([
            chunk("answer", finish="stop"), self.usage_chunk()])))
        run = await self.submit()
        path = run.paths.root / "run.json"
        before = path.read_bytes()
        project_view = self.projects.results.load(self.project, run.id)
        task_view = self.tasks.results.load(self.task, run.id)
        self.assertEqual(project_view, task_view)
        self.assertEqual(self.manager.results.list(self.task), [task_view])
        task_view.completions[0].usage["total_tokens"] = 999
        self.assertEqual(self.tasks.results.load(self.task, run.id).total_tokens, 5)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.project.paths.state / "executions").exists())

    async def test_old_project_summaries_are_ignored_and_do_not_resurrect_runs(self):
        self.engines.register("fake", FakeStreamingEngine())
        run = await self.submit("fake")
        legacy = self.project.paths.state / "executions" / (run.id + ".json")
        atomic_json(legacy, {"stale_summary": True, "total_tokens": 999})
        self.assertIsNone(self.projects.results.load(self.project, run.id).total_tokens)
        await self.manager.shutdown()
        self.tasks.delete(self.task, permanent=True)
        self.assertEqual(self.projects.results.list(self.project), [])
        with self.assertRaises(FileNotFoundError):
            self.projects.results.load(self.project, run.id)
        self.assertEqual(read_json(legacy), {"stale_summary": True, "total_tokens": 999})

    async def test_task_filter_soft_delete_and_cross_project_lookup(self):
        self.engines.register("fake", FakeStreamingEngine())
        first = await self.submit("fake")
        second_task = self.tasks.create(self.project, "Second")
        second = await self.submit("fake", task=second_task)
        self.assertEqual([item.run_id for item in self.tasks.results.list(second_task)], [second.id])
        self.assertEqual(len(self.projects.results.list(self.project)), 2)
        with self.assertRaises(FileNotFoundError):
            self.tasks.results.load(second_task, first.id)
        other = self.projects.create("Other")
        with self.assertRaises(FileNotFoundError):
            self.projects.results.load(other, first.id)
        await self.manager.shutdown()
        self.tasks.delete(self.task)
        self.assertEqual(len(self.projects.results.list(self.project)), 1)
        self.assertEqual(len(self.projects.results.list(self.project, include_deleted=True)), 2)
        self.tasks.restore(self.task)
        self.assertEqual(len(self.projects.results.list(self.project)), 2)

    async def test_running_results_and_injected_repository(self):
        from typing import get_type_hints
        from ish.services.results import RunResultQuery
        self.engines.register("fake", FakeStreamingEngine())
        run = await self.submit("fake")
        await self.manager.shutdown()
        run.status = RunStatus.RUNNING
        run.ended_at = None
        self.manager.runs.save(run)
        self.assertEqual(self.projects.results.list(self.project), [])
        self.assertEqual(self.projects.results.list(self.project, include_running=True)[0].status, RunStatus.RUNNING)
        self.assertIsNone(self.tasks.results.load(self.task, run.id).duration_seconds)
        calls = []
        class Reader:
            def load(self, task, run_id):
                calls.append((task.id, run_id))
                return run
            def list(self, task):
                return [run]
        query = RunResultQuery(self.tasks, Reader())
        self.assertEqual(query.load(self.task, run.id).run_id, run.id)
        self.assertEqual(calls, [(self.task.id, run.id)])
        self.assertTrue(get_type_hints(RunResultQuery.__init__))

    async def test_legacy_configuration_and_custom_fields_migrate_on_load(self):
        path = self.project.paths.root / "project.json"
        data = read_json(path)
        data["config"] = {"model": "old-model", "temperature": 0.1, "default_engine": "loop",
                          "credential_ref": "env:OLD", "custom": {"mode": "personal"}}
        atomic_json(path, data)
        migrated = self.projects.load(self.project.id)
        self.assertEqual(migrated.config.completion, {"model": "old-model", "temperature": 0.1})
        self.assertEqual(migrated.config["custom"], {"mode": "personal"})
        self.projects.save(migrated)
        self.assertEqual(read_json(path)["config"]["credential_ref"], "env:OLD")

    async def test_configuration_validation_runs_before_create_and_save(self):
        for data in ({"bad": object()}, {1: "bad"}, {"n": float("nan")}):
            with self.subTest(data=data), self.assertRaises((TypeError, ValueError)):
                ProjectConfig(data=data)
        self.project.config["custom_flag"] = True
        self.projects.save(self.project)
        self.assertTrue(self.projects.load(self.project.id).config["custom_flag"])
        self.task.config = {"engines": []}
        with self.assertRaises(TypeError):
            self.tasks.save(self.task)
