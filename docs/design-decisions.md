# Design Decisions

This document explains the *why* behind every significant architectural
choice in `mini_coding_agent.py`. Good design decisions have reasons; this
file makes those reasons explicit so that learners can evaluate them and
adapt them when building their own agents.

---

## Why a Single File?

The file opens with this comment block, which doubles as a table of contents:

```python
##############################
#### Six Agent Components ####
##############################
# 1) Live Repo Context -> WorkspaceContext
# 2) Prompt Shape And Cache Reuse -> build_prefix, memory_text, prompt
# 3) Structured Tools, Validation, And Permissions -> build_tools, run_tool, validate_tool, approve, parse, path, tool_*
# 4) Context Reduction And Output Management -> clip, history_text
# 5) Transcripts, Memory, And Resumption -> SessionStore, record, note_tool, ask, reset
# 6) Delegation And Bounded Subagents -> tool_delegate
```

That comment is a navigation aid. Everything it references is reachable with
a single search in one file. There is no need to jump between modules,
packages, or directories. A student reading the code for the first time has
one artifact to hold in mind.

The practical consequence of a monolith is that reading comprehension scales
with file length, not with graph complexity. Following a function call in a
multi-module system requires context-switching between files, tracking
relative imports, and building a mental picture of which layer owns what.
None of that overhead exists here.

The tradeoff is real: 1,017 lines is approaching the limit of what a single
file can do legibly. Adding a second model backend, a plugin system, or a
REST interface would push the file to a size where the single-file benefit
inverts. For a learning project, the current size is close to ideal. For a
production system, you would split the file along the six component
boundaries that already exist.

---

## Why Ollama? (No Cloud APIs)

The agent calls exactly one HTTP endpoint:

```python
request = urllib.request.Request(
    self.host + "/api/generate",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
```

Ollama runs locally. There are no API keys, no billing accounts, no rate
limits, and no data leaving the machine. For a coding agent that reads and
writes files in your workspace, keeping inference local is a meaningful
privacy guarantee.

The cost is model quality. At the time of writing, the default model is
`qwen3.5:4b`. A 4-billion-parameter local model is substantially less capable
than a frontier cloud model. Longer or more complex tasks will occasionally
require the user to switch to a larger local model (`qwen3.5:9b` or similar).

The backend abstraction is thin on purpose. `OllamaModelClient` implements a
single method:

```python
def complete(self, prompt, max_new_tokens) -> str:
    ...
```

The test suite provides `FakeModelClient`, which has the same interface with
fixed outputs. Swapping the Ollama backend for a different provider requires
implementing one method on one class. No other code needs to change.

---

## Why Zero External Dependencies?

Running the agent requires no `pip install` step:

```bash
python mini_coding_agent.py
```

That command works on any machine with Python 3.10+. Every import in the file
is from the standard library:

```python
import argparse, json, os, re, shutil, subprocess, sys, textwrap
import urllib.error, urllib.request, uuid
from datetime import datetime, timezone
from pathlib import Path
```

The specific substitutions that made this possible:

| Dependency avoided | Standard library used instead |
|---|---|
| `requests` | `urllib.request.urlopen` |
| `httpx` | `urllib.request` with `HTTPError` / `URLError` |
| Shell wrapper libs | `subprocess.run` directly |
| `python-dateutil` | `datetime` with `timezone.utc` |
| JSON schema validator | Manual `validate_tool()` per tool |

The tradeoff is verbosity. The HTTP code in `OllamaModelClient.complete()` is
more explicit than a `requests.post()` call. There is no automatic retry,
no connection pooling, and no backoff library. For a local Ollama connection
that rarely fails, this is an acceptable tradeoff. For a production agent
hitting a remote API, you would want `httpx` or `tenacity`.

---

## Why Accept Both JSON and XML Tool Formats?

The model is instructed to use JSON inside `<tool>` tags:

```
<tool>{"name":"list_files","args":{"path":"."}}</tool>
```

But for multi-line file content, the model is encouraged to use XML
attributes instead:

```
<tool name="write_file" path="binary_search.py">
<content>
def binary_search(nums, target):
    lo, hi = 0, len(nums) - 1
    ...
</content>
</tool>
```

This is not inconsistency. It is a deliberate format selection based on what
each format handles well.

**JSON is compact and easy to parse**, but JSON strings require escaping
newlines (`\n`), quotes (`\"`), and backslashes (`\\`). A model generating a
50-line Python function inside a JSON string is likely to produce escaping
errors, especially smaller models. A single missing backslash makes the
entire tool call unparseable.

**XML content elements preserve multi-line text verbatim.** There is nothing
to escape. The model writes the file content exactly as it would appear in
the editor, and `extract_raw()` recovers it character-for-character.

`parse()` tries JSON first. If the `<tool>` tag has no attributes (the
`<tool>{...}</tool>` form), it attempts `json.loads`. If that fails, or if
the tag has attributes (the `<tool name="..." ...>` form), it calls
`parse_xml_tool()`. A `("retry", notice)` result is returned only if both
parsers fail.

