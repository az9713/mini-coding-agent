# Tools Reference

This document is the authoritative reference for the seven tools available to
mini-coding-agent. It explains what each tool does, what arguments it accepts,
what the model sees back, how errors surface, and why the safety boundaries are
designed the way they are.

---

## Overview

**Tools** are the only way the agent can interact with the workspace. The model
cannot read files, run commands, or write code through prose — it must emit a
tool call, wait for the result, and then decide what to do next. Every action
the agent takes is therefore auditable: there is a complete record of every
tool name, argument set, and result in the session JSON.

The agent exposes seven tools in its system prompt. Six are always available;
the seventh (`delegate`) is registered only when the agent is not already at
maximum delegation depth. Each tool listing in the prompt includes the
argument schema, a risk flag (`safe` or `approval required`), and a one-line
description.

### Two call formats

The model can invoke a tool in two syntactically distinct formats. Both are
accepted by the `parse()` method.

**JSON format** — used for most tools, especially when arguments are short
single-line strings:

```
<tool>{"name":"list_files","args":{"path":"src"}}</tool>
```

**XML format** — preferred for `write_file` and `patch_file` when the content
spans multiple lines, because it avoids JSON escaping of newlines and special
characters:

```
<tool name="write_file" path="utils.py">
<content>
def clamp(value, lo, hi):
    return max(lo, min(hi, value))
</content>
</tool>
```

In the XML format, the tool name and simple scalar arguments are XML
attributes on the opening `<tool>` tag. Multi-line values (`content`,
`old_text`, `new_text`, `command`, `task`, `pattern`) are child elements
inside the body. `parse_xml_tool()` extracts both forms and assembles them
into the same `{"name": ..., "args": {...}}` dictionary that the JSON format
produces. From this point on the two formats are indistinguishable to the
rest of the pipeline.

---

## Tool Execution Pipeline

Every tool invocation passes through a fixed sequence of gates before any
side effect can occur. Understanding this pipeline explains why certain error
messages appear and when they appear.

```
  model output
       |
       v
    parse()
       |
       +-- malformed JSON? ---------> return retry notice to model
       |
       v
  validate_tool()
       |
       +-- invalid args? -----------> return "error: invalid arguments for <name>: ..."
       |                              + example call appended to error
       v
  repeated_tool_call()
       |
       +-- same call twice in a row? -> return "error: repeated identical tool call for <name>; ..."
       |
       v
    approve()
       |
       +-- denied? -----------------> return "error: approval denied for <name>"
       |
       v
  tool["run"](args)
       |
       +-- exception raised? -------> return "error: tool <name> failed: <exc>"
       |
       v
  clip(result, 4000)
       |
       v
  record() + note_tool()
       |
       v
  result string injected into next prompt
```

The model always receives a string back — even on failure. Errors are not
raised as Python exceptions into the main loop; they are returned as error
strings so the model can read them, understand what went wrong, and try a
corrected call.

---

## Path Safety

Every tool that accepts a `path` argument passes it through `self.path()`
before use. This method resolves the path to an absolute form and verifies
that it remains inside the workspace root.

```python
def path(self, raw_path):
    path = Path(raw_path)
    path = path if path.is_absolute() else self.root / path
    resolved = path.resolve()
    if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
        raise ValueError(f"path escapes workspace: {raw_path}")
    return resolved
```

**Path resolution** works as follows: if the raw path is relative, it is
joined with `self.root` (the git repository root). The result is then
`resolve()`d, which expands all symlinks and `..` components into a canonical
absolute path. The resolved path is then compared against `self.root` using
`os.path.commonpath`. If the resolved path does not share `self.root` as its
common prefix, the call raises `ValueError` and the tool execution is
aborted.

**Path traversal** is the attack (or accident) where a relative path like
`../../etc/passwd` navigates above the workspace root. Consider an agent
running with `root=/home/user/project`. A raw path of
`../../../etc/passwd` would resolve to `/etc/passwd`. The commonpath check
catches this:

```
self.root  = /home/user/project
resolved   = /etc/passwd
commonpath = /

commonpath != self.root  →  ValueError("path escapes workspace: ../../../etc/passwd")
```

