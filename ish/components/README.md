# Component boundaries

This package is the home for reusable capabilities consumed by Loop, Single,
and Graph engines. It is separate from execution strategies in `ish/engines`.

| Package | Intended responsibility | Current state |
| --- | --- | --- |
| tools | Tool definitions, registry, future persistent CRUD | Existing runtime Tool/ToolRegistry moved here |
| rag | Retrieval collections, indexes, configuration CRUD, retrieval adapters | Package reserved |
| mcp | Server definitions, configuration CRUD, connection lifecycle | Package reserved |
| skills | Skill definitions, CRUD, resolution | Package reserved |
| subagents | Sub-agent definitions, CRUD, execution adapters | Package reserved |
| workflows | Workflow definitions, CRUD, graph validation/loading | Package reserved |

For each future component, keep serializable definitions in `models.py`, storage
in a repository, CRUD/lifecycle policy in a manager, and live connections or
execution in runtime adapters. Add only the files a concrete implementation
needs. No placeholder CRUD classes claim functionality that is not implemented.

Managers may implement the ProjectInitializer protocol. A subsystem owns the
layout beneath its assigned Project root; do not add nested paths to
ProjectPaths or execution to ProjectManager. Work inside an Engine remains
observable through EngineEvent and StepManager. Secret values belong in a secret
store, not component configuration. Engine sub-agents remain execution units
within their owning Run unless the domain model is explicitly redesigned.
