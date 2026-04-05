# Session Memory

This document explains how mini-coding-agent persists state between turns,
across restarts, and into child agents. Understanding the session system
explains why the agent remembers what it has done, how `--resume` works, and
what the REPL commands `/memory`, `/session`, and `/reset` actually affect.

---

## Overview

A **session** is the agent's durable record of everything that has happened
since it was started. Every user message, every tool call and its result, and
every model response is appended to the session the moment it occurs. Nothing
waits for a graceful shutdown. If the process is killed mid-task, the session
file on disk is no more than one model call behind.

Sessions serve two distinct purposes that are worth keeping separate:

1. **The full history** — the complete, ordered record of every event in the
   conversation. This is used to reconstruct the session on resume and to
   assemble the transcript section of the model prompt.

2. **Working memory** — a small, distilled summary of what the agent is
   currently doing. This is shown to the model at every turn via the
   `memory_text()` section of the prompt. It is updated automatically as tools
   run and is designed to survive context-window pressure by staying compact.

These two structures are stored together in a single JSON file but serve
different purposes in the prompt. The full history is compressed before it
enters the prompt (`history_text()` applies tiered truncation); the working
memory is shown verbatim and is always up-to-date.

---

## Session File Location and Format

Session files are stored at:

```
<workspace_root>/.mini-coding-agent/sessions/<session-id>.json
```

