"""Workflow directory initialization; workflow CRUD/Graph execution are future work."""

from pathlib import Path
from ish.compat import dataclass
from ish.core.models import Project
from ish.components.base import Component


@dataclass(frozen=True, slots=True)
class WorkflowPaths:
    root: Path

    @classmethod
    def for_project(cls, project: Project) -> "WorkflowPaths":
        return cls(project.paths.root / "workflows")


class WorkflowComponent(Component):
    name = "workflows"

    def initialize(self, project: Project) -> None:
        WorkflowPaths.for_project(project).root.mkdir(parents=True, exist_ok=True)