The model receives:

```
error: invalid arguments for read_file: path escapes workspace: ../../../etc/passwd
```

This boundary matters even for a local agent because a model hallucinating
paths or an adversarial prompt injected via a file in the workspace could
otherwise cause the agent to read or overwrite files outside the project.

---

## IGNORED_PATH_NAMES

Several tools skip entries whose name appears in the `IGNORED_PATH_NAMES` set:

```python
IGNORED_PATH_NAMES = {
    ".git",
    ".mini-coding-agent",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
}
```

| Name | Reason excluded |
|---|---|
| `.git` | Binary objects, pack files, and internal refs are not useful to the model and can be very large |
| `.mini-coding-agent` | The session store itself — reading the agent's own session files mid-task would be confusing and circular |
| `__pycache__` | Compiled Python bytecode (`.pyc` files) — not human-readable source |
| `.pytest_cache` | Test runner cache — not part of the project source |
| `.ruff_cache` | Linter cache — not part of the project source |
| `.venv` | Virtual environment — thousands of dependency files the model does not need |
| `venv` | Alternate virtualenv convention — same reason |

`list_files` applies this filter at the top level of a directory listing.
The fallback path in `search` applies it to every path component of every
file found via `rglob`, so a file nested inside `.venv/lib/python3.12/site-packages/`
is excluded at the `.venv` component.

---

## `list_files`

**Purpose:** List the files and directories inside a workspace directory,
one entry per line, sorted with directories first.

### Schema

| Argument | Type | Default | Required |
|---|---|---|---|
| `path` | string | `"."` | optional |

### Risk

**Safe.** `list_files` is read-only and requires no approval in any mode.

### Validation rules

- `path` must resolve to an existing directory (not a file, not a
  non-existent path). If it is not a directory, validation raises
  `"path is not a directory"`.

### Output format

Each entry is prefixed with `[D]` for directories and `[F]` for files.
Paths are shown relative to the workspace root, not the queried directory.
Directories are sorted before files; within each group, entries are sorted
case-insensitively by name. The output is capped at 200 entries. If the
directory is empty (after filtering ignored names), the output is the string
`(empty)`.

```
[D] src
[D] tests
[F] README.md
[F] main.py
[F] pyproject.toml
```

### Example calls

JSON format:
```
<tool>{"name":"list_files","args":{"path":"src"}}</tool>
```

XML format:
```
<tool name="list_files" path="src"></tool>
```

### What can go wrong

```
error: invalid arguments for list_files: path is not a directory
example: <tool>{"name":"list_files","args":{"path":"."}}</tool>
```

```
error: invalid arguments for list_files: path escapes workspace: ../../other-project
example: <tool>{"name":"list_files","args":{"path":"."}}</tool>
```

> **Note:** `list_files` shows at most 200 entries. For repositories with
> extremely large directories, some entries will be silently omitted. Use
> `search` to find specific files by name pattern in those cases.

---

## `read_file`

**Purpose:** Read a UTF-8 text file and return a numbered line listing for a
specified line range.

### Schema

| Argument | Type | Default | Required |
|---|---|---|---|
| `path` | string | — | **required** |
| `start` | integer | `1` | optional |
| `end` | integer | `200` | optional |

### Risk

**Safe.** `read_file` is read-only and requires no approval in any mode.

### Validation rules

- `path` must resolve to an existing file (not a directory).
- `start` must be at least `1`.
- `end` must be greater than or equal to `start`.

### Output format

The output begins with a header line containing the file path relative to the
workspace root, then the requested lines with four-digit right-aligned line
numbers:

```
# src/utils.py
   1: import os
   2: from pathlib import Path
   3:
   4: def clamp(value, lo, hi):
   5:     return max(lo, min(hi, value))
```

The file is decoded as UTF-8 with `errors="replace"`, which means binary
files or files with encoding errors never crash the tool — undecodable bytes
are silently replaced with the Unicode replacement character `\ufffd`. The
line numbers in the output match the actual file line numbers, so the model
can supply them directly to `patch_file` for context.

**Files longer than 200 lines** must be read in multiple calls. To read lines
201 through 400:

