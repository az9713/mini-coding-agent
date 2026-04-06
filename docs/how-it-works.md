# How mini-coding-agent Works

This document is the architecture overview for `mini_coding_agent.py`. It
explains every major structural decision and how the six logical components
relate to each other. Read this first if you want to understand the design
before reading the source.

---

## The Monolith Rationale

The entire agent — REPL, tools, session storage, LLM client, prompt
assembly, context compression — lives in a single file of roughly 1,345
lines. That is a deliberate choice, not an oversight.

Sebastian Raschka designed the project as an **educational artifact**: a
complete, working coding agent that a student can fully understand in one
sitting. When everything is in one file, a single `grep` finds every
definition, every call site, and every constant. There is no import graph to
trace, no package hierarchy to navigate, and no need to switch between
directories to follow a code path. The whole mental model fits on one screen.

The tradeoff is explicit: this layout does not scale to a production agent
with dozens of tools and multiple model backends. But for learning purposes,
a monolith is the right call. You see the whole system at once.

> **Note:** The file opens with a comment block that names all six
> components. That comment is the table of contents. Keep it open in a split
> view while reading the code.

Despite living in one file, the code has six clearly defined logical
components. The sections below describe each one. Understanding their
boundaries is the key to understanding the agent.

---

## Component Overview

The diagram below shows how the six components are arranged inside
`MiniAgent` and how they connect to each other at runtime.

```
+------------------------------------------------------------------+
|                          MiniAgent                               |
|                                                                  |
|  +------------------+        +-----------------------------+     |
|  | 1) WorkspaceCtx  |        | 2) Prompt Assembly          |     |
|  |                  |        |                             |     |
|  | - git branch     +------->| build_prefix()  (static)    |     |
|  | - git status     |        | memory_text()   (per-turn)  |     |
|  | - recent commits |        | history_text()  (per-turn)  |     |
|  | - project docs   |        | prompt()        (assembled) |     |
|  +------------------+        +-------------+---------------+     |
|                                            |                     |
|                                            v                     |
|  +-------------------------+    +----------+------------------+  |
|  | 3) Tools & Permissions  |<---| OllamaModelClient           |  |
|  |                         |    | .complete(prompt, tokens)   |  |
|  | build_tools()           |    +-----------------------------+  |
|  | validate_tool()         |                                     |
|  | approve()               |    +-----------------------------+  |
|  | run_tool()              +--->| 4) Context Reduction        |  |
|  | parse()                 |    |                             |  |
|  +-------------------------+    | clip()                      |  |
|                                 | history_text()              |  |
|  +-------------------------+    +-----------------------------+  |
|  | 5) Session Memory       |                                     |
|  |                         |    +-----------------------------+  |
|  | SessionStore (JSON)     |    | 6) Delegation               |  |
|  | CheckpointStore (JSON)  |    |                             |  |
|  | record()                |    | tool_delegate()             |  |
|  | note_tool()             |    | child MiniAgent (depth+1)   |  |
|  | remember()              |    | read_only=True              |  |
|  | ask()  [main loop]      +--->| approval_policy="never"     |  |
|  +-------------------------+    | checkpoint_store=None       |  |
|                                 +-----------------------------+  |
+------------------------------------------------------------------+
```

Every user message travels top-to-bottom through this diagram:

1. The workspace snapshot (component 1) is baked into the static prefix
   (component 2) at startup.
2. Each turn, `prompt()` assembles the static prefix plus dynamic memory and
   history and sends the result to Ollama. With `verbose=True`, tokens are
   printed as they arrive via the `on_token` streaming callback.
3. The model response is parsed (component 3) into a tool call or a final
   answer.
4. If it is a tool call, the tool output is clipped (component 4) and
   appended to the session (component 5). With `--auto-verify`, file-writing
   tools also run the project's test suite and append the result.
5. If delegation is requested, a child agent is spawned (component 6) and its
   result is returned as a tool output.

---

## Component 1: Live Repo Context (`WorkspaceContext`)

**`WorkspaceContext`** is a data class that captures a snapshot of the
repository at the moment the agent starts. It runs a small set of `git`
subprocesses and reads up to four documentation files. The result is a
compact text block that becomes part of the static system prompt.

### What it captures

