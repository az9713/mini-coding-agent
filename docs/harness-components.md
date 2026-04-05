# Harness Components

This document is the bridge between Sebastian Raschka's article "Components of A Coding Agent"
and the concrete implementation in `mini_coding_agent.py`. The article describes six architectural
components that every practical coding agent needs. This file maps each component to its exact
location in the source, walks through the code line by line, and explains why each design choice
was made. Reading this document alongside the source should make every non-obvious decision
legible.

## Component Map

| # | Name | Primary symbols | Lines |
|---|------|----------------|-------|
| 1 | Live Repo Context | `WorkspaceContext`, `WorkspaceContext.build`, `WorkspaceContext.text` | 77-144 |
| 2 | Prompt Shape and Cache Reuse | `build_prefix`, `memory_text`, `prompt` | 337-443 |
| 3 | Structured Tools, Validation, and Permissions | `build_tools`, `run_tool`, `validate_tool`, `approve`, `parse`, `parse_xml_tool`, `path`, `repeated_tool_call` | 286-332, 511-697, 737-743 |
| 4 | Context Reduction and Output Management | `clip`, `middle`, `history_text`, `MAX_TOOL_OUTPUT`, `MAX_HISTORY` | 36-37, 56-71, 401-425 |
| 5 | Transcripts, Memory, and Resumption | `SessionStore`, `record`, `note_tool`, `remember`, `ask`, `reset` | 150-168, 253-259, 274-280, 448-506, 732-735 |
| 6 | Delegation and Bounded Subagents | `tool_delegate`, conditional in `build_tools`, `validate_tool("delegate")` | 325-332, 609-614, 850-869 |

---

## Component 1 — Live Repo Context

### What it does

A language model starts every conversation with no awareness of the filesystem. It does not know
which branch is checked out, whether there are uncommitted changes, or where to find
project-specific instructions. If the agent had to discover all of this dynamically through tool
calls, it would waste several steps of its limited step budget before it could do any useful work.

This component solves that problem by running a set of shell commands and file reads at startup,
collecting the stable facts about the workspace into a `WorkspaceContext` object, and serializing
that object into the static prompt prefix. Every model call in the session receives this context
automatically, without re-running any git commands.

The word "stable" is deliberate. Git branch, default branch, recent commits, and documentation
files change infrequently during a session. The git working-tree status and the current directory
are captured once and injected as a reference point. If those facts change mid-session, the agent
can use `run_shell` to check them again; that is an intentional design boundary.

### Where in the code

| Symbol | Lines | Purpose |
|--------|-------|---------|
| `WorkspaceContext.__init__` | 78-85 | Stores the 7 collected fields |
| `WorkspaceContext.build` | 87-125 | Classmethod that runs git commands and reads docs |
| `git()` nested helper | 91-103 | Subprocess wrapper with fallback for non-git dirs |
| `repo_root` derivation | 105 | `git rev-parse --show-toplevel` with cwd fallback |
| Doc collection loop | 106-115 | Reads `DOC_NAMES` from two base directories |
| `return cls(...)` | 117-124 | Constructs the instance from collected data |
| `WorkspaceContext.text` | 127-144 | Serializes the object into a prompt-ready string |
| `DOC_NAMES` constant | 16 | The four filenames searched for project docs |

### Code walkthrough

**The `__init__` method (lines 78-85)**

```python
class WorkspaceContext:
    def __init__(self, cwd, repo_root, branch, default_branch, status, recent_commits, project_docs):
        self.cwd = cwd
        self.repo_root = repo_root
        self.branch = branch
        self.default_branch = default_branch
        self.status = status
        self.recent_commits = recent_commits
        self.project_docs = project_docs
```

Seven fields, all strings or simple collections. `cwd` is where the agent was invoked; `repo_root`
is the git repository root (possibly different if the agent was started from a subdirectory).
`branch` is the current branch name. `default_branch` is the merge target, used to help the agent
understand what constitutes a "clean" starting point. `status` is the abbreviated `git status`
output. `recent_commits` is a list of the last five one-line commit summaries. `project_docs` is
a dict mapping relative file paths to their (possibly clipped) contents.

This is a plain data class — no logic, no validation. All the work happens in `build`.

**The `git()` nested helper (lines 91-103)**

```python
def git(args, fallback=""):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or fallback
    except Exception:
        return fallback
```

`check=True` means any non-zero git exit code raises `subprocess.CalledProcessError`. The bare
`except Exception` then catches that — along with `FileNotFoundError` if git is not installed,
`subprocess.TimeoutExpired` if the repository is very large or on a slow network, and any other
failure. All of these cases return `fallback`. This design means the agent degrades gracefully in
non-git directories: `repo_root` falls back to `str(cwd)`, `branch` falls back to `"-"`, and so
on. `timeout=5` prevents the startup from hanging on a remote-tracking operation.

`capture_output=True` suppresses stdout and stderr from leaking to the terminal during startup.
`text=True` decodes the bytes output using the system default encoding. The `or fallback` at the
end handles the case where git succeeds but returns an empty string — for example,
`git branch --show-current` returns an empty string when the repository is in detached HEAD state.

> **Note:** The function silently swallows all exceptions. If you need to debug why workspace
> context looks wrong, temporarily replace `except Exception: return fallback` with
> `except Exception as exc: print(exc, file=sys.stderr); return fallback`.

**`repo_root` derivation and doc collection (lines 105-115)**

```python
repo_root = Path(git(["rev-parse", "--show-toplevel"], str(cwd))).resolve()
docs = {}
for base in (repo_root, cwd):
    for name in DOC_NAMES:
        path = base / name
        if not path.exists():
            continue
        key = str(path.relative_to(repo_root))
        if key in docs:
            continue
        docs[key] = clip(path.read_text(encoding="utf-8", errors="replace"), 1200)
```

`git rev-parse --show-toplevel` returns the absolute path to the root of the git repository. If
the command fails (no git repo), the fallback is `str(cwd)`, so `repo_root` and `cwd` become the
same path, which is safe. Calling `.resolve()` normalizes symlinks and `..` components.

The doc loop iterates `(repo_root, cwd)` — two base directories — and for each checks all four
names in `DOC_NAMES`. The `key` is the path relative to `repo_root`, which makes it
human-readable and deduplicated: if `cwd == repo_root`, the same file would be found twice but
the `if key in docs: continue` guard keeps only the first occurrence, which is the `repo_root`
copy. Each file is clipped to 1,200 characters so that a large `README.md` does not consume the
entire model context.

```python
DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
```

`AGENTS.md` is listed first because it is the highest-priority instruction file by convention
(Claude Code uses this name for per-project agent instructions). `README.md` gives a project
overview. `pyproject.toml` and `package.json` provide dependency and tooling context. When
`AGENTS.md` exists, the agent reads it before any other file — this is where you put instructions
like "always run `uv run pytest` to execute tests."

**The `return cls(...)` call (lines 117-124)**

```python
return cls(
    cwd=str(cwd),
    repo_root=str(repo_root),
    branch=git(["branch", "--show-current"], "-") or "-",
    default_branch=(git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], "origin/main") or "origin/main").removeprefix("origin/"),
    status=clip(git(["status", "--short"], "clean") or "clean", 1500),
    recent_commits=[line for line in git(["log", "--oneline", "-5"]).splitlines() if line],
    project_docs=docs,
)
```