```
<tool>{"name":"read_file","args":{"path":"main.py","start":201,"end":400}}</tool>
```

The tool does not automatically page; the model must explicitly request
subsequent ranges if it needs them.

### Example calls

JSON format:
```
<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>
```

XML format:
```
<tool name="read_file" path="README.md" start="1" end="80"></tool>
```

### What can go wrong

```
error: invalid arguments for read_file: path is not a file
example: <tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>
```

```
error: invalid arguments for read_file: invalid line range
example: <tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>
```

---

## `search`

**Purpose:** Search for a text pattern across all files in a directory,
returning file path, line number, and matching line for each hit.

### Schema

| Argument | Type | Default | Required |
|---|---|---|---|
| `pattern` | string | — | **required** |
| `path` | string | `"."` | optional |

### Risk

**Safe.** `search` is read-only and requires no approval in any mode.

### Validation rules

- `pattern` must not be an empty string after stripping whitespace.
- `path` must be a valid path within the workspace (path safety applies).

### Output format

When `rg` is available, results follow ripgrep's default line format:

```
src/utils.py:14:def clamp(value, lo, hi):
tests/test_utils.py:8:    assert clamp(5, 0, 10) == 5
```

When the fallback is used, the format is identical:

```
src/utils.py:14:def clamp(value, lo, hi):
```

If no matches are found, the output is the string `(no matches)`.

### The `rg` vs fallback distinction

`search` checks for `rg` (ripgrep) with `shutil.which("rg")` at call time.
If `rg` is available:

- It is invoked with `-n` (line numbers), `--smart-case` (case-insensitive
  when the pattern is all lowercase, case-sensitive when it contains any
  uppercase), and `--max-count 200` (at most 200 matches per file).
- `rg` respects `.gitignore` rules automatically.
- Regex syntax is full ripgrep syntax (Rust regex engine).

If `rg` is not available, a pure-Python fallback runs:

- It walks the directory with `rglob("*")`, skipping any path whose
  components intersect with `IGNORED_PATH_NAMES`.
- Matching is a simple case-insensitive substring test (`pattern.lower() in
  line.lower()`). This means the pattern is treated as a literal string, not
  a regex.
- It stops after collecting 200 total matches across all files.

The `rg` path is significantly faster for large repositories and supports
regex patterns. Installing `rg` is strongly recommended.

### Example calls

JSON format:
```
<tool>{"name":"search","args":{"pattern":"def binary_search","path":"src"}}</tool>
```

XML format:
```
<tool name="search" pattern="def binary_search" path="src"></tool>
```

### What can go wrong

```
error: invalid arguments for search: pattern must not be empty
example: <tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>
```

---

## `run_shell`

**Purpose:** Execute an arbitrary shell command in the workspace root and
return its exit code, stdout, and stderr.

### Schema

| Argument | Type | Default | Required |
|---|---|---|---|
| `command` | string | — | **required** |
| `timeout` | integer (seconds) | `20` | optional |

### Risk

**Risky.** `run_shell` requires approval before execution.

| Approval policy | Behaviour |
|---|---|
| `"ask"` | Prompts the user: `approve run_shell {"command": "..."}? [y/N]` |
| `"auto"` | Executes silently without prompting |
| `"never"` | Denied immediately; model receives error string |
| `read_only=True` | Always denied, regardless of policy (child agents) |

### Validation rules

- `command` must not be empty after stripping whitespace.
- `timeout` must be an integer in the range `[1, 120]`.

### Output format

```
exit_code: 0
stdout:
All tests passed.
stderr:
(empty)
```

All four fields are always present. `stdout` and `stderr` show `(empty)` if
the respective stream produced no output. The full `subprocess.run` output is
returned, meaning the model can distinguish a zero exit code (success) from a
non-zero one (failure) and inspect both streams independently.

The command runs with `cwd=self.root` (the git repository root), `shell=True`
(so the full shell is available, including pipes, redirects, and environment
variable expansion), and `capture_output=True` (so output is not printed to
the terminal directly). The `text=True` flag decodes output as UTF-8.