| Field | Source | Purpose |
|---|---|---|
| `cwd` | `Path(cwd).resolve()` | Absolute working directory |
| `repo_root` | `git rev-parse --show-toplevel` | Anchor for all path operations |
| `branch` | `git branch --show-current` | Current branch name |
| `default_branch` | `git symbolic-ref refs/remotes/origin/HEAD` | Upstream default (usually `main`) |
| `status` | `git status --short` | Uncommitted changes (clipped to 1,500 chars) |
| `recent_commits` | `git log --oneline -5` | Last five commit summaries |
| `project_docs` | Files from `DOC_NAMES` | Human-readable project context |

### Project documentation

The agent reads any of the following files it finds under both `repo_root`
and `cwd`:

```python
DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
```

Each file is clipped to **1,200 characters** before being stored. If the
same relative path appears under both `repo_root` and `cwd`, the
`repo_root` copy wins and the duplicate is skipped. The resulting
`project_docs` dict maps relative paths to their clipped text.

### Why this matters

Without the workspace snapshot, the model would need to call `list_files`
and `read_file` just to orient itself. By embedding git state and key docs
into the static prefix, the agent gives the model **situational awareness
before the first tool call**. The model knows which branch it is on, whether
there are uncommitted changes, and what the project does — all from the
prompt alone.

### The `text()` method

`WorkspaceContext.text()` serialises all fields into a plain-text block:

```
Workspace:
- cwd: /home/user/my-project
- repo_root: /home/user/my-project
- branch: main
- default_branch: main
- status:
M  src/utils.py
- recent_commits:
- a1b2c3d fix off-by-one in binary search
- 9f8e7d6 add test for edge case
- project_docs:
- README.md
...clipped content...
```

This block is embedded verbatim into `build_prefix()` and never changes
during the session.

---

## Component 2: Prompt Shape and Cache Reuse

Every call to the model uses the same three-part prompt structure:

```
+------------------------------------------------+
|  PREFIX  (static, built once at startup)       |
|                                                |
|  - System persona and rules                    |
|  - Tool definitions with risk flags            |
|  - Example tool call formats (JSON and XML)    |
|  - WorkspaceContext.text()                     |
+------------------------------------------------+
|  MEMORY  (dynamic, changes when memory mutates)|
|                                                |
|  - Current task description                    |
|  - Recently accessed file paths (up to 8)     |
|  - Recent notes (up to 5 entries)              |
+------------------------------------------------+
|  TRANSCRIPT  (dynamic, grows each turn)        |
|                                                |
|  - Compressed turn history                     |
|  - [user], [assistant], [tool:name] entries    |
+------------------------------------------------+
|  CURRENT USER REQUEST  (the active message)    |
+------------------------------------------------+
```

### The static prefix

`build_prefix()` is called **once** in `__init__` and stored as
`self.prefix`. It never changes for the lifetime of the session. It
contains:

- The agent persona and output format rules.
- The full tool listing with signatures and risk flags, rendered from
  `self.tools` at startup.
- Four concrete examples of valid tool call formats.
- The `WorkspaceContext.text()` block.

The prefix is the largest section. Because it never changes, any inference
engine that supports **KV-cache prefix reuse** can cache the attention state
for those tokens and skip recomputing them on every turn. Even without
hardware-level caching, a consistent prefix layout helps
instruction-following models: the rules and tools are always at the same
position, so the model learns where to look.

Two optional additions can appear at the end of the prefix:

- **Persistent memory** (`AGENT_MEMORY.md`): If `AGENT_MEMORY.md` exists in
  the workspace root, its contents are injected as a `Persistent memory`
  section. The model can write new entries with `update_memory(note)`; the
  file survives `/reset` and new sessions.
- **Plan rule**: If `plan_mode=True` (set by `--plan`), an additional rule
  is appended instructing the model to emit a `<plan>` block listing numbered
  steps before using any tools. The plan must be confirmed before execution
  begins; the `<plan>` step does not count against `--max-steps`.

### Dynamic sections

`memory_text()` returns the current task, file list, and notes as a compact
block. It is reconstructed from `self.session["memory"]` on every call, so
it always reflects the latest state.

`history_text()` compresses the full turn history into a string bounded by
`MAX_HISTORY = 12000` characters. See Component 4 for details on how that
compression works.