`branch` uses `or "-"` as a second fallback because `git branch --show-current` returns an empty
string in detached HEAD state even when git succeeds (exit code 0), so the `git()` helper returns
the empty string, not the fallback `"-"`.

`default_branch` uses `git symbolic-ref --short refs/remotes/origin/HEAD` to discover the remote
default branch. That command returns `"origin/main"` or `"origin/master"`. The `.removeprefix`
call strips the `"origin/"` prefix so the stored value is just `"main"` or `"master"`. If the
command fails (no remote configured), the entire expression falls back to `"origin/main"` before
the strip, yielding `"main"`.

`status` is clipped at 1,500 chars — slightly longer than the doc clip because the status section
is a compact list format and is higher-priority information.

`recent_commits` filters empty strings from the splitlines result; this handles the case where
git returns a trailing newline that produces an empty final element.

**`WorkspaceContext.text()` (lines 127-144)**

```python
def text(self):
    commits = "\n".join(f"- {line}" for line in self.recent_commits) or "- none"
    docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()) or "- none"
    return textwrap.dedent(
        f"""\
        Workspace:
        - cwd: {self.cwd}
        - repo_root: {self.repo_root}
        - branch: {self.branch}
        - default_branch: {self.default_branch}
        - status:
        {self.status}
        - recent_commits:
        {commits}
        - project_docs:
        {docs}
        """
    ).strip()
```

`textwrap.dedent` removes the common leading whitespace from the multi-line string so the output
is left-aligned. The backslash after the opening `"""` prevents a leading newline. `.strip()` at
the end removes any trailing whitespace or newline.

The rendered output for a typical project looks like:

```
Workspace:
- cwd: /home/user/myproject
- repo_root: /home/user/myproject
- branch: feature/add-search
- default_branch: main
- status:
M  src/search.py
- recent_commits:
- a1b2c3d Add initial search implementation
- 9f8e7d6 Update README
- project_docs:
- README.md
# My Project
...
```

If this method were removed, the model would receive no grounding information about the workspace.
It would have to call `list_files` and `run_shell git status` at the start of every request,
consuming two of the six available tool steps.

---

## Component 2 — Prompt Shape and Cache Reuse

### What it does

A multi-turn coding session involves many model calls. Each call needs to provide the model with
its identity, its rules, its tool catalog, its workspace context, its working memory, the
conversation history, and the current request. The naive approach is to rebuild this entire string
on every model call. That works but is wasteful: the first four sections (identity, rules, tools,
workspace) are completely static for the duration of a session.

This component separates the prompt into a **static prefix** (built once in `build_prefix()` and
stored in `self.prefix`) and two **dynamic sections** (rebuilt each call via `memory_text()` and
`history_text()`). The final assembly happens in `prompt()`, which simply concatenates the three
parts.

The cache-reuse benefit is particularly relevant for local models served through Ollama. Many
Ollama backends implement KV-cache prefix matching: if the beginning of the prompt is identical
to the previous call, the cached KV state is reused and the time-to-first-token is dramatically
reduced. The static prefix enables this optimization automatically.

### Where in the code

| Symbol | Lines | Purpose |
|--------|-------|---------|
| `build_prefix` | 337-384 | Builds the static part of the prompt once |
| Tool catalog loop | 338-343 | Renders each tool as a one-line description |
| Example strings | 344-353 | Six examples of valid model responses |
| Rules block | 354-374 | 13 behavioral rules injected into every call |
| `self.prefix = self.build_prefix()` | 261 | Called once in `__init__`, stored permanently |
| `memory_text` | 386-396 | Dynamic section: task, files, notes |
| `prompt` | 430-443 | Assembles prefix + memory + transcript + request |

### Code walkthrough

**Tool catalog generation in `build_prefix` (lines 338-343)**

```python
def build_prefix(self):
    tool_lines = []
    for name, tool in self.tools.items():
        fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
        risk = "approval required" if tool["risky"] else "safe"
        tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
    tool_text = "\n".join(tool_lines)
```

Each tool in `self.tools` contributes one line. The `schema` dict stores argument names and their
type-with-default strings (e.g., `"int=1"`, `"str='.'"`). These are formatted as
`argname: type=default` and joined with commas. The risk label `[approval required]` or `[safe]`
tells the model which tools will prompt the user before execution. The description is the
`"description"` value from the tool registry.

A rendered tool line looks like:
```
- read_file(path: str, start: int=1, end: int=200) [safe] Read a UTF-8 file by line range.
```

If this loop were removed or the tool catalog omitted from the prefix, the model would have no
information about which tools are available or how to call them. It would either hallucinate tool
names or refuse to call tools altogether.

**Example responses (lines 344-353)**

```python
examples = "\n".join(
    [
        '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
        '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
        '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
        '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
        "<final>Done.</final>",
    ]
)
```

Six examples cover both the JSON format (`<tool>{...}</tool>`) and the XML format
(`<tool name="..." path="..."><content>...</content></tool>`). Small local models are sensitive to
format demonstrations. Without examples, many models will emit tool calls that parse correctly
from a human perspective but fail the `parse()` checks — for example, omitting the `args` key or
wrapping the payload in a list instead of a dict. The examples prime the model's output
distribution toward the exact formats the parser handles.

The `write_file` and `patch_file` examples use the XML format because multi-line file content
is difficult to embed in JSON (newlines must be `\n` escapes, indentation becomes fragile). The
XML format allows literal newlines inside the `<content>` block, which is far more reliable for
code generation.

**The rules block (lines 354-374)**

```python
return textwrap.dedent(
    f"""\
    You are Mini-Coding-Agent, a small local coding agent running through Ollama.

    Rules:
    - Use tools instead of guessing about the workspace.
    - Return exactly one <tool>...</tool> or one <final>...</final>.
    - Tool calls must look like:
      <tool>{{"name":"tool_name","args":{{...}}}}</tool>
    - For write_file and patch_file with multi-line text, prefer XML style:
      <tool name="write_file" path="file.py"><content>...</content></tool>
    - Final answers must look like:
      <final>your answer</final>
    - Never invent tool results.
    - Keep answers concise and concrete.
    - If the user asks you to create or update a specific file and the path is clear, use write_file or patch_file instead of repeatedly listing files.
    - Before writing tests for existing code, read the implementation first.
    - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
    - New files should be complete and runnable, including obvious imports.
    - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
    - Required tool arguments must not be empty. Do not call read_file, write_file, patch_file, run_shell, or delegate with args={{}}.

    Tools:
    {tool_text}

    Valid response examples:
    {examples}

    {self.workspace.text()}
    """
).strip()
```

The rules are behavioral constraints derived from observed failure modes of small local models.
Each one addresses a specific class of problem:

| Rule | Problem it prevents |
|------|---------------------|
| "Use tools instead of guessing" | Model fabricates directory listings or file contents |
| "Return exactly one `<tool>` or `<final>`" | Model emits two tool calls in one response |
| "Never invent tool results" | Model skips the tool call and writes a fictional result |
| "Use `write_file` or `patch_file` if path is clear" | Model wastes steps on `list_files` before creating a file |
| "Before writing tests, read the implementation first" | Model writes tests against a stale mental model of the code |
| "Match the current implementation when writing tests" | Model silently refactors code while writing tests |
| "New files should be complete and runnable" | Model writes a skeleton with `pass` bodies and no imports |
| "Do not repeat the same tool call" | Infinite loop when a tool call returns an unhelpful result |
| "Required arguments must not be empty" | Model calls `read_file` with `args={}`, triggering a validation error |

