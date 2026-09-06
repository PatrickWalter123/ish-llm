# Component boundaries

This package is the home for reusable capabilities consumed by Loop, Single,
and Graph engines. It is separate from execution strategies in `ish/engines`.

| Package | Intended responsibility | Current state |
| --- | --- | --- |
| tools | Tool catalog and Project enabled-tool configuration | ToolRegistry, ToolComponent, ToolPaths implemented; handlers remain runtime-only |
| rag | Retrieval collections, indexes, configuration CRUD, retrieval adapters | Package reserved |
| mcp | Server definitions, configuration CRUD, connection lifecycle | Package reserved |
| skills | Skill definitions, CRUD, resolution | Package reserved |
| subagents | Sub-agent definitions, CRUD, execution adapters | Package reserved |
| workflows | Workflow definitions, CRUD, graph validation/loading | WorkflowComponent/WorkflowPaths initialize a directory; definition CRUD/Graph execution remain planned |

For each future component, keep serializable definitions in `models.py`, storage
in a repository, CRUD/lifecycle policy in a manager, and live connections or
execution in runtime adapters. Add only the files a concrete implementation
needs. No placeholder CRUD classes claim functionality that is not implemented.

Components implement `ProjectComponent` from `base.py` and are registered in
ComponentRegistry. Select their names through ProjectManager.create's
`components=` argument. Configuration changes go through
ProjectManager.configure_component, which checks current Project state first.
ProjectManager.set_components changes selection without deleting old data.
Initialize idempotently; runtime capability resolution must return fresh
registries and must not initialize or modify persisted configuration.

A subsystem owns the layout beneath its Project root; do not add component paths to
ProjectPaths or execution to ProjectManager. Work inside an Engine remains
observable through EngineEvent and StepManager. Secret values belong in a secret
store, not component configuration. Engine sub-agents remain execution units
within their owning Run unless the domain model is explicitly redesigned.

The same configured ComponentRegistry is passed to RunManager as `capabilities=`.
It resolves only the Project's selected components and configured tool names.
This defines application-level tool availability; it does not sandbox the Python
handler or replace operating-system permissions. Tool calls/results remain a
transient per-Run transcript until structured conversation events are added.