### The `prompt()` method

`prompt(user_message)` assembles all four sections using `textwrap.dedent`:

```python
def prompt(self, user_message):
    return textwrap.dedent(f"""
        {self.prefix}

        {self.memory_text()}

        Transcript:
        {self.history_text()}

        Current user request:
        {user_message}
    """).strip()
```

The same `user_message` string is passed unchanged through all tool-call
iterations for a single user request. The model always sees the original
question at the bottom, no matter how many tool turns have elapsed.

---

## Component 3: Structured Tools, Validation, and Permissions

The agent exposes seven tools permanently and an eighth (`delegate`) only when
the agent is not already at maximum delegation depth.

### The tool table

| Tool | Risk | Key arguments | Default |
|---|---|---|---|
| `list_files` | safe | `path` | `'.'` |
| `read_file` | safe | `path`, `start`, `end` | `start=1`, `end=200` |
| `search` | safe | `pattern`, `path` | `path='.'` |
| `run_shell` | **risky** | `command`, `timeout` | `timeout=20` |
| `write_file` | **risky** | `path`, `content` | — |
| `patch_file` | **risky** | `path`, `old_text`, `new_text` | — |
| `update_memory` | safe | `note` | — |
| `delegate` | safe | `task`, `max_steps` | `max_steps=3` |

`delegate` is absent when `self.depth >= self.max_depth`, which prevents
infinite recursion. `update_memory` appends a dated bullet to `AGENT_MEMORY.md`
in the workspace root; the file is read back into the prefix on the next session
start. All other tools are always available.

### Two-phase execution

Every tool call goes through two gates before the run function is called:

```
Model output
     |
     v
  parse()           -- extract tool name and args from raw text
     |
     v
  validate_tool()   -- type-check args, check path bounds, enforce match count
     |
     v
  approve()         -- check approval policy and optionally prompt the user
     |
     v
  tool["run"](args) -- execute the tool function
     |
     v
  clip(result)      -- truncate output to MAX_TOOL_OUTPUT chars
```

**Validation** is strict and type-aware. For `run_shell`, the timeout must
be in `[1, 120]`. For `patch_file`, `old_text` must occur exactly once (see
the design decisions document for why). For all path-taking tools, `path()`
resolves the path and raises `ValueError` if it escapes `repo_root`.

**Approval** is controlled by `approval_policy`:

| Policy | Behaviour |
|---|---|
| `"ask"` | Prompts the user with `[y/N]` for every risky tool call |
| `"auto"` | Approves all risky tool calls silently |
| `"never"` | Denies all risky tool calls |

If `self.read_only` is `True` (always the case for child agents), `approve()`
returns `False` immediately regardless of the policy.

### Parsing: JSON-first, XML fallback

The model is instructed to emit `<tool>...</tool>` or `<final>...</final>`.
`parse()` handles both JSON and XML formats because different models format
tool calls differently.

```
Raw model output
      |
      +-- contains "<plan>" (before any "<tool>") ?
      |         |
      |         +-- non-empty body -> ("plan", plan_text)
      |         +-- empty body     -> ("retry", notice)
      |
      +-- contains "<tool>" ?
      |         |
      |         +-- try json.loads(body)
      |         |       |
      |         |       +-- success -> ("tool", payload)
      |         |       +-- failure -> ("retry", notice)
      |         |
      |         +-- try parse_xml_tool()
      |                 |
      |                 +-- success -> ("tool", payload)
      |                 +-- failure -> ("retry", notice)
      |
      +-- contains "<final>" ?
      |         |
      |         +-- extract body -> ("final", text)
      |
      +-- non-empty raw text -> ("final", raw)
      +-- empty              -> ("retry", notice)
```

**JSON format** (`<tool>` with no attributes):

```
<tool>{"name":"list_files","args":{"path":"."}}</tool>
```

**XML format** (`<tool` with attributes, used for multi-line content):

```
<tool name="write_file" path="binary_search.py">
<content>
def binary_search(nums, target):
    lo, hi = 0, len(nums) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if nums[mid] == target:
            return mid
        elif nums[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1
</content>
</tool>
```