The double-braces `{{` and `}}` in the f-string are Python escape sequences for literal `{` and
`}` characters. Without the double-braces, Python would try to interpret `{"name":"tool_name"}`
as an f-string substitution and raise a `KeyError`.

**`self.prefix = self.build_prefix()` (line 261)**

```python
self.tools = self.build_tools()
self.prefix = self.build_prefix()
self.session_path = self.session_store.save(self.session)
```

`build_prefix` is called exactly once, in `__init__`, after `build_tools` (because it iterates
`self.tools`). The result is stored in `self.prefix`. Every subsequent call to `prompt()` reuses
this stored string. If `build_prefix` were called inside `prompt()`, the workspace context and
tool catalog would be recomputed on every model call, breaking KV-cache prefix matching.

**`memory_text()` (lines 386-396)**

```python
def memory_text(self):
    memory = self.session["memory"]
    return textwrap.dedent(
        f"""\
        Memory:
        - task: {memory['task'] or "-"}
        - files: {", ".join(memory["files"]) or "-"}
        - notes:
          {chr(10).join(f"- {note}" for note in memory["notes"]) or "- none"}
        """
    ).strip()
```

The memory section contains three fields: `task` (the original user request, captured on first
call), `files` (up to eight recently accessed file paths), and `notes` (up to five short
summaries of recent tool results). These fields are updated automatically by `note_tool()` after
each tool call.

`chr(10).join(...)` is used instead of `"\n".join(...)` inside the f-string because Python does
not allow a literal backslash `\n` inside `{}` expression slots in f-strings. `chr(10)` produces
the newline character at runtime without needing a backslash escape.

**`prompt()` (lines 430-443)**

```python
def prompt(self, user_message):
    return textwrap.dedent(
        f"""\
        {self.prefix}

        {self.memory_text()}

        Transcript:
        {self.history_text()}

        Current user request:
        {user_message}
        """
    ).strip()
```

The full prompt structure, in order:

```
+------------------------------------------------------+
| STATIC PREFIX                                        |
|  - Identity paragraph                                |
|  - Rules (13 items)                                  |
|  - Tool catalog (N tools, one line each)             |
|  - Response format examples (6 examples)             |
|  - Workspace snapshot (branch, status, docs, etc.)   |
+------------------------------------------------------+
| DYNAMIC: MEMORY                                      |
|  - task: (original request, clipped to 300 chars)    |
|  - files: (up to 8 recently touched files)           |
|  - notes: (up to 5 recent tool summaries)            |
+------------------------------------------------------+
| DYNAMIC: TRANSCRIPT                                  |
|  - History items, recency-weighted compression       |
+------------------------------------------------------+
| DYNAMIC: CURRENT USER REQUEST                        |
|  - The user_message repeated here                    |
+------------------------------------------------------+
```

The user message appears in two places: embedded in the transcript (as the most recent `[user]`
entry added by `record()`) and again as "Current user request" at the very bottom. This is
**attention anchoring**: transformer attention is weighted toward the beginning and end of the
context. Repeating the request at the end ensures the model does not lose track of what it is
supposed to do, especially when the transcript is long and pushes the original request toward
the middle.

---

## Component 3 — Structured Tools, Validation, and Permissions

### What it does

Unrestricted shell access would make the agent dangerous. But overly restricted access makes it
useless. This component defines a fixed **tool registry** — a dict of named operations with
schemas, risk flags, and runnable implementations — and enforces a five-gate execution pipeline
before any tool runs: unknown-tool check, argument validation, repeated-call detection, approval
gate, and execution with exception handling.

A parallel concern is the model's output format. Local models do not always emit valid JSON.
This component also implements the **response parser**, which handles JSON tool calls, XML tool
calls, bare-text answers, and malformed output (triggering a retry).

### Where in the code

| Symbol | Lines | Purpose |
|--------|-------|---------|
| `build_tools` | 286-332 | Defines the tool registry dict |
| `run_tool` | 511-530 | Five-gate execution pipeline |
| `repeated_tool_call` | 532-537 | Detects back-to-back identical calls |
| `tool_example` | 539-549 | Returns a usage example string per tool |
| `validate_tool` | 551-615 | Per-tool argument validation |
| `approve` | 617-628 | Three-mode approval gate |
| `parse` | 630-662 | JSON-first/XML-fallback/bare-text decision tree |
| `retry_notice` | 664-674 | Formats retry instructions for the model |
| `parse_xml_tool` | 676-697 | Regex-based XML tool call parser |
| `parse_attrs` | 699-704 | Parses `key="value"` attributes from XML open tag |
| `extract` | 706-717 | Extracts content between `<tag>` and `</tag>` (strips whitespace) |
| `extract_raw` | 719-730 | Same but preserves internal whitespace |
| `path` | 737-743 | Sandbox enforcement — resolves and validates paths |
| `tool_list_files` | 745-757 | Lists directory contents |
| `tool_read_file` | 759-769 | Reads file lines with line numbers |
| `tool_search` | 771-797 | ripgrep or fallback text search |
| `tool_run_shell` | 799-822 | Shell command execution |
| `tool_write_file` | 824-829 | Creates or overwrites a file |
| `tool_patch_file` | 831-845 | Replaces one exact occurrence of text in a file |

### Code walkthrough

**`build_tools()` — the tool registry (lines 286-332)**

```python
def build_tools(self):
    tools = {
        "list_files": {
            "schema": {"path": "str='.'"},
            "risky": False,
            "description": "List files in the workspace.",
            "run": self.tool_list_files,
        },
        "read_file": {
            "schema": {"path": "str", "start": "int=1", "end": "int=200"},
            "risky": False,
            "description": "Read a UTF-8 file by line range.",
            "run": self.tool_read_file,
        },
        "search": {
            "schema": {"pattern": "str", "path": "str='.'"},
            "risky": False,
            "description": "Search the workspace with rg or a simple fallback.",
            "run": self.tool_search,
        },
        "run_shell": {
            "schema": {"command": "str", "timeout": "int=20"},
            "risky": True,
            "description": "Run a shell command in the repo root.",
            "run": self.tool_run_shell,
        },
        "write_file": {
            "schema": {"path": "str", "content": "str"},
            "risky": True,
            "description": "Write a text file.",
            "run": self.tool_write_file,
        },
        "patch_file": {
            "schema": {"path": "str", "old_text": "str", "new_text": "str"},
            "risky": True,
            "description": "Replace one exact text block in a file.",
            "run": self.tool_patch_file,
        },
    }
    if self.depth < self.max_depth:
        tools["delegate"] = {
            "schema": {"task": "str", "max_steps": "int=3"},
            "risky": False,
            "description": "Ask a bounded read-only child agent to investigate.",
            "run": self.tool_delegate,
        }
    return tools
```

Each entry has four keys: `schema` (argument names mapped to type strings used for prompt
rendering), `risky` (whether the tool requires approval), `description` (the one-line prompt
description), and `run` (a bound method reference). The tool registry is both the prompt catalog
and the dispatch table — `run_tool` looks up `tool["run"]` and calls it.

Three tools are `risky`: `run_shell`, `write_file`, and `patch_file`. These are the tools that
modify state (filesystem or process execution). The three read-only tools (`list_files`,
`read_file`, `search`) are safe. `delegate` is listed as safe because the child agent's
`read_only=True` flag makes it incapable of modifying the filesystem regardless of what it tries.

