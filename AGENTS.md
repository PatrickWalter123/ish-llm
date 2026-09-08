# AI TUI Development Instructions

## Project Overview

This repository implements a production-oriented AI TUI client for Linux using Python, LiteLLM, asyncio, and prompt-toolkit.

The application is designed around long-lived projects, concurrent AI tasks, durable conversation history, interruptible execution, and multiple execution engines.

## Core Architecture

The primary hierarchy is:

Project
→ Task
→ Run
→ Engine
→ Step

Conversation messages belong to Task and are persisted separately through ConversationStore.

### Project

A Project is a persistent AI workspace.

A Project owns:

* configuration
* model configuration
* project-level memory
* tools
* workflows
* tasks
* project paths

Project objects must not directly execute LLM calls, tools, workflows, or Runs.

### Task

A Task is one independent long-lived AI session inside a Project.

A Task owns persistent session state such as:

* title
* status
* current Run ID
* Task metadata
* Task paths

Task objects must not contain runtime-only objects such as:

* asyncio.Task
* asyncio.Queue
* locks
* live provider connections

### Message

Messages represent conversation history.

Conversation persistence is handled by ConversationStore using append-only JSONL events.

Important message states include:

* queued
* committed
* streaming
* completed
* interrupted
* cancelled
* failed

Queued user requests must be durable.

An asyncio.Queue may be used for runtime scheduling, but the persisted queued Message is the source of truth.

### Run

A Run represents one actual Engine execution for one committed user request.

Only one Run may execute at a time inside a Task.

Different Tasks may execute concurrently.

A Run stores:

* input message ID
* assistant message ID
* Engine name
* status
* timing
* error
* metadata

Do not automatically retry stale Runs after process restart because previous Steps may have produced side effects.

### Engine

Engine defines how a Run is executed.

Current intended Engine types are:

* SingleEngine
* LoopEngine
* GraphEngine

Engine implementations should expose an async event stream.

Engine implementations must not directly write Run, Step, Task, or conversation persistence files.

They communicate execution progress through EngineEvent.

### Reusable Inference

Embedding and reranking belong to ish/inference as reusable model calls, not
Engine strategies. They may be used by Engines, Tools and RAG components and must
not own Run/Step lifecycle or write domain persistence. Execution observations
belong to Run; Project/Task queries read Runs without duplicating result files.

### Step

A Step is an observable execution unit inside a Run.

Examples:

* llm
* tool
* shell
* retrieval
* rerank
* graph_node

StepManager owns Step lifecycle and persistence.

Engine Step events are converted into persistent Steps through StepEventRecorder.

## Service Responsibilities

### ProjectManager

Responsible for Project lifecycle orchestration:

* create
* load
* save
* list
* clone
* import/export
* soft delete
* restore
* backup
* migration coordination
* component initialization coordination

ProjectManager must not know the internal directory layout of Memory, Workflow, Tool, or Task components.

### TaskManager

Responsible for Task lifecycle:

* initialize
* create
* load
* save
* list
* clone
* soft delete
* restore
* cleanup

TaskManager must not execute Engines.

### ConversationStore

Responsible for conversation persistence.

Conversation storage is append-only JSONL.

Supported event concepts include:

* message.create
* message.delta
* message.status
* message.metadata
* message.run

Do not rewrite the entire conversation file during streaming.

### RunManager

Responsible for runtime orchestration.

RunManager:

* accepts user requests
* durably records requests as QUEUED
* is constructed for exactly one Task and maintains its runtime queue
* creates Runs
* promotes queued requests to COMMITTED
* executes Engines
* persists Assistant streaming output
* handles interrupt
* recovers queued requests after restart
* validates Engine/component registrations before admitting new requests
* publishes Run lifecycle events after persistence, separately from EngineEvent

Runs inside one Task are serial.

Runs belonging to different Tasks may execute concurrently.

### StepManager

Responsible only for Step lifecycle and persistence:

* create
* start
* complete
* fail
* interrupt
* cancel
* load
* list
* recover

StepManager must not perform Tool, LLM, Shell, Retrieval, or Graph execution itself.

## Persistence Rules

Use atomic replacement for mutable JSON metadata files.

Examples:

* project.json
* task.json
* run.json
* step.json

Conversation history uses append-only JSONL.

Never persist runtime asyncio objects.

ProjectConfig and component records accept arbitrary JSON field names. There is
no application secret store, key-name blocking or exception-message masking.
Operational logs describe lifecycle operations; runtime handles are not persisted.

## Runtime Rules

User input must first be durably persisted as QUEUED.

When a Run begins:

QUEUED → COMMITTED

When a Run is already executing, additional input stays QUEUED.

Normal input must not interrupt the current Run.

Explicit interrupt should:

1. interrupt only the active Run
2. preserve queued messages
3. mark the partial Assistant response INTERRUPTED
4. allow the Task worker to continue with the next queued message

After process restart:

* stale RUNNING Run → INTERRUPTED
* stale RUNNING/PENDING Step → INTERRUPTED
* stale STREAMING Assistant message → INTERRUPTED
* QUEUED user messages → restored to runtime queue

Never automatically replay a stale Run that may have produced side effects.

## Dependency Direction

Prefer:

TUI
→ Services
→ Core domain models

Core models must not import prompt-toolkit.

Core models must not depend on TUI components.

Engine implementations must not depend directly on prompt-toolkit.

Persistence and UI should communicate through service APIs and events.

## Paths

ProjectPaths defines only major Project-level locations.

TaskPaths defines only major Task-level locations.

RunPaths defines only major Run-level locations.

StepPaths defines only major Step-level locations.

Do not centralize every nested component path inside ProjectPaths.

Each subsystem owns the structure beneath its own root directory.

## Project Components

Components explicitly declare their Project-root directory and own configuration,
open JSON definitions and storage lifecycle beneath it. Use the Component base for
common CRUD/codecs/cloning, and ComponentData through ProjectManager for locked,
lifecycle-checked access. Base and registry must not depend on Tool execution.
Domain-specific runtime capability adapters live with their own components.
Declare capability names and resolve only the requested capability. Tool data CRUD
must not require runtime handlers; bind handlers only for execution.
Workflow/Subagent records are data, not a new execution hierarchy; graph/subagent
execution stays within the owning Run and uses Engine events for Steps.

## Code Style

Target Python 3.9 or newer. Maintain compatibility with Python 3.9.13 and newer
3.9 patch releases as requested by the user. Use `ish.compat` for version-specific
APIs; run tests with both Python 3.9 and the available modern interpreter.

Use:

* type hints
* dataclasses
* slots=True where appropriate (native slots on 3.10+, regular dataclasses on 3.9)
* StrEnum for persisted enum values
* pathlib.Path
* async/await for runtime execution

Prefer explicit responsibilities over large manager classes.

Avoid global mutable state.

Keep persisted domain state separate from runtime state.

## Tests

Before considering a change complete, run the repository test suite.

Important integration cases include:

1. single user request
2. streaming Assistant response
3. second request queued during streaming
4. explicit interrupt
5. multiple concurrent Tasks
6. process-restart recovery
7. stale Run recovery
8. stale Step recovery
9. soft-delete and restore
10. Project/Task clone behavior

Do not substantially change architectural boundaries without updating `docs/architecture.md`.
