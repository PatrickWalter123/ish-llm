"""Open workflow graph definitions; execution belongs to Graph engines."""

from pathlib import Path
from ish.compat import dataclass
from ish.core.models import Project
from ish.components.base import Component


@dataclass(frozen=True, slots=True)
class WorkflowPaths:
    root: Path

    @classmethod
    def for_project(cls, project: Project) -> "WorkflowPaths":
        return cls(project.paths.root / WorkflowComponent.directory)


class WorkflowComponent(Component):
    name = "workflows"
    directory = "workflows"

    def configuration(self, project: Project) -> dict:
        # Earlier WorkflowComponent created an empty directory only. Reading or
        # cloning that format must not require a write or discard old artifacts.
        if not self._configuration_path(project).exists() and self.root(project).is_dir():
            return self.default_configuration()
        return super().configuration(project)