The `delegate` tool is conditionally registered: it only appears when `self.depth < self.max_depth`.
This means the child agent's tool registry will not contain `delegate` at all — the option is
physically absent, not just blocked at runtime.

**`run_tool()` — the five-gate pipeline (lines 511-530)**

```python
def run_tool(self, name, args):
    tool = self.tools.get(name)
    if tool is None:
        return f"error: unknown tool '{name}'"
    try:
        self.validate_tool(name, args)
    except Exception as exc:
        example = self.tool_example(name)
        message = f"error: invalid arguments for {name}: {exc}"
        if example:
            message += f"\nexample: {example}"
        return message
    if self.repeated_tool_call(name, args):
        return f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
    if tool["risky"] and not self.approve(name, args):
        return f"error: approval denied for {name}"
    try:
        return clip(tool["run"](args))
    except Exception as exc:
        return f"error: tool {name} failed: {exc}"
```

Every gate returns an error **string** rather than raising an exception. This is a critical design
decision: the error string becomes the tool result, which is recorded in the session history and
included in the next prompt. The model sees the error and can adapt — for example, by correcting
the path argument or choosing a different tool. If the gates raised exceptions, the agent loop
would have to catch them, and the model would never learn what went wrong.

Gate 1: `self.tools.get(name)` returns `None` for unknown tool names. This handles hallucinated
tool names cleanly.

Gate 2: `validate_tool` raises `ValueError` on bad arguments. The error message includes a usage
example via `tool_example(name)`, so the model sees both what went wrong and how to fix it.

Gate 3: `repeated_tool_call` checks whether the last two tool events were identical. If the model
tried `read_file("foo.py")` and got a result, then immediately tries `read_file("foo.py")` again,
it is stuck in a loop. The error message explicitly says to choose a different tool.

Gate 4: `approve` is only checked for risky tools. For safe tools the flag is never consulted.

Gate 5: The actual `tool["run"](args)` call is wrapped in a try/except so that unexpected
implementation errors (e.g., a permission denied from the OS) return an error string rather than
crashing the agent loop. The result is passed through `clip` to enforce the `MAX_TOOL_OUTPUT`
limit.

**`repeated_tool_call()` (lines 532-537)**

```python
def repeated_tool_call(self, name, args):
    tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
    if len(tool_events) < 2:
        return False
    recent = tool_events[-2:]
    return all(item["name"] == name and item["args"] == args for item in recent)
```

Only the last two tool events are checked, not the entire history. This is intentional: if the
model called `read_file("a.py")` at step 1, then did other things, then called `read_file("a.py")`
again at step 8, that is legitimate — it may have modified the file in between. The guard only
catches the case where the model tries the same call twice in a row with no intervening progress.
Checking `args` equality is also strict: `read_file("a.py", start=1, end=200)` and
`read_file("a.py", start=1, end=400)` are different calls and would both be allowed.

**`validate_tool()` — per-tool argument checking (lines 551-615)**

```python
def validate_tool(self, name, args):
    args = args or {}

    if name == "list_files":
        path = self.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = self.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        self.path(args.get("path", "."))
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        return

    if name == "write_file":
        path = self.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "patch_file":
        path = self.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return

    if name == "delegate":
        if self.depth >= self.max_depth:
            raise ValueError("delegate depth exceeded")
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        return
```

Every validation branch calls `self.path(...)` on any path argument. This is the sandbox check —
it ensures the path resolves inside the workspace root. The `args or {}` at the top handles the
case where `args` is `None` (which can happen if the model emits `"args": null` in JSON).

The `patch_file` validation is the most complex: it reads the file and counts occurrences of
`old_text`. The rule is that `old_text` must appear **exactly once**. If it appears zero times,
the model gave the wrong snippet. If it appears multiple times, applying the replace would modify
the wrong occurrence. Both cases return a descriptive error with the actual count, so the model
can adjust its `old_text` to be more specific.

The `timeout` validation for `run_shell` clamps to `[1, 120]` seconds. Without an upper bound, a
model could request a 3600-second timeout for a command that hangs, blocking the entire process.

**`path()` — the workspace sandbox (lines 737-743)**

```python
def path(self, raw_path):
    path = Path(raw_path)
    path = path if path.is_absolute() else self.root / path
    resolved = path.resolve()
    if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
        raise ValueError(f"path escapes workspace: {raw_path}")
    return resolved
```

If `raw_path` is relative, it is joined to `self.root` (the repository root). If it is absolute,
it is used as-is. Then `.resolve()` expands symlinks and normalizes `..` components.

`os.path.commonpath([root, resolved])` returns the longest common leading path of the two
arguments. If `resolved` is inside `root`, the common path equals `root` exactly. If `resolved`
is `/etc/passwd` and `root` is `/home/user/myproject`, the common path would be `/` — not equal
to `root` — and the check raises `ValueError`.

Example: if `root = /home/user/project` and the model passes `path = "../../etc/passwd"`, then:
```
raw_path  = "../../etc/passwd"
joined    = /home/user/project/../../etc/passwd
resolved  = /etc/passwd
commonpath(["/home/user/project", "/etc/passwd"]) = "/"  !=  "/home/user/project"
=> raises ValueError("path escapes workspace: ../../etc/passwd")
```

> **Note:** `os.path.commonpath` returns a string, so the comparison uses `str(self.root)`. If
> `self.root` is a `Path` object with a trailing separator on some platforms, this comparison
> could fail. The code avoids this by always constructing `self.root = Path(workspace.repo_root)`
> in `__init__`, which never has a trailing separator.

**`approve()` (lines 617-628)**

```python
def approve(self, name, args):
    if self.read_only:
        return False
    if self.approval_policy == "auto":
        return True
    if self.approval_policy == "never":
        return False
    try:
        answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}
```

`read_only` is checked before the approval policy. A child agent spawned by `tool_delegate` has
`read_only=True`, which means it cannot execute risky tools regardless of what policy the parent
was started with. This is belt-and-suspenders: even if somehow `approval_policy="auto"` were
passed to the child, `read_only` would still block it.

The three policies: `"ask"` calls `input()` to prompt the user interactively. `"auto"` approves
all risky tools without prompting (useful in CI or batch mode). `"never"` denies all risky tools
(useful for inspection-only runs). `EOFError` is caught because `input()` raises `EOFError` when
stdin is closed (e.g., when the agent is run as a subprocess in a test or pipeline). In that case
the tool is denied, which is the safe default.

**`parse()` — the response decision tree (lines 630-662)**

```python
@staticmethod
def parse(raw):
    raw = str(raw)
    if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
        body = MiniAgent.extract(raw, "tool")
        try:
            payload = json.loads(body)
        except Exception:
            return "retry", MiniAgent.retry_notice("model returned malformed tool JSON")
        if not isinstance(payload, dict):
            return "retry", MiniAgent.retry_notice("tool payload must be a JSON object")
        if not str(payload.get("name", "")).strip():
            return "retry", MiniAgent.retry_notice("tool payload is missing a tool name")
        args = payload.get("args", {})
        if args is None:
            payload["args"] = {}
        elif not isinstance(args, dict):
            return "retry", MiniAgent.retry_notice()
        return "tool", payload
    if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
        payload = MiniAgent.parse_xml_tool(raw)
        if payload is not None:
            return "tool", payload
        return "retry", MiniAgent.retry_notice()
    if "<final>" in raw:
        final = MiniAgent.extract(raw, "final").strip()
        if final:
            return "final", final
        return "retry", MiniAgent.retry_notice("model returned an empty <final> answer")
    raw = raw.strip()
    if raw:
        return "final", raw
    return "retry", MiniAgent.retry_notice("model returned an empty response")
```