> **Warning:** `shell=True` means the model can invoke any program available
> on the system PATH, including `rm`, `curl`, `git push`, or any installed
> tool. Always review shell commands carefully before approving them when
> running in `"ask"` mode. Use `"never"` in automated pipelines where shell
> access is not intended.

### Example calls

JSON format:
```
<tool>{"name":"run_shell","args":{"command":"python -m pytest -q","timeout":30}}</tool>
```

XML format:
```
<tool name="run_shell" timeout="30">
<command>python -m pytest -q</command>
</tool>
```

### What can go wrong

```
error: approval denied for run_shell
```

```
error: invalid arguments for run_shell: timeout must be in [1, 120]
example: <tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>
```

```
error: tool run_shell failed: Command '...' timed out after 20 seconds
```

---

## `write_file`

**Purpose:** Write a complete text file to a path in the workspace, creating
parent directories if needed and overwriting the file if it already exists.

### Schema

| Argument | Type | Default | Required |
|---|---|---|---|
| `path` | string | — | **required** |
| `content` | string | — | **required** |

### Risk

**Risky.** `write_file` requires approval before execution.

| Approval policy | Behaviour |
|---|---|
| `"ask"` | Prompts the user with the target path |
| `"auto"` | Executes silently without prompting |
| `"never"` | Denied immediately |
| `read_only=True` | Always denied |

### Validation rules

- `path` must not resolve to an existing directory (writing to a directory
  path would be ambiguous).
- `content` key must be present in `args` (an empty string `""` is valid
  content; a missing key is not).

### Output format

```
wrote src/new_module.py (412 chars)
```

The character count is the length of the `content` string (not bytes). Parent
directories are created with `mkdir(parents=True, exist_ok=True)`, so the
model can write to nested paths like `src/sub/module.py` even if `sub` does
not yet exist.

**`write_file` overwrites without warning.** If the file already exists, its
previous contents are replaced entirely. The model has no way to append to an
existing file — to make targeted changes to an existing file, use
`patch_file`.

For multi-line content, the XML format is strongly preferred because it
avoids the need to JSON-escape newlines and quotes:

```
<tool name="write_file" path="src/utils.py">
<content>
import os


def ensure_dir(path):
    """Create directory and all parents if they do not exist."""
    os.makedirs(path, exist_ok=True)
</content>
</tool>
```

### Example calls

JSON format (short content):
```
<tool>{"name":"write_file","args":{"path":".env.example","content":"API_KEY=\nDEBUG=false\n"}}</tool>
```

XML format (multi-line content):
```
<tool name="write_file" path="hello.py">
<content>
def hello(name: str) -> str:
    return f"Hello, {name}!"


if __name__ == "__main__":
    print(hello("world"))
</content>
</tool>
```

### What can go wrong

```
error: approval denied for write_file
```

```
error: invalid arguments for write_file: path is a directory
example: <tool name="write_file" path="binary_search.py"><content>...</content></tool>
```

```
error: invalid arguments for write_file: missing content
example: <tool name="write_file" path="binary_search.py"><content>...</content></tool>
```

---

## `patch_file`

**Purpose:** Replace one exact occurrence of a text block in an existing file
with a new text block, leaving the rest of the file untouched.

### Schema

| Argument | Type | Default | Required |
|---|---|---|---|
| `path` | string | — | **required** |
| `old_text` | string | — | **required** |
| `new_text` | string | — | **required** |

### Risk

**Risky.** `patch_file` requires approval before execution.

| Approval policy | Behaviour |
|---|---|
| `"ask"` | Prompts the user |
| `"auto"` | Executes silently |
| `"never"` | Denied immediately |
| `read_only=True` | Always denied |

### Validation rules

- `path` must resolve to an existing file.
- `old_text` must not be empty.
- `new_text` key must be present (an empty string is a valid replacement —
  it deletes the matched block).
- `old_text` must occur **exactly once** in the file. Zero occurrences and
  two or more occurrences both raise errors.

The uniqueness requirement is enforced twice: once during `validate_tool()`
(which reads the file to count occurrences) and once during `tool_patch_file()`
execution. This double-check is safe because `validate_tool` and the run
function execute synchronously in the same request — no other process can
modify the file between the two reads in normal single-user operation.

### Output format