This dual-format tolerance also makes the agent more robust across different
model families. A model that was trained to emit XML-style tool calls will
work without any prompt engineering changes.

---

## Why Is the Prefix Static (Built Once)?

`build_prefix()` is called in `__init__` and the result is stored as
`self.prefix`. It is never called again for the lifetime of that agent
instance.

```python
self.prefix = self.build_prefix()  # once, at construction
```

There are two reasons for this.

**First, prompt prefix caching.** Some inference engines — and Ollama's
underlying llama.cpp backend is one of them — can cache the **key-value
attention state** for a fixed prefix. If the first N tokens of every prompt
are identical, the engine can reuse the computed attention matrices rather
than recomputing them. The prefix accounts for the largest section of the
prompt. Keeping it byte-for-byte identical across all turns in a session
makes that caching possible.

**Second, consistent layout.** Even without hardware-level caching, a
consistent prompt structure helps instruction-following models. The rules,
tool definitions, and workspace context are always at the same position. The
model does not need to search for where the instructions end and the
transcript begins.

The workspace context (git state, project docs) is captured once at startup
and baked into the prefix. It is not re-fetched on every turn. If you run
`git commit` in another terminal while the agent is running, the agent will
not see that commit until you restart it. This is the correct tradeoff: the
workspace snapshot is meant to orient the model, not to be a live mirror of
the repository.

---

## Why Does History Compression Deduplicate Reads?

A typical agent turn sequence for an edit task looks like this:

```
1. [user]  "add error handling to parse_csv in utils.py"
2. [tool:read_file]  path=utils.py, lines 1-200  -> full file content
3. [tool:write_file] path=utils.py               -> "wrote utils.py"
4. [tool:read_file]  path=utils.py, lines 1-200  -> full file content (verify)
5. [assistant]       "Done. Added try/except around the CSV reader."
```

After a few more turns, items 2 and 4 are both in the "older" tier of
`history_text()`. They have the same path, and item 4's content is the
post-edit version — but by the time they are both old, the current file
state is what matters, not either historical read.

**Deduplication keeps the first read for a given path and drops all
subsequent older reads for the same path.** The logic in `history_text()`:

```python
seen_reads = set()
for index, item in enumerate(history):
    recent = index >= recent_start
    if item["role"] == "tool" and item["name"] == "read_file" and not recent:
        path = str(item["args"].get("path", ""))
        if path in seen_reads:
            continue          # drop this item entirely
        seen_reads.add(path)
```

The deduplication applies only to older items (outside the recent 6). Recent
reads are always included at full length because the model is actively using
that information right now.

This matters because `read_file` outputs are the largest items in the
history. A full file read at 200 lines could easily be 4,000 characters.
Five duplicates of the same file would consume 20,000 characters — more than
the entire `MAX_HISTORY` budget. Deduplication keeps the history budget
available for tool results that contain genuinely new information.

---

## Why Does `patch_file` Require Exactly One Match?

`validate_tool()` for `patch_file` counts occurrences of `old_text` in the
file and raises an error if the count is not exactly 1:

```python
count = text.count(old_text)
if count != 1:
    raise ValueError(f"old_text must occur exactly once, found {count}")
```

This applies both in `validate_tool()` (called before approval) and in
`tool_patch_file()` (called during execution). The same check runs twice to
ensure consistency.

**Zero matches** means the file changed between when the model read it and
when it issued the patch. Silently succeeding on a zero-match patch would
leave the file unmodified while returning a success message. The model would
proceed believing the patch was applied when it was not.

**Two or more matches** means the replacement is ambiguous. Suppose
`old_text` is `"return None"` and it appears on three lines. Should all
three be replaced? Only the first? Only the one the model intended? The
agent cannot know. The strict requirement forces the model to provide an
`old_text` that is precise enough to be unique — typically a few lines of
surrounding context, not just the line being changed.

The practical effect is that the model must read before patching. If the
file has been modified since the last read, the patch will fail and the model
will re-read. This is the correct behavior: the model should never apply a
patch to a file state it has not verified.

---

## Why Is `read_only=True` for Child Agents?

Delegation is designed for **investigation**, not modification. The parent
agent spawns a child to answer a question about the codebase — "what does
this function do?", "where is this symbol used?" — and the child reports
back. The parent then decides what to do with that information.

If a child agent could write files, the parent's history would become
unreliable. The parent's transcript would show the delegation task and the
child's final answer, but not the intermediate writes the child made. The
parent might then issue its own writes based on a stale understanding of the
file state.

`read_only=True` causes `approve()` to return `False` unconditionally:

```python
def approve(self, name, args):
    if self.read_only:
        return False
    ...
```

Because `run_shell`, `write_file`, and `patch_file` are all `risky=True`,
they are denied at the approval gate. The child never reaches the run
function for any of them. The child can only call `list_files`, `read_file`,
and `search`.

Note that `run_shell` is also blocked. A shell command could modify the
workspace even if it does not write Python files directly. Blocking all risky
tools ensures the child is truly read-only.

---