The parser implements a priority-ordered decision tree:

```
raw response received
        |
        v
  contains "<tool>" ?   ----YES----> JSON path: json.loads the body
        |                                 valid dict with name? -> return ("tool", payload)
        NO                                else -> return ("retry", notice)
        |
        v
  contains "<tool" ?    ----YES----> XML path: parse_xml_tool(raw)
        |                                 not None? -> return ("tool", payload)
        NO                                else -> return ("retry", notice)
        |
        v
  contains "<final>" ?  ----YES----> extract body
        |                                 non-empty? -> return ("final", text)
        NO                                else -> return ("retry", notice)
        |
        v
  non-empty raw text?   ----YES----> return ("final", raw)   [bare-text fallback]
        |
        NO
        v
  return ("retry", notice)
```

The `<tool>` check (exact tag with closing `>`) catches the JSON format. The `<tool` check
(open tag without `>`) catches the XML format where the tag has attributes. Both checks also
verify that if a `<final>` tag is present, the `<tool>` tag comes first — this handles models
that emit a `<tool>` call followed by a premature `<final>`, where the tool call should take
precedence.

The bare-text fallback (lines 659-661) handles models that do not emit any tags at all but
return a useful text answer. This degrades gracefully instead of looping on retry.

**`parse_xml_tool()` (lines 676-697)**

```python
@staticmethod
def parse_xml_tool(raw):
    match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.S)
    if not match:
        return None
    attrs = MiniAgent.parse_attrs(match.group("attrs"))
    name = str(attrs.pop("name", "")).strip()
    if not name:
        return None

    body = match.group("body")
    args = dict(attrs)
    for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
        if f"<{key}>" in body:
            args[key] = MiniAgent.extract_raw(body, key)

    body_text = body.strip("\n")
    if name == "write_file" and "content" not in args and body_text:
        args["content"] = body_text
    if name == "delegate" and "task" not in args and body_text:
        args["task"] = body_text.strip()
    return {"name": name, "args": args}
```

The regex `r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>"` has two named groups:
- `attrs`: everything between `<tool` and `>` — the opening tag's attribute string
- `body`: everything between `>` and `</tool>` — the element body

`re.S` (DOTALL) makes `.` match newlines, so `body` can span multiple lines.

`parse_attrs` extracts `key="value"` pairs from `attrs`. For a tag like
`<tool name="write_file" path="foo.py">`, `attrs` is the string ` name="write_file" path="foo.py"`.

After extracting the name, the remaining attributes become the initial `args` dict. Then for each
known nested element (`<content>`, `<old_text>`, etc.), if the element appears in `body`, its
content is extracted with `extract_raw` (which preserves internal whitespace — important for code).

The two fallback lines handle models that emit the body text directly without a wrapper element:
```xml
<tool name="write_file" path="hello.py">
print("hello")
</tool>
```
In this case `content` is not in `args` but `body_text` is non-empty, so the body becomes the
content. This is a permissive fallback that makes the parser more robust to minor format
deviations.

---

## Component 4 — Context Reduction and Output Management

### What it does

Local models through Ollama have a finite **context window** — typically 4,096 to 32,768 tokens.
A long coding session with many tool calls can easily exhaust this window. This component
implements two complementary strategies to keep the prompt within bounds.

The first strategy is **output clipping**: every tool result is clipped at `MAX_TOOL_OUTPUT`
(4,000 characters) before being stored. This prevents a single large file read from consuming
the entire context.

The second strategy is **history compression**: the `history_text()` function applies
recency-weighted limits and deduplication when rendering the transcript. Recent items get generous
space (900 chars for tool output, 900 chars for messages). Older items are compressed aggressively
(180 chars for tool output, 220 chars for messages). Duplicate `read_file` calls for the same
path in the non-recent window are deduplicated entirely.

### Where in the code

| Symbol | Lines | Purpose |
|--------|-------|---------|
| `MAX_TOOL_OUTPUT` | 36 | 4,000-char per-tool-result limit |
| `MAX_HISTORY` | 37 | 12,000-char overall transcript limit |
| `clip` | 56-60 | Truncates a string and appends a count |
| `middle` | 63-71 | Truncates to a fixed width, keeping both ends |
| `history_text` | 401-425 | Renders the compressed transcript |

### Code walkthrough

**`clip()` (lines 56-60)**

```python
def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
```

The truncation appends `"...[truncated N chars]"` rather than silently cutting. The model can
see that the output was cut and can use `read_file` with a `start`/`end` range to read the
missing portion. Without the truncation notice, the model might not realize the output was
incomplete and draw wrong conclusions from partial data.

`str(text)` at line 1 coerces any non-string input (e.g., an integer exit code) to a string
before checking the length. If this coercion were removed, calling `clip(0)` would fail with
`TypeError: object of type 'int' has no len()`.

**`middle()` (lines 63-71)**

```python
def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]
```

`middle` is used in `build_welcome()` for rendering truncated values in the startup banner — for
example, a very long workspace path is shown as `"/home/.../very/long/path"`. It is not used in
the prompt or history. Newlines are replaced with spaces because the banner is a fixed-width box.
The three-dot `...` uses exactly 3 characters, leaving `limit - 3` characters for content,
split evenly between left and right sides.

**`history_text()` — the compression algorithm (lines 401-425)**

```python
def history_text(self):
    history = self.session["history"]
    if not history:
        return "- empty"

    lines = []
    seen_reads = set()
    recent_start = max(0, len(history) - 6)
    for index, item in enumerate(history):
        recent = index >= recent_start
        if item["role"] == "tool" and item["name"] == "read_file" and not recent:
            path = str(item["args"].get("path", ""))
            if path in seen_reads:
                continue
            seen_reads.add(path)

        if item["role"] == "tool":
            limit = 900 if recent else 180
            lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
            lines.append(clip(item["content"], limit))
        else:
            limit = 900 if recent else 220
            lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

    return clip("\n".join(lines), MAX_HISTORY)
```

`recent_start = max(0, len(history) - 6)` marks the last 6 history items as "recent". Items
before this index are "old" and receive aggressive compression.

The `seen_reads` set implements deduplication for `read_file` tool calls in the old window. If
the model read `utils.py` at step 2 and again at step 5, and both are now in the old window, only
the first occurrence is shown. The second is skipped entirely via `continue`. This matters because
a long session might read the same file dozens of times as context for different modifications;
keeping all those copies would waste tokens for stale data.

The two-tier limits:

| Zone | Tool output limit | Message limit |
|------|-------------------|---------------|
| Recent (last 6 items) | 900 chars | 900 chars |
| Old (all earlier items) | 180 chars | 220 chars |

Tool args are rendered with `json.dumps(..., sort_keys=True)` so they are consistently ordered
regardless of how the model constructed the dict.

The final `clip("\n".join(lines), MAX_HISTORY)` is a safety net: if enough old items survive
deduplication and compression, the combined history could still exceed 12,000 chars. This clips
the entire transcript at that ceiling. The clip is applied to the joined string, not to individual
items, so it cuts from the bottom — preserving the most recent items at the expense of older ones.

**Concrete example**

Consider a session with 10 history items: items 0-3 are "old" (before `recent_start = 4`), items
4-9 are "recent".