```
patched src/utils.py
```

### Choosing a good `old_text`

The most common failure mode is `old_text` that is either too short (appears
multiple times) or wrong (the file has changed since the model last read it).

**Too generic — fails with `found 2`:**

```python
old_text = "return None"
```

This string might appear in multiple functions, class methods, or early-return
guards throughout the file.

**Good — unique with enough surrounding context:**

```python
old_text = "def process(data):\n    if not data:\n        return None\n    return transform(data)"
```

Include the full function signature and enough body lines to make the string
unique. When in doubt, read the file first and count.

### Example calls

JSON format:
```
<tool>{"name":"patch_file","args":{"path":"main.py","old_text":"return -1","new_text":"return mid"}}</tool>
```

XML format (preferred for multi-line patches):
```
<tool name="patch_file" path="main.py">
<old_text>
def binary_search(nums, target):
    return -1
</old_text>
<new_text>
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
</new_text>
</tool>
```

### What can go wrong

```
error: approval denied for patch_file
```

```
error: invalid arguments for patch_file: path is not a file
example: <tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>
```

```
error: invalid arguments for patch_file: old_text must occur exactly once, found 0
example: <tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>
```

```
error: invalid arguments for patch_file: old_text must occur exactly once, found 3
example: <tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>
```

> **Note:** When `patch_file` returns `found 0`, the most likely causes are:
> the file was modified after the model last read it, the `old_text` contains
> leading/trailing whitespace that differs from the file, or the model
> constructed the string from memory rather than copying it verbatim from a
> `read_file` result. Always copy `old_text` directly from a recent
> `read_file` output.

---

## `delegate`

**Purpose:** Spawn a bounded, read-only child agent to investigate a question
and return its answer as a string. See `delegation.md` for a full deep-dive.

### Schema

| Argument | Type | Default | Required |
|---|---|---|---|
| `task` | string | — | **required** |
| `max_steps` | integer | `3` | optional |

### Risk

**Safe.** `delegate` does not modify any files. The child agent is
`read_only=True` and `approval_policy="never"`, so all risky tools are
automatically denied inside the child.

> **Note:** `delegate` is only available when `depth < max_depth`. At the
> default `max_depth=1`, a child agent (depth=1) does not have `delegate` in
> its tool list. Calling `delegate` at maximum depth raises a validation error.

### Validation rules

- `depth` must be less than `max_depth` (checked at `build_tools()` time and
  again at `validate_tool()` time).
- `task` must not be empty.

### Output format

```
delegate_result:
The `process()` function at line 42 reads from stdin line by line and calls
`transform()` on each. Error handling catches only `IOError`. No test files
for this module were found under tests/.
```

The prefix `delegate_result:\n` is always present. The remainder is whatever
the child agent returned as its final answer.

### Example calls

JSON format:
```
<tool>{"name":"delegate","args":{"task":"find all functions that handle errors in main.py","max_steps":3}}</tool>
```

XML format:
```
<tool name="delegate" max_steps="2">
inspect the README and list all runtime dependencies
</tool>
```

### What can go wrong

```
error: invalid arguments for delegate: delegate depth exceeded
```

```
error: invalid arguments for delegate: task must not be empty
example: <tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>
```

---

## Repeated Call Detection

The agent detects when the model issues the same tool call twice in a row and
returns an error instead of executing the tool a second time. The rule is
exact: if the two most recent entries in the session history that have
`"role": "tool"` share the same `name` **and** the same `args` dict, the call
is blocked.

```python
def repeated_tool_call(self, name, args):
    tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
    if len(tool_events) < 2:
        return False
    recent = tool_events[-2:]
    return all(item["name"] == name and item["args"] == args for item in recent)
```

This guards against a common model failure mode where the model is confused by
a result (for example, an error message) and loops by re-issuing the same
request. If the most recent two tool results are both from the same call, the
model receives:

```
error: repeated identical tool call for read_file; choose a different tool or return a final answer
```

The check looks only at the two most recent tool events, not the full history.
This means the model can call the same tool with the same arguments later in
the session (after at least one different tool call in between) without
triggering the guard. The intent is to break tight loops, not to forbid
revisiting files.
