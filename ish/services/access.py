"""Authoritative Project lifecycle checks shared by application services."""

from typing import Protocol

from ish.core.models import Project
from ish.core.paths import ProjectPaths


class ProjectReader(Protocol):
    def paths(self, project_id: str) -> ProjectPaths: ...
    def load(self, project_id: str) -> Project: ...


class ProjectAccess:
    def __init__(self, repository: ProjectReader) -> None:
        self.repository = repository

    def load(self, project_id: str, *, allow_deleted: bool = False) -> Project:
        current = self.repository.load(project_id)
        if current.deleted and not allow_deleted:
            raise ValueError("Project is deleted")
        return current

    def require(self, project: Project, *, allow_deleted: bool = False) -> Project:
        expected = self.repository.paths(project.id)
        if project.paths.root.absolute() != expected.root.absolute():
            raise ValueError("Project path ownership mismatch")
        return self.load(project.id, allow_deleted=allow_deleted)