```
History (10 items):
  [0] [user] "write a binary search function"           (old, limit 220)
  [1] [tool:list_files] {"path":"."}                    (old, limit 180)
       "[F] src/main.py\n[F] tests/..."
  [2] [tool:read_file] {"path":"src/main.py"}           (old, limit 180, first read)
       "   1: import sys\n   2: ..."
  [3] [tool:read_file] {"path":"src/main.py"}           (old, SKIPPED -- duplicate read)
  [4] [tool:write_file] {"content":"...","path":"..."}  (recent, limit 900)
       "wrote src/binary_search.py (320 chars)"
  [5] [tool:run_shell] {"command":"pytest -q"}          (recent, limit 900)
       "3 passed in 0.12s"
  [6] [assistant] "I've created binary_search.py..."   (recent, limit 900)
  [7] [user] "add a docstring"                          (recent, limit 900)
  [8] [tool:patch_file] {"old_text":"...","path":"..."}  (recent, limit 900)
       "patched src/binary_search.py"
  [9] [assistant] "Done. Added docstring."             (recent, limit 900)

Rendered output (simplified):
  [user] write a binary search function          <- clipped at 220 chars
  [tool:list_files] {"path":"."}                 <- args shown
  [F] src/main.py\n[F] tests/...                 <- content clipped at 180 chars
  [tool:read_file] {"path":"src/main.py","..."}  <- first read included
     1: import sys\n   2: ...                    <- clipped at 180 chars
  (item 3 skipped entirely -- duplicate read_file for src/main.py)
  [tool:write_file] {"content":"...","path":"..."} <- recent: full args
  wrote src/binary_search.py (320 chars)           <- recent: up to 900 chars
  ... (items 5-9 at full recent limits)
```

The model sees a compressed but coherent picture: the original task, the early exploration steps
in summary form, and the most recent steps at full fidelity.

---

## Component 5 — Transcripts, Memory, and Resumption

### What it does

Every action in a session must be durable. If the process crashes after a tool call but before
the model responds, the work done so far should not be lost. This component implements
**write-through persistence**: every `record()` call immediately writes the full session to disk
as a JSON file.

Beyond persistence, the component manages a distinction between two kinds of state. The **history**
is the complete raw transcript — every user message, tool call, and model response, in order.
The **memory** is a curated three-field summary: the original task, recently accessed files, and
notes. Memory is short (fits in a few hundred tokens) and is always shown in the prompt. History
is long and is shown in compressed form via `history_text()`.

### Where in the code

| Symbol | Lines | Purpose |
|--------|-------|---------|
| `SessionStore.__init__` | 151-153 | Creates the sessions directory |
| `SessionStore.save` | 158-161 | Writes JSON with `indent=2` |
| `SessionStore.load` | 163-164 | Reads and parses JSON |
| `SessionStore.latest` | 166-168 | Returns stem of most recently modified session |
| Session dict structure | 253-259 | The five top-level keys initialized in `__init__` |
| `record` | 448-450 | Appends to history and saves immediately |
| `note_tool` | 452-458 | Updates memory after a tool call |
| `remember` | 274-280 | LRU-dedup helper for bounded lists |
| `ask` (opening) | 460-464 | Task capture and user message recording |
| `reset` | 732-735 | Clears history and memory in-place |

### Code walkthrough

**`SessionStore` (lines 150-168)**

```python
class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def save(self, session):
        path = self.path(session["id"])
        path.write_text(json.dumps(session, indent=2), encoding="utf-8")
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None
```

`mkdir(parents=True, exist_ok=True)` creates the sessions directory and any missing parent
directories without error if the directory already exists. The sessions directory is
`.mini-coding-agent/sessions/` inside the repository root (set in `build_agent` at line 917).

`save` uses `json.dumps(session, indent=2)` — human-readable JSON with 2-space indentation.
This makes the session files inspectable and diff-friendly in git. The method returns the `Path`
object, which `record()` and `MiniAgent.__init__` store in `self.session_path` for display via
the `/session` command.

`load` is a pure read with no error handling: if the session file does not exist or is corrupted,
the `json.loads` will raise, which propagates to the caller. This is intentional — a missing
session file is a user error (wrong `--resume` argument) that should surface as an exception.

`latest()` sorts session files by `mtime` and returns the stem (filename without `.json`). The
stem is the session ID, which `build_agent` passes to `from_session`.

**Session dict structure (lines 253-259)**

```python
self.session = session or {
    "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
    "created_at": now(),
    "workspace_root": workspace.repo_root,
    "history": [],
    "memory": {"task": "", "files": [], "notes": []},
}
```

The session ID format is `YYYYMMDD-HHMMSS-<6hex>` — for example, `20260404-153022-a7f3b1`.
The timestamp prefix makes sessions sort chronologically. The six-character UUID suffix adds
collision resistance: two sessions started within the same second will have different IDs because
`uuid4()` is random. `uuid4().hex[:6]` takes just the first six hex characters, giving
16^6 = ~16 million possible suffixes.

The five top-level keys:

| Key | Type | Purpose |
|-----|------|---------|
| `id` | string | Unique session identifier |
| `created_at` | ISO 8601 string | Session creation timestamp |
| `workspace_root` | string | Absolute path to repo root at session creation |
| `history` | list | All recorded events in order |
| `memory` | dict | Curated summary: task, files, notes |

**`record()` (lines 448-450)**

```python
def record(self, item):
    self.session["history"].append(item)
    self.session_path = self.session_store.save(self.session)
```

Two lines, but they implement write-through persistence. Every history append is immediately
followed by a full file write. There is no batching. If the process is killed after line 1 but
before line 2, the in-memory history has the new item but the file does not. If the process is
killed after line 2, both are consistent. The cost is one file write per model call and one per
tool call — acceptable for a local development tool.

Removing the `save()` call would mean the session file is only written at the end of a successful
`ask()` call, losing all tool calls and partial progress if the process crashes mid-session.

**`note_tool()` (lines 452-458)**

```python
def note_tool(self, name, args, result):
    memory = self.session["memory"]
    path = args.get("path")
    if name in {"read_file", "write_file", "patch_file"} and path:
        self.remember(memory["files"], str(path), 8)
    note = f"{name}: {clip(str(result).replace(chr(10), ' '), 220)}"
    self.remember(memory["notes"], note, 5)
```

Two updates happen after every tool call. First, if the tool accessed a file, that path is added
to `memory["files"]` via `remember`. The limit of 8 ensures the files list stays compact. Second,
a one-line note summarizing the tool call is added to `memory["notes"]`. The note clips the result
to 220 chars and replaces newlines with spaces (via `chr(10)`) so the note occupies exactly one
line in the rendered memory.