## Why Are Session IDs Formatted as `YYYYMMDD-HHMMSS-<6hex>`?

The session ID is generated in `__init__`:

```python
"id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
```

A concrete example: `20260401-144025-2dd0aa`.

**Sortability.** The format is lexicographically sortable. Alphabetical order
equals chronological order. `SessionStore.latest()` sorts by `mtime`, but
the name format means a simple `sorted()` on the filenames would also work:

```python
def latest(self):
    files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
    return files[-1].stem if files else None
```

**Human readability.** The date prefix is immediately recognisable. When a
user runs `--resume latest`, the session ID printed in the welcome banner
tells them at a glance which session they are continuing.

**Collision resistance.** Two sessions started within the same second would
have identical timestamps. The six-character hex suffix from `uuid4()` adds
approximately 16 million possible values per second, making collisions
effectively impossible in normal use.

The format is also directly usable as a CLI argument:

```bash
uv run mini-coding-agent --resume 20260401-144025-2dd0aa
```

No quoting, no special characters, no ambiguity about what the argument
means.

---

## Why Is `max_steps` a Per-Request Limit, Not a Per-Session Limit?

`max_steps` (default 6) limits the number of tool-call iterations for a
single user message. After 6 tool steps without a `<final>` response, the
agent stops and returns an error message.

A per-session limit would be unusual: the user might send 20 messages in
one session, each requiring only 2-3 tool steps. Limiting the session total
would penalise longer conversations arbitrarily.

A per-request limit has a clearer contract: each user message gets up to N
steps to produce an answer. If the task cannot be completed in N steps, the
user sees an explicit stop message and can either refine their request or
increase `--max-steps` for more complex tasks.

There is also a separate `max_attempts` limit:

```python
max_attempts = max(self.max_steps * 3, self.max_steps + 4)
```

This is a safety valve for malformed model responses. A model that
consistently returns unparseable output would consume `max_attempts` before
`tool_steps` reached `max_steps`. The model gets more than `max_steps`
chances, but not unlimited chances to produce a valid response.

---

## Why Does `list_files` Filter `IGNORED_PATH_NAMES`?

```python
IGNORED_PATH_NAMES = {
    ".git", ".mini-coding-agent", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".venv", "venv"
}
```

These directories are either version control metadata, the agent's own
storage, or Python build artefacts. None of them contain source code the
agent would meaningfully act on. Showing them in `list_files` output would
add noise to the model's context and potentially cause it to waste tool steps
investigating irrelevant directories.

`.mini-coding-agent` is specifically excluded so that the agent does not read
its own session files when asked to explore the workspace. Session JSON files
contain the full history of past interactions. Including them in directory
listings would be confusing and would leak prior context into the model in an
uncontrolled way.

The filter applies only to `list_files`. If the model explicitly constructs a
path to `.git/config` and calls `read_file`, the path validation in `path()`
will allow it (as long as it does not escape `repo_root`). The ignore list
suppresses noise, not access.

---

## Why Checkpointing? (File Undo and Diff)

Before checkpointing, approving a `write_file` or `patch_file` call was
irreversible without a git checkout. If the model produced bad code and
overwrote a file, the user had to either rely on git or reconstruct the
original manually. This made `--approval auto` genuinely risky and
discouraged experimentation.

The checkpointing system borrowed the design from Claude Code's own
`/rewind` command: snapshot the file **before** the edit, not after. The
snapshot is taken in one place per tool:

```python
# tool_write_file, before any filesystem operation
if self.checkpoint_store:
    self.checkpoint_store.snapshot(path, self.current_turn)

# tool_patch_file, before reading the file to patch
if self.checkpoint_store:
    self.checkpoint_store.snapshot(path, self.current_turn)
```

**Why snapshot before, not after?** The goal is restoration. Storing the
post-edit state would tell you what changed; storing the pre-edit state lets
you undo it. The pre-edit state is the one the user can recover.

**Why deduplicate within a turn?** If the same file is written twice in one
turn, the checkpoint must hold the **original** state — the state before the
turn started — not the intermediate state. `snapshot()` checks whether the
key already exists in the turn data before recording. Only the first call
stores anything.

**Why separate from the session JSON?** Session history records what the agent
did (tool calls and their results). Checkpoints record what files looked like
before changes. Mixing the two would require reading the session file to
perform a rewind, which is expensive for long sessions. A separate file
allows `rewind()` to operate without loading session history at all.

**Why delete turn data after a successful rewind?** The files are restored. The
snapshot data is no longer useful — a second rewind of the same turn cannot
improve on the already-restored state, and storing stale data risks confusion.
Deleting ensures that a double rewind returns `None` (the idiomatic "nothing to
do" signal) rather than silently overwriting files that may have changed since
the first rewind.

**Why does `checkpoint_store=None` for child agents?** Child agents are
unconditionally read-only (`read_only=True`). They cannot call `write_file` or
`patch_file`, so `snapshot()` would never be called. Passing `None` makes the
invariant explicit and avoids allocating an on-disk file for a child that will
never write anything.