The XML format sidesteps JSON escaping for multi-line strings, which is the
primary reason it exists. `parse_xml_tool()` extracts named attributes from
the opening tag and named sub-elements from the body.

### Repeated call detection

If the two most recent tool history entries have identical `name` and `args`,
`run_tool()` returns an error message instead of executing the tool again.
This prevents the model from getting stuck in a loop calling the same tool
repeatedly without making progress.

---

## Component 4: Context Reduction and Output Management

Local models have limited context windows. Without active compression, a
multi-turn session would eventually overflow. Component 4 has two
mechanisms: output clipping and history compression.

### Output clipping: `clip()`

```python
def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
```

`clip()` is called in two places:

1. After every tool execution: `clip(tool["run"](args))` with the default
   `MAX_TOOL_OUTPUT = 4000` char limit.
2. Inside `history_text()` for each individual history item.

When output is truncated, the model sees exactly how many characters were
dropped. This matters because the model may decide to re-read with a
narrower line range rather than working from incomplete data.

### History compression: `history_text()`

`history_text()` applies a two-tier compression strategy before assembling
the transcript string:

```
All history items
       |
       +-- index >= (len - 6)  -->  RECENT TIER
       |                              - tool output: 900 chars max
       |                              - message:     900 chars max
       |
       +-- index < (len - 6)   -->  OLDER TIER
                                      - tool output: 180 chars max
                                      - message:     220 chars max
                                      - read_file duplicates: DROPPED
```

**Deduplication of reads:** For older items only, if the same file path
appears in multiple `read_file` tool calls, only the first occurrence is kept.
The agent commonly reads a file, edits it, then reads it again to verify the
change. By the time that third read is several turns back, the model does not
need it at all — the current state is what matters.

The assembled string is then passed through one final `clip(..., MAX_HISTORY)`
call with `MAX_HISTORY = 12000`. This is the hard ceiling for the entire
transcript section.

The tradeoff is explicit: older context is aggressively compressed. This
keeps token usage predictable at the cost of losing detail for long sessions.
Recent history, where the model is most likely to need full fidelity, is
preserved at nearly full length.

---

## Component 5: Session Memory and Transcripts

**`SessionStore`** manages the on-disk session state. Each session is a
single JSON file stored at:

```
<repo_root>/.mini-coding-agent/sessions/<session-id>.json
```

### Session JSON structure

```json
{
  "id": "20260401-144025-2dd0aa",
  "created_at": "2026-04-01T14:40:25.123456+00:00",
  "workspace_root": "/home/user/my-project",
  "history": [
    {
      "role": "user",
      "content": "add a binary search function",
      "created_at": "2026-04-01T14:40:26.000000+00:00"
    },
    {
      "role": "tool",
      "name": "write_file",
      "args": {"path": "search.py", "content": "..."},
      "content": "wrote search.py (312 chars)",
      "created_at": "2026-04-01T14:40:28.000000+00:00"
    },
    {
      "role": "assistant",
      "content": "Done. The function is in search.py.",
      "created_at": "2026-04-01T14:40:29.000000+00:00"
    }
  ],
  "memory": {
    "task": "add a binary search function",
    "files": ["search.py"],
    "notes": ["write_file: wrote search.py (312 chars)"]
  }
}
```

### The `memory` object

The `memory` object is a **distilled summary** that persists across turns and
is shown to the model at every step via `memory_text()`:

| Field | Type | Capacity | Purpose |
|---|---|---|---|
| `task` | string | 300 chars (clipped) | First user message of the session |
| `files` | list of strings | 8 entries (LRU) | Paths accessed by file tools |
| `notes` | list of strings | 5 entries (LRU) | Recent tool results and assistant replies |

### LRU-style `remember()`

`remember(bucket, item, limit)` implements a simple **LRU deduplication**:

```python
@staticmethod
def remember(bucket, item, limit):
    if not item:
        return
    if item in bucket:
        bucket.remove(item)   # remove existing occurrence
    bucket.append(item)       # add to end (most recent)
    del bucket[:-limit]       # trim from front (oldest)
```

If the item is already in the list, it is moved to the end rather than
duplicated. If the list exceeds `limit`, the oldest entries are dropped from
the front. The result is an ordered list where position zero is the oldest
and position `-1` is the most recent.