`run_shell` and `search` and `list_files` do not update `memory["files"]` because they do not
access a specific file (or in `run_shell`'s case, it may touch many files indirectly). Only the
three file-content tools track specific paths.

**`remember()` (lines 274-280)**

```python
@staticmethod
def remember(bucket, item, limit):
    if not item:
        return
    if item in bucket:
        bucket.remove(item)
    bucket.append(item)
    del bucket[:-limit]
```

This is an LRU (least-recently-used) list with deduplication and a fixed maximum size. Walking
through a concrete example: `bucket = ["a.py", "b.py"]`, `limit = 3`.

- Call `remember(bucket, "a.py", 3)`:
  - `"a.py"` is already in `bucket` — remove it: `["b.py"]`
  - Append: `["b.py", "a.py"]`
  - `del bucket[:-3]` removes nothing (length is 2, limit is 3)
  - Result: `["b.py", "a.py"]` — "a.py" moved to the end (most recent)

- Call `remember(bucket, "c.py", 3)`:
  - `"c.py"` not in bucket
  - Append: `["b.py", "a.py", "c.py"]`
  - `del bucket[:-3]` removes nothing (length is 3)
  - Result: `["b.py", "a.py", "c.py"]`

- Call `remember(bucket, "d.py", 3)`:
  - `"d.py"` not in bucket
  - Append: `["b.py", "a.py", "c.py", "d.py"]`
  - `del bucket[:-3]` removes `bucket[0]` = `"b.py"`
  - Result: `["a.py", "c.py", "d.py"]`

`del bucket[:-limit]` is a Python idiom: `bucket[:-limit]` is everything except the last `limit`
items; `del` on that slice removes it in-place. This is more concise than `while len(bucket) > limit: bucket.pop(0)`.

**`ask()` opening (lines 460-464)**

```python
def ask(self, user_message):
    memory = self.session["memory"]
    if not memory["task"]:
        memory["task"] = clip(user_message.strip(), 300)
    self.record({"role": "user", "content": user_message, "created_at": now()})
```

`memory["task"]` is set only once — on the first call to `ask()`. Subsequent calls do not
overwrite it. This means the task always reflects the original request, not the most recent
follow-up. The task is shown in the memory section of every prompt, giving the model a persistent
anchor for what it is supposed to accomplish overall.

**`reset()` (lines 732-735)**

```python
def reset(self):
    self.session["history"] = []
    self.session["memory"] = {"task": "", "files": [], "notes": []}
    self.session_store.save(self.session)
```

`reset` clears history and memory but does NOT change the session ID. The session file is
overwritten with the empty state. This means a reset session file is indistinguishable from a
freshly created one except for the `"created_at"` timestamp. The `/reset` command in the REPL
calls this method — it is a clean-slate restart without leaving the session.

**What a session file looks like on disk**

After a two-turn conversation ("list files", "create hello.py"), the session file contains:

```json
{
  "id": "20260404-153022-a7f3b1",
  "created_at": "2026-04-04T15:30:22.000000+00:00",
  "workspace_root": "/home/user/myproject",
  "history": [
    {
      "role": "user",
      "content": "list the files in this project",
      "created_at": "2026-04-04T15:30:25.000000+00:00"
    },
    {
      "role": "tool",
      "name": "list_files",
      "args": {"path": "."},
      "content": "[F] README.md\n[F] pyproject.toml\n[D] src",
      "created_at": "2026-04-04T15:30:26.000000+00:00"
    },
    {
      "role": "assistant",
      "content": "The project contains README.md, pyproject.toml, and a src/ directory.",
      "created_at": "2026-04-04T15:30:27.000000+00:00"
    },
    {
      "role": "user",
      "content": "create hello.py",
      "created_at": "2026-04-04T15:30:40.000000+00:00"
    },
    {
      "role": "tool",
      "name": "write_file",
      "args": {"path": "hello.py", "content": "print('hello')\n"},
      "content": "wrote hello.py (16 chars)",
      "created_at": "2026-04-04T15:30:41.000000+00:00"
    },
    {
      "role": "assistant",
      "content": "Created hello.py.",
      "created_at": "2026-04-04T15:30:42.000000+00:00"
    }
  ],
  "memory": {
    "task": "list the files in this project",
    "files": ["hello.py"],
    "notes": [
      "list_files: [F] README.md [F] pyproject.toml [D] src",
      "write_file: wrote hello.py (16 chars)",
      "Created hello.py."
    ]
  }
}
```

The `memory.task` is the first user message. `memory.files` contains only `hello.py` because
`list_files` does not update the files list. `memory.notes` has three entries: two tool summaries
and the final answer (added by `ask()` after the loop via `self.remember(memory["notes"], ...)`).

---

## Component 6 — Delegation and Bounded Subagents

### What it does

Some investigation tasks are easier to handle with a fresh agent that has a narrow scope and a
clean history. For example, if the parent agent is in the middle of a complex refactoring and
needs to understand the structure of an unfamiliar module, delegating that inspection to a
child agent keeps the parent's context clean.

This component spawns a child `MiniAgent` with three hard constraints: it cannot write to the
filesystem (`read_only=True`), it has a reduced step budget (default 3), and it cannot recurse
further (`depth = parent.depth + 1`, blocked by `max_depth`). The child runs autonomously and
returns its final answer as a string, which becomes a tool result in the parent's history.

### Where in the code

| Symbol | Lines | Purpose |
|--------|-------|---------|
| `build_tools` conditional | 325-332 | Registers `delegate` only when `depth < max_depth` |
| `validate_tool("delegate")` | 609-614 | Belt-and-suspenders depth check, empty-task guard |
| `tool_delegate` | 850-869 | Spawns the child agent and returns its answer |

### Code walkthrough

**`build_tools()` conditional registration (lines 325-332)**

```python
if self.depth < self.max_depth:
    tools["delegate"] = {
        "schema": {"task": "str", "max_steps": "int=3"},
        "risky": False,
        "description": "Ask a bounded read-only child agent to investigate.",
        "run": self.tool_delegate,
    }
return tools
```

The `delegate` tool is absent from the child's tool registry entirely. This is not a runtime
block — the tool simply does not exist in the child's `self.tools` dict. If the child somehow
attempted to call `delegate`, `run_tool` would return `"error: unknown tool 'delegate'"` at
gate 1. The conditional registration makes the depth limit structurally enforced rather than
just policy-enforced.

`delegate` is listed as `risky: False`. The child agent's `read_only=True` flag means all risky
tools are denied in the child anyway. Marking `delegate` itself as safe means it does not require
user approval to initiate — the parent agent can spawn a child without interrupting the user.

**`validate_tool("delegate")` (lines 609-614)**

```python
if name == "delegate":
    if self.depth >= self.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")
    return
```

This is a second depth check, separate from the conditional registration. Even if somehow a child
agent had the `delegate` tool registered (e.g., in a test with a manipulated `depth`), this check
would still block it at validation time. The empty-task guard prevents the model from delegating
with no instructions, which would waste the child's entire step budget on confusion.

**`tool_delegate()` (lines 850-869)**

```python
def tool_delegate(self, args):
    if self.depth >= self.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")
    child = MiniAgent(
        model_client=self.model_client,
        workspace=self.workspace,
        session_store=self.session_store,
        approval_policy="never",
        max_steps=int(args.get("max_steps", 3)),
        max_new_tokens=self.max_new_tokens,
        depth=self.depth + 1,
        max_depth=self.max_depth,
        read_only=True,
    )
    child.session["memory"]["task"] = task
    child.session["memory"]["notes"] = [clip(self.history_text(), 300)]
    return "delegate_result:\n" + child.ask(task)
```

Every constructor argument has a specific justification:

| Argument | Value | Why |
|----------|-------|-----|
| `model_client` | `self.model_client` | Reuses the same Ollama connection — no new HTTP client |
| `workspace` | `self.workspace` | Child sees the same filesystem and git state |
| `session_store` | `self.session_store` | Child writes its own session file to the same store |
| `approval_policy` | `"never"` | All risky tools denied regardless of parent policy |
| `max_steps` | `args.get("max_steps", 3)` | Caller controls the budget; default is 3 |
| `max_new_tokens` | `self.max_new_tokens` | Same token limit as parent |
| `depth` | `self.depth + 1` | Prevents further nesting |
| `max_depth` | `self.max_depth` | Propagates the ceiling unchanged |
| `read_only` | `True` | `approve()` returns `False` immediately for all risky tools |

After constructing the child, two memory fields are set before calling `ask()`:

`child.session["memory"]["task"] = task` — sets the task so it appears in the memory section
of every child prompt. Without this, the child's first prompt would show `task: -` and the child
would have to re-derive the goal from the user message alone.

`child.session["memory"]["notes"] = [clip(self.history_text(), 300)]` — injects a 300-char
summary of the parent's history into the child's notes. This gives the child context about what
the parent has already done, preventing duplicate work.

The return value `"delegate_result:\n" + child.ask(task)` wraps the child's final answer with
a prefix. This prefix appears in the parent's tool history as the result of the `delegate` call,
making it clear to the parent that this result came from a subordinate agent, not from a direct
tool operation.

**Agent tree diagram**

```
Parent MiniAgent (depth=0, max_depth=1)
  - tools: list_files, read_file, search, run_shell,
           write_file, patch_file, delegate
  - read_only: False
  - approval_policy: "ask" (or configured value)
  - max_steps: 6
        |
        | tool_delegate(task="summarize the test coverage")
        |
        v
  Child MiniAgent (depth=1, max_depth=1)
    - tools: list_files, read_file, search
             (NO run_shell, write_file, patch_file -- risky, denied by read_only)
             (NO delegate -- depth >= max_depth, not registered)
    - read_only: True
    - approval_policy: "never"
    - max_steps: 3
    - memory.task: "summarize the test coverage"
    - memory.notes: ["<300-char summary of parent history>"]
        |
        | child.ask("summarize the test coverage")
        | -> calls list_files, read_file, search
        | -> returns "delegate_result:\nTests cover 83% of src/..."
        |
        v
  result stored in parent's tool history as:
  [tool:delegate] {"max_steps": 3, "task": "summarize the test coverage"}
  delegate_result:
  Tests cover 83% of src/...
```

The child's session is written to disk as a separate JSON file with its own session ID. After the
parent session ends, both files persist and can be examined independently.

---

## How the 6 Components Wire Together at Runtime

The following annotated flow traces a single user request — `"create a hello.py"` — from the
`main()` entry point to the printed answer. Each activation is annotated with its component.

```
main(argv)
  |
  +--> build_arg_parser().parse_args(argv)
  |
  +--> build_agent(args)                                [setup]
  |      |
  |      +--> WorkspaceContext.build(args.cwd)          [Component 1]
  |      |      - runs git commands (branch, status, commits)
  |      |      - reads AGENTS.md, README.md, pyproject.toml
  |      |      - returns WorkspaceContext instance
  |      |
  |      +--> SessionStore(repo_root / ".mini-coding-agent/sessions")
  |      |      - creates directory if absent              [Component 5]
  |      |
  |      +--> OllamaModelClient(model, host, ...)
  |      |
  |      +--> MiniAgent(model_client, workspace, store, ...)
  |             |
  |             +--> build_tools()                       [Component 3]
  |             |      - registers list_files, read_file, search,
  |             |        run_shell, write_file, patch_file
  |             |      - registers delegate (depth=0 < max_depth=1)
  |             |
  |             +--> build_prefix()                      [Component 2]
  |             |      - renders tool catalog
  |             |      - embeds 13 rules + 6 examples
  |             |      - embeds workspace.text()         [Component 1]
  |             |      - stores in self.prefix (never rebuilt)
  |             |
  |             +--> session_store.save(session)         [Component 5]
  |
  +--> print(build_welcome(...))
  |
  +--> agent.ask("create a hello.py")                   [agent loop]
         |
         +--> memory["task"] = "create a hello.py"      [Component 5]
         |
         +--> record({"role":"user", ...})               [Component 5]
         |      - appends to history, writes session file
         |
         [loop iteration 1]
         |
         +--> prompt("create a hello.py")               [Component 2]
         |      - assembles prefix + memory_text()
         |        + history_text()                       [Component 4]
         |        + "Current user request: create a hello.py"
         |
         +--> model_client.complete(prompt, 512)
         |      - HTTP POST to Ollama /api/generate
         |      - returns raw string, e.g.:
         |        '<tool name="write_file" path="hello.py">
         |         <content>print("hello")\n</content></tool>'
         |
         +--> parse(raw)                                 [Component 3]
         |      - detects "<tool" with attributes
         |      - calls parse_xml_tool(raw)
         |      - returns ("tool", {"name":"write_file",
         |                          "args":{"path":"hello.py",
         |                                  "content":"print(\"hello\")\n"}})
         |
         +--> run_tool("write_file", args)               [Component 3]
         |      - gate 1: tool exists in self.tools
         |      - gate 2: validate_tool("write_file", args)
         |          - path("hello.py") resolves to /workspace/hello.py
         |          - commonpath check passes (inside root)
         |          - "content" key present
         |      - gate 3: repeated_tool_call() -> False (no prior calls)
         |      - gate 4: tool["risky"]=True, call approve()  [Component 3]
         |          - approval_policy="ask": prints prompt to user
         |          - user enters "y"
         |          - returns True
         |      - gate 5: tool_write_file(args)
         |          - writes "print(\"hello\")\n" to hello.py
         |          - returns "wrote hello.py (16 chars)"
         |      - clip("wrote hello.py (16 chars)") -> unchanged
         |
         +--> record({"role":"tool", "name":"write_file", ...}) [Component 5]
         |      - history now has 2 items (user + tool)
         |      - session file updated on disk
         |
         +--> note_tool("write_file", args, result)      [Component 5]
         |      - remember(memory["files"], "hello.py", 8)
         |      - remember(memory["notes"], "write_file: wrote hello.py (16 chars)", 5)
         |
         [loop iteration 2]
         |
         +--> prompt("create a hello.py")               [Component 2]
         |      - same prefix (static, cached)
         |      - memory_text() now shows files: hello.py and the write_file note
         |      - history_text() now shows [user] and [tool:write_file] entries  [Component 4]
         |
         +--> model_client.complete(prompt, 512)
         |      - returns "<final>Created hello.py with a basic print statement.</final>"
         |
         +--> parse(raw)                                 [Component 3]
         |      - detects "<final>"
         |      - extracts "Created hello.py with a basic print statement."
         |      - returns ("final", "Created hello.py with a basic print statement.")
         |
         +--> record({"role":"assistant", "content":"Created...", ...})  [Component 5]
         |      - session file updated on disk
         |
         +--> remember(memory["notes"], "Created hello.py...", 5)  [Component 5]
         |
         +--> return "Created hello.py with a basic print statement."
         |
  +--> print("Created hello.py with a basic print statement.")
```

The six components activate in a fixed sequence every session:

1. **Component 1** runs once at startup, during `WorkspaceContext.build()`.
2. **Component 3** (`build_tools`) and **Component 2** (`build_prefix`) run once in `MiniAgent.__init__`.
3. For each turn, **Component 5** (`record`) fires immediately when the user message arrives.
4. For each model call, **Component 2** (`prompt`) assembles the full prompt, calling **Component 4** (`history_text`) for the compressed transcript.
5. After each tool call, **Component 3** handles parsing, validation, and approval; **Component 5** handles recording and memory updates.
6. **Component 6** is activated only when the model emits a `delegate` tool call, spinning up a nested instance that traverses the same pipeline at depth+1.