The `.mini-coding-agent` directory is added to `.gitignore` automatically
(the project's own `.gitignore` contains `.mini-coding-agent`), so session
files never accidentally appear in commits or diffs.

**Session ID format:** `YYYYMMDD-HHMMSS-<6hex>`. For example:
`20260401-144025-2dd0aa`. The date-time prefix makes sessions
lexicographically sortable by creation time. The six-character hex suffix
(from `uuid4().hex[:6]`) provides enough entropy to avoid collisions when
two sessions are created in the same second.

### Complete JSON structure

```json
{
  "id": "20260401-144025-2dd0aa",
  "created_at": "2026-04-01T14:40:25.123456+00:00",
  "workspace_root": "/home/user/my-project",
  "history": [
    {
      "role": "user",
      "content": "add a binary search function to utils.py",
      "created_at": "2026-04-01T14:40:26.000000+00:00"
    },
    {
      "role": "tool",
      "name": "read_file",
      "args": {"path": "utils.py", "start": 1, "end": 200},
      "content": "# utils.py\n   1: import os\n   2: ...",
      "created_at": "2026-04-01T14:40:27.000000+00:00"
    },
    {
      "role": "tool",
      "name": "patch_file",
      "args": {
        "path": "utils.py",
        "old_text": "# end of file",
        "new_text": "\ndef binary_search(nums, target):\n    ..."
      },
      "content": "patched utils.py",
      "created_at": "2026-04-01T14:40:29.000000+00:00"
    },
    {
      "role": "assistant",
      "content": "Done. I added `binary_search` to utils.py starting at line 42.",
      "created_at": "2026-04-01T14:40:30.000000+00:00"
    }
  ],
  "memory": {
    "task": "add a binary search function to utils.py",
    "files": ["utils.py"],
    "notes": [
      "read_file: # utils.py    1: import os    2: ...",
      "patch_file: patched utils.py",
      "Done. I added `binary_search` to utils.py starting at line 42."
    ]
  }
}
```

**Field annotations:**

| Field | Description |
|---|---|
| `id` | Sortable, human-readable identifier. Also the filename stem. |
| `created_at` | ISO 8601 UTC timestamp of session creation. |
| `workspace_root` | Absolute path to the git repository root at session creation time. |
| `history` | Ordered list of all events. Grows without bound during the session. |
| `memory` | Distilled working memory. Bounded in size (see below). |

---

## History Items

All three history item types share `role` and `created_at`. Their additional
fields differ by role.

### `"user"` — user messages

```json
{
  "role": "user",
  "content": "add a binary search function to utils.py",
  "created_at": "2026-04-01T14:40:26.000000+00:00"
}
```

Recorded once per REPL turn, before any tool calls for that turn.

### `"tool"` — tool calls and their results

```json
{
  "role": "tool",
  "name": "patch_file",
  "args": {"path": "utils.py", "old_text": "...", "new_text": "..."},
  "content": "patched utils.py",
  "created_at": "2026-04-01T14:40:29.000000+00:00"
}
```

`name` is the tool name. `args` is the exact argument dict the model
produced. `content` is the tool result after clipping to 4,000 characters.
One of these items is recorded for every tool execution that reaches
`run_tool()`, including failed ones (the error string is stored in
`content`).

### `"assistant"` — model responses and retry notices

```json
{
  "role": "assistant",
  "content": "Done. I added `binary_search` to utils.py starting at line 42.",
  "created_at": "2026-04-01T14:40:30.000000+00:00"
}
```

This role covers two distinct situations: the final answer the model returns
when it emits `<final>...</final>`, and the retry notices that are injected
back into the prompt when the model produces malformed output. Both are
stored under `"assistant"` so the transcript remains a faithful record of
everything that was fed to the model.

---

## The `remember()` Helper

**`remember(bucket, item, limit)`** is a static method that maintains the
working memory lists under an LRU-with-deduplication strategy:

```python
@staticmethod
def remember(bucket, item, limit):
    if not item:
        return
    if item in bucket:
        bucket.remove(item)   # (1) remove existing occurrence
    bucket.append(item)       # (2) append to end (most recent position)
    del bucket[:-limit]       # (3) trim from front (oldest entries)
```

The three operations together implement "recency wins":

1. **Deduplication:** If the item is already in the list, its old position is
   removed first. This prevents the list from containing the same string
   twice.

2. **Recency:** The item is always appended to the end, making position `-1`
   the most recently seen item and position `0` the oldest.

3. **Capacity:** `del bucket[:-limit]` removes all but the last `limit`
   entries. If the bucket has exactly `limit` entries and a new item is added,
   the oldest is dropped.

**Concrete example with the `files` bucket (limit=8):**

```
Initial:  ["utils.py", "main.py", "config.py"]
read_file("main.py")
  → remove "main.py" from index 1
  → append "main.py"
  → result: ["utils.py", "config.py", "main.py"]

read_file("new_module.py")
  → not in list, just append
  → result: ["utils.py", "config.py", "main.py", "new_module.py"]
```

This pattern ensures that the files the agent has touched most recently are
always at the end of the list, where `memory_text()` displays them to the
model.

---

## Working Memory: `note_tool()`

After every tool execution, `note_tool(name, args, result)` is called
automatically by the main `ask()` loop. It updates both fields of the working
memory without any action from the model.

```python
def note_tool(self, name, args, result):
    memory = self.session["memory"]
    path = args.get("path")
    if name in {"read_file", "write_file", "patch_file"} and path:
        self.remember(memory["files"], str(path), 8)
    note = f"{name}: {clip(str(result).replace(chr(10), ' '), 220)}"
    self.remember(memory["notes"], note, 5)
```

### `memory["files"]` — file path tracking

Any call to `read_file`, `write_file`, or `patch_file` that includes a
`path` argument adds that path to the files list. The list is bounded at 8
entries using the LRU pattern described above. `list_files`, `search`, and
`run_shell` do not update this list, even though they may operate on paths.

The files list gives the model a quick reminder of which files are "in play"
without requiring it to scan the full history. At the start of the next turn,
the model sees `files: utils.py, main.py` in the memory block and knows where
previous work happened.

### `memory["notes"]` — tool result summaries

Every tool call generates one note entry, regardless of tool type or success.
The note is constructed by:

1. Prepending the tool name.
2. Taking the result string, collapsing all newlines to spaces (so the note
   is always a single line).
3. Clipping the result to 220 characters.

```
"patch_file: patched utils.py"
"run_shell: exit_code: 0 stdout: All 12 tests passed. stderr: (empty)"
"read_file: # utils.py    1: import os    2: from pathlib import Path ..."
```

The notes list is bounded at 5 entries. It acts as a short-term log: the
model can see at a glance what the last five things it did were, even if those
events have been compressed in the transcript.

The final assistant answer is also added to notes via a direct call to
`remember` in `ask()`, so the most recent response is always visible in the
memory block.

---

## The `memory_text()` Prompt Block

At every model turn, `memory_text()` serializes the working memory into the
prompt:

```
Memory:
- task: add a binary search function to utils.py
- files: utils.py, main.py
- notes:
  - read_file: # utils.py    1: import os    2: from pathlib import Path ...
  - patch_file: patched utils.py
  - Done. I added `binary_search` to utils.py starting at line 42.
```

This block appears between the static prefix and the transcript in every
prompt. It is always current — `memory_text()` reads directly from
`self.session["memory"]` and is not cached. Because the block is compact (at
most a few hundred characters), it adds negligible token cost while giving the
model persistent situational awareness.

---

## Session Resume

The `--resume` flag restores a previous session. Two forms are supported:

```
python mini_coding_agent.py --resume latest
python mini_coding_agent.py --resume 20260401-144025-2dd0aa
```

### `--resume latest`

`SessionStore.latest()` lists all `*.json` files under the sessions directory,
sorts them by modification time (`st_mtime`), and returns the stem of the most
recently modified file:

```python
def latest(self):
    files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
    return files[-1].stem if files else None
```

The stem is the session ID, which is then passed to `load()`.

### `--resume <session-id>`

`SessionStore.load(session_id)` reads the corresponding `.json` file and
deserializes it:

```python
def load(self, session_id):
    return json.loads(self.path(session_id).read_text(encoding="utf-8"))
```

### `MiniAgent.from_session()`

The loaded session dict is passed directly to `MiniAgent.__init__` as the
`session` argument. The initializer skips the default session construction
and uses the provided dict instead:

```python
@classmethod
def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
    return cls(
        model_client=model_client,
        workspace=workspace,
        session_store=session_store,
        session=session_store.load(session_id),
        **kwargs,
    )
```

Two things are restored from disk and two things are re-built fresh:

| Restored from disk | Re-built fresh |
|---|---|
| `history` — all previous events | `prefix` — workspace snapshot re-taken from current git state |
| `memory` — task, files, notes | `tools` — re-registered from current agent config |

This means the resumed agent sees the full history of the previous session
exactly as if the session had never been interrupted. The workspace snapshot
in the prefix is re-taken from the current state of the repository, so the
model gets up-to-date git status and branch information even when resuming an
old session.

---

## REPL Commands for Session Inspection

The interactive REPL exposes three commands for inspecting and managing the
current session. None of these commands affect the on-disk session file
except `/reset`.

| Command | What it does |
|---|---|
| `/memory` | Prints the current working memory: task, files list, and notes list |
| `/session` | Prints the absolute path to the current session `.json` file |
| `/reset` | Clears `history[]` to `[]` and `memory` to `{"task": "", "files": [], "notes": []}`, keeps the same session ID, and saves immediately |

`/reset` is a destructive operation on the session: the history and memory are
gone and cannot be recovered (the session file on disk is immediately
overwritten). The session ID is preserved, so the file path does not change.
Use `/reset` when you want to start a fresh conversation in the same workspace
without creating a new session file.

---

## `record()` and Write-Through Persistence

Every state change flows through `record()`:

```python
def record(self, item):
    self.session["history"].append(item)
    self.session_path = self.session_store.save(self.session)
```

`session_store.save()` immediately serializes the entire session dict to the
JSON file. There is no buffering, no batch write, and no periodic flush.
Every user message, every tool result, and every model response triggers a
full file write the moment it is recorded.

This write-through strategy means the session is at most one event behind
disk at any point. If the process is killed between tool calls, the previous
tool result is already on disk. If it is killed during a model call, the user
message that triggered the call is on disk. The only unrecoverable loss is the
in-flight model response.

The tradeoff is disk I/O: each `record()` call writes the entire session JSON.
For short sessions this is negligible. For long sessions with many large
`read_file` results in the history, the file can reach several megabytes and
each write rewrites the entire file. This is acceptable for a local
single-user agent but would not scale to a high-throughput server setting.

---

## Session Growth and Limits

The session JSON file grows without a hard ceiling. A session with many
`read_file` calls on large files can accumulate megabytes because the full
tool output (up to 4,000 characters per call) is stored in every `"tool"`
history item.

The compression in `history_text()` applies only to prompt construction. It
does not modify the session file. When `history_text()` drops an old duplicate
`read_file` or truncates an older entry to 180 characters, the original
uncompressed data is still in the JSON file. This is intentional: the session
file is a faithful audit log, while the prompt transcript is an
approximation optimized for token budget.

If disk space is a concern for very long sessions, the `/reset` command clears
the history and memory, bringing the file back to a minimal state. There is no
built-in pruning or rotation of session files beyond this manual option.