### Automatic path tracking: `note_tool()`

After every successful tool execution, `note_tool(name, args, result)` is
called:

```python
def note_tool(self, name, args, result):
    memory = self.session["memory"]
    path = args.get("path")
    if name in {"read_file", "write_file", "patch_file"} and path:
        self.remember(memory["files"], str(path), 8)
    note = f"{name}: {clip(str(result).replace(chr(10), ' '), 220)}"
    self.remember(memory["notes"], note, 5)
```

File paths from `read_file`, `write_file`, and `patch_file` are
automatically added to `memory["files"]`. Every tool result (condensed to a
single line, 220 chars max) is added to `memory["notes"]`. This means the
model's working memory is maintained automatically without the model needing
to explicitly request that anything be saved.

### Session resumption

`SessionStore.latest()` sorts session files by `mtime` and returns the stem
of the most recently modified file. On resume, `MiniAgent.from_session()`
loads the JSON and passes it to `__init__` as the `session` argument.
`build_prefix()` is called fresh (the workspace snapshot is re-taken), but
the full `history` and `memory` are restored from disk.

---

## Component 6: Delegation and Bounded Subagents

**`tool_delegate()`** allows the main agent to spawn a child `MiniAgent` for
a focused investigation sub-task. Delegation is the only tool that creates
another agent instance.

### Child agent configuration

```python
child = MiniAgent(
    model_client=self.model_client,   # same Ollama connection
    workspace=self.workspace,          # same repo snapshot
    session_store=self.session_store,  # writes its own session file
    approval_policy="never",           # risky tools auto-denied
    max_steps=int(args.get("max_steps", 3)),
    max_new_tokens=self.max_new_tokens,
    depth=self.depth + 1,              # increments depth counter
    max_depth=self.max_depth,
    read_only=True,                    # approve() always returns False
)
```

The child inherits the model connection and workspace snapshot. All other
state is fresh: a new session file, empty history, and an empty memory
object. Context from the parent is passed explicitly:

```python
child.session["memory"]["task"] = task
child.session["memory"]["notes"] = [clip(self.history_text(), 300)]
```

The parent's compressed history is injected as a note in the child's memory.
This gives the child enough context to understand what has already happened
without receiving the full transcript.

### Depth limit

`self.depth` starts at 0 for the root agent and increments by 1 for each
child. `build_tools()` omits `delegate` when `depth >= max_depth`, so a
child at `max_depth=1` cannot create grandchildren. The default `max_depth`
is 1, meaning one level of delegation is permitted.

### Read-only enforcement

`read_only=True` causes `approve()` to return `False` unconditionally. Since
`run_shell`, `write_file`, and `patch_file` are all `risky=True`, they are
automatically denied for every child agent. The child can only use the three
safe tools: `list_files`, `read_file`, and `search`.

---

## Data Flow Summary

The full lifecycle of a single user request:

```
User types a message
         |
         v
    ask(user_message)
         |
         +-- record user message to history
         |
         +-- LOOP (while tool_steps < max_steps):
         |       |
         |       +-- prompt() assembles:
         |       |     prefix + memory_text() + history_text() + user_message
         |       |
         |       +-- model_client.complete(prompt, max_new_tokens)
         |       |     POST /api/generate to Ollama
         |       |
         |       +-- parse(raw_response)
         |       |     |
         |       |     +-- "tool"  -> run_tool(name, args)
         |       |     |               |
         |       |     |               +-- validate_tool()
         |       |     |               +-- approve()  (if risky)
         |       |     |               +-- tool["run"](args)
         |       |     |               +-- clip(result, 4000)
         |       |     |               +-- record tool result to history
         |       |     |               +-- note_tool()  -> update memory
         |       |     |               +-- continue loop
         |       |     |
         |       |     +-- "final" -> record assistant message
         |       |     |              return final answer
         |       |     |
         |       |     +-- "retry" -> record retry notice
         |       |                    continue loop (attempt counter)
         |       |
         |       +-- session_store.save(session)  after every record()
         |
         v
    return final answer string
         |
         v
    print to terminal
```

Every `record()` call triggers an immediate `session_store.save()`. The
session JSON is always up-to-date on disk, so a crash mid-session loses at
most the in-flight model call.
