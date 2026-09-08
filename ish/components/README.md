# Project components

Components own Project-root directories and persistent definitions. They do not
execute Runs. `Component` in `base.py` provides directory initialization/removal,
open JSON serialization/deserialization, configuration, definition CRUD and clone.
`ComponentRegistry` registers identities/directories and collects generic optional
capability resolution; neither module imports Tools or requires `resolve_tools`.

| Package | Persisted data | Runtime responsibility |
| --- | --- | --- |
| tools | Enabled names and optional native function-tool definitions | Bind definitions to explicitly registered Python handlers |
| subagents | Specialized LLM settings, prompts and arbitrary application options | Consumed by developer workflow/Engine/Tool code; no automatic execution |
| workflows | Open graph documents such as nodes, edges and subagent references | Graph validation/execution belongs to the future GraphEngine |
| rag, mcp, skills | Packages reserved; inherit Component when implemented | Indexes/connections/resolution need domain-specific adapters |

## Declare a component

```python
from ish.components import Component, ComponentRegistry

class NotesComponent(Component):
    name = "notes"
    directory = "knowledge"  # Explicit direct child of the Project root.

    def default_configuration(self):
        return {"format_version": 1}

components = ComponentRegistry((NotesComponent(),))
```

Registration rejects missing/unsafe directory declarations, Windows device names,
core-owned tasks/logs/state/cache directories and duplicate directory ownership.
Each component owns everything beneath its declared root. Paths remain out of
ProjectPaths. Base file operations reject symlinks/junctions and linked ancestors;
permanent removal checks every descendant. This assumes cooperative exclusive
workspace ownership, not hostile external filesystem mutation. Component code is
trusted Python and can override the contract; it is not sandboxed.

Default persistence:

```text
<project>/
  project.json                      # Selected component names only
  knowledge/
    component.json                  # Open configuration object
    records/
      <id>.json                     # One open definition per file
  subagents/
    component.json
    records/reviewer.json           # Model/settings/prompt chosen by the app
  workflows/
    component.json
    records/review.json             # Nodes/edges/reference keys chosen by the app
  tools/
    component.json                  # {"enabled": ["add"], ...}
    records/add.json                # Optional native function-tool definition
```

Only selected components are initialized. Records are plain dicts, not fixed
schema dataclasses; unknown keys survive load/save and clone. `serialize(dict)`
returns JSON text and `deserialize(text)` returns a detached dict. Values must be
JSON-safe, string-keyed and finite; runtime objects are rejected. Field names are
unrestricted, including names used in schema definitions. ProjectConfig uses the
same JSON compatibility checks without an application key-name blacklist. There is no automatic migration or
schema-version field: components may add their own and override validation/codecs.
`validate_configuration` and `validate_record` are optional semantic hooks.

## Use through ProjectManager

```python
# Configure ProjectManager with the registry once.
project = projects.create("Example", components=("notes",))
notes = projects.component(project, "notes")
identifier = notes.create({"title": "Example", "tags": ["draft"]}, identifier="intro")
notes.update(identifier, {"new_option": True})
value = notes.load(identifier)
all_values = notes.list()                 # {record_id: dict, ...}
notes.save(identifier, {"replacement": True})
notes.delete(identifier)
notes.configure({"arbitrary_setting": {"enabled": True}})
configuration = notes.configuration()

projects.set_components(project, ("notes", "subagents", "workflows"))  # Register first.
projects.remove_component(project, "notes")                 # Disable, retain data.
projects.remove_component(project, "notes", permanent=True) # Remove retained tree.
```

`create` rejects duplicate IDs; `save` replaces an existing record; `update` is a
shallow key update (nested values replace); `delete` removes one record. Configure
replaces the configuration dict. To remove a key, load/pop/save. Reads return
independent values. Mutable JSON uses atomic replacement and fsync; updates hold
the workspace lock over the complete read/modify/write operation.

The ComponentData handle reloads authoritative Project state and selection on
every call, so stale handles cannot modify a deleted or disabled Project/component.
Direct Component/ComponentRegistry APIs are lower-level and require the caller's
workspace ownership scope. In an async UI, use `StorageIO(projects.ownership).run`
for these synchronous APIs. Runtime capabilities are resolved in RunManager's
existing ordered storage lane.

`set_components` initializes missing directories/configuration before publishing
the new selection, preserving previous data on re-enable. Initialization must be
idempotent; overridden initializers should call `super().initialize(project)`.
Failure may retain partial directories, but does not publish the changed selection.
Permanent removal requires all Project Tasks to be inactive/detached. It publishes
disabled selection before recursive deletion; failed/interrupted removal leaves
it disabled and can be retried, including when already disabled. Removal is not
a multi-file transaction. Deleting Project removes its entire component tree.

Cloning copies component.json and records into the new Project, retaining record
IDs so graph references stay valid. It does not copy unregistered artifacts,
indexes, connections or runtime handlers. Override clone for a component-specific
artifact policy. Old tools/component.json remains supported; legacy directory-only
workflows read as empty configuration without a source write. Old arbitrary
workflow files remain untouched; there is no implicit graph import.

## Tool specialization and runtime capabilities

```python
tools = projects.component(project, "tools")
tools.create({
    "type": "function",
    "function": {
        "name": "add", "description": "Add numbers",
        "parameters": {"type": "object", "properties": {}},
        "strict": True,
    },
})  # Uses the function name as its record ID; the handler is only needed for execution.
tools.configure({"enabled": ["add"], "custom_policy": {"label": "example"}})
```

Tool definitions preserve provider extension keys. Function identity and argument
schema are validated independently of the application catalog. CRUD, configuration
and clone work with an empty catalog; handlers are required only for execution. Missing
record overrides use the catalog definition, preserving old enabled-name files.
Disable a tool before deleting its override. Saving JSON never imports code or
creates a handler; startup must register handlers again.

ToolComponent declares capabilities = ("tools",) and resolves a fresh ToolRegistry
only when tools is requested. ComponentToolResolver in tools/resolver.py collects
and validates that result.
RunManager still accepts `capabilities=components` and wraps it with this adapter;
custom resolvers with resolve_tools continue to work. For direct resolution use
`ComponentToolResolver(components).resolve_tools(project)` instead of the removed
ComponentRegistry.resolve_tools API. Other capabilities use arbitrary keys and
values through `components.resolve(project, "retriever")`. Declare a tuple of
capability names and implement resolve(project, capability); the registry skips
components that do not declare the requested name. Base capabilities is empty.

The EngineContext tool snapshot stays fixed for one Run. Definition/configuration
edits affect later Runs. Engine subagent/graph execution and Steps remain inside
the owning Run; components own data only. Tool transcripts are still runtime-only.
Reusable embedding/rerank calls remain in ish/inference, with RAG/index persistence
owned by components and execution observations explicitly reported by Engines.
