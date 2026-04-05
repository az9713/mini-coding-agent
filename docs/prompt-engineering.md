# Prompt Engineering

## Overview

The prompt is the model's entire world. Every fact the agent knows about the workspace — the current git branch, which files were recently touched, what tools are available, what happened in earlier turns — arrives exclusively through a single string assembled fresh on every model call. There is no hidden state, no side channel, no persistent model memory between calls. If something is not in the prompt, the model does not know it. This document dissects how that string is built, why each section is structured the way it is, and how aggressive compression keeps a multi-turn session from overwhelming a local model's limited context window.

---

## The Four-Part Prompt

Every call to `model_client.complete()` receives the output of `prompt(user_message)`. That method concatenates four sections in a fixed order:

```
+----------------------------------------------------------+
|  PART 1: PREFIX (static, built once at __init__)        |
|                                                          |
|  - Agent identity ("You are Mini-Coding-Agent...")      |
|  - 12 rules the model must follow                       |
|  - Tool catalog: names, schemas, risk flags             |
|  - 6 valid response examples                            |
|  - Workspace snapshot: git state + project docs         |
+----------------------------------------------------------+
|  PART 2: MEMORY (dynamic, updated each turn)           |
|                                                          |
|  - task: first user message, clipped to 300 chars       |
|  - files: LRU list of up to 8 recently touched paths   |
|  - notes: LRU list of up to 5 distilled summaries      |
+----------------------------------------------------------+
|  PART 3: TRANSCRIPT (dynamic, compressed)              |
|                                                          |
|  - Every user message, assistant response, and         |
|    tool result from the current session                 |
|  - Recent items shown in full; old items truncated     |
|  - Duplicate read_file calls for the same path skipped |
|  - Total capped at 12,000 characters                   |
+----------------------------------------------------------+
|  PART 4: CURRENT REQUEST                               |
|                                                          |
|  - The user's latest message, repeated verbatim        |
+----------------------------------------------------------+
```

The `prompt()` method assembles these parts with `textwrap.dedent` and `.strip()`:

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

Each section has a distinct job, and the order is deliberate. Instructions come first, before any variable content, so the model reads its rules before it reads any context that might distract it. The current request comes last, immediately before the model generates its next token, so it is the freshest thing in the model's attention window.

---

## Part 1: The Prefix

The prefix is built by `build_prefix()`, which is called from `__init__` and
stored as `self.prefix`. In most sessions the prefix is static — every model
call across the entire session uses the identical string.

There is one exception: the `/forget` REPL command deletes `AGENT_MEMORY.md`
and immediately calls `build_prefix()` again to produce a prefix without the
persistent memory section. This is the only time the prefix is rebuilt mid-session.

This near-static design is intentional. Local models like `qwen3.5:4b` have
limited and somewhat inconsistent instruction-following. A stable prefix means
the model sees exactly the same rules, tool schemas, and examples every time.
There is no drift in phrasing across turns, which reduces the chance that a
slight rephrasing on turn 4 causes the model to interpret a rule differently
than it did on turn 1.

The prefix has up to six subsections, depending on configuration:

The first two subsections (identity/rules and persistent memory) are always present if applicable; the rest follow in fixed order.

### Agent identity and rules

```
You are Mini-Coding-Agent, a small local coding agent running through Ollama.

Rules:
- Use tools instead of guessing about the workspace.
- Return exactly one <tool>...</tool> or one <final>...</final>.
...
```

The identity line anchors the model's role. The rules section follows immediately and is listed below in full with commentary.

### Persistent memory (conditional)

If `AGENT_MEMORY.md` exists in the workspace root, its contents are injected
immediately after the identity/rules block:

```
Persistent memory (from AGENT_MEMORY.md):
- 2026-04-05: this project uses black for formatting
- 2026-04-05: all API endpoints require JWT authentication
```

This section is omitted entirely when the file does not exist. The model can
add notes to this file using the `update_memory` tool; the notes then appear
here in every subsequent session without user repetition.

### Planning rule (conditional)

When the agent is started with `--plan`, one additional rule is appended to
the rules section:

```
Before using any tools, emit a numbered <plan>...</plan> block listing the
steps you intend to take. Wait for user confirmation before proceeding.
```

This rule is absent by default. It instructs the model to reason through its
approach before acting, and the `parse()` function looks for the `<plan>` tag
as a dedicated response kind. See `agent-loop.md` for how plan responses are
handled.

### Tool catalog

`build_prefix()` iterates over `self.tools` and emits one line per tool:

```python
for name, tool in self.tools.items():
    fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
    risk = "approval required" if tool["risky"] else "safe"
    tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
```

The rendered output looks like:

```
Tools:
- list_files(path: str='.') [safe] List files in the workspace.
- read_file(path: str, start: int=1, end: int=200) [safe] Read a UTF-8 file by line range.
- search(pattern: str, path: str='.') [safe] Search the workspace with rg or a simple fallback.
- run_shell(command: str, timeout: int=20) [approval required] Run a shell command in the repo root.
- write_file(path: str, content: str) [approval required] Write a text file.
- patch_file(path: str, old_text: str, new_text: str) [approval required] Replace one exact text block in a file.
- delegate(task: str, max_steps: int=3) [safe] Ask a bounded read-only child agent to investigate.
```

Each entry communicates three things: the call signature with argument types and defaults, the risk level, and a plain-English description. The schema uses a compact type-annotation style (`str`, `int`, `str='.'`) rather than full JSON Schema, which is shorter and more token-efficient.

> **Note:** The `delegate` tool only appears in the catalog if `self.depth < self.max_depth`. A child agent spawned by `delegate` does not see `delegate` in its tool list and cannot recurse further.

### Valid response examples

Six concrete examples follow the tool catalog:

```python
examples = "\n".join([
    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
    '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
    '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
    '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
    "<final>Done.</final>",
])
```

These examples serve as a few-shot template. Rather than describing the output format in prose, the agent shows the model exactly what valid output looks like. The examples cover both formats (JSON and XML), four different tools, and the final answer tag. Small local models benefit substantially from concrete examples because they have been trained on far less code-generation data than large models.

### Workspace snapshot

The last subsection of the prefix is the output of `workspace.text()`, which is built by `WorkspaceContext.build()` at startup:

```
Workspace:
- cwd: /home/user/project
- repo_root: /home/user/project
- branch: main
- default_branch: main
- status:
 M src/utils.py
- recent_commits:
- a3f1c2d Fix off-by-one in binary_search
- 9b22e1a Add initial tests
- project_docs:
- README.md
  # My Project
  ...
```

The workspace snapshot includes:
- `cwd` and `repo_root`: the absolute paths the agent is sandboxed to
- `branch` and `default_branch`: current git state
- `status`: `git status --short`, clipped to 1500 chars — shows which files are dirty
- `recent_commits`: last 5 commit subjects from `git log --oneline -5`
- `project_docs`: contents of `AGENTS.md`, `README.md`, `pyproject.toml`, `package.json` if present, each clipped to 1200 chars

Because the workspace snapshot is part of the static prefix, it reflects the state of the repository at the moment the agent was launched, not the current state. If the agent writes a file and then calls `read_file`, the agent's edits appear in the transcript, not in the prefix.

---

## Part 2: Working Memory

`memory_text()` renders the three fields of `session["memory"]` into a short structured section:

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

A concrete rendered example looks like this:

```
Memory:
- task: Add a binary search function to utils.py and write a test for it
- files: src/utils.py, tests/test_utils.py
- notes:
  - read_file: # src/utils.py    1: def linear_search(nums, target): ...
  - write_file: wrote src/utils.py (312 chars)
  - run_shell: exit_code: 0 stdout: ...... 2 passed in 0.12s
```

Each field in memory has a specific job:

**`task`** is set on the first call to `ask()` and never overwritten. It is the original user message clipped to 300 characters. Even after many tool steps and assistant turns scroll through the transcript, the task field always reminds the model what it was originally asked to do. Without this, a model deep in a long session might lose track of the original goal.

```python
if not memory["task"]:
    memory["task"] = clip(user_message.strip(), 300)
```

**`files`** is an LRU list capped at 8 entries, maintained by `note_tool()`. Whenever the agent calls `read_file`, `write_file`, or `patch_file`, the file path is added to the front of this list (and the oldest entry is dropped if the list exceeds 8 items). This gives the model a running record of which files it has actually touched in the current session, preventing it from forgetting a file it read several turns ago and trying to read it again from scratch.

**`notes`** is an LRU list capped at 5 entries. After every tool call, `note_tool()` prepends a one-line summary: the tool name and a 220-character clip of the result. After every final answer, the answer itself (clipped to 220 chars) is added to notes. The notes list is a distillation of the agent's progress — what it found, what it changed, what it concluded — in a compact form that always fits in the prompt regardless of how much history has accumulated.

The memory section is dynamic but small. Its total size is bounded: the task is at most 300 chars, the files list at most ~8 * 60 = ~480 chars, and the notes list at most 5 * 220 = ~1100 chars. This is approximately 2000 characters for a saturated memory, which is a modest and predictable cost.

---

## Part 3: History Compression

`history_text()` is where most of the prompt engineering complexity lives. A session can accumulate dozens of history items, and naive inclusion of all of them at full length would quickly overflow the context window of a local model. The method applies several compression strategies simultaneously.

### The full compression table

| Item age | Item type | Character limit |
|----------|-----------|-----------------|
| Recent (last 6 items) | tool output | 900 chars |
| Recent (last 6 items) | user/assistant message | 900 chars |
| Old (before last 6) | tool output | 180 chars |
| Old (before last 6) | user/assistant message | 220 chars |
| Old, duplicate `read_file` for same path | any | skipped entirely |

The total history string is capped at `MAX_HISTORY = 12000` characters by a final `clip()` call.

### The "recent window" concept

```python
recent_start = max(0, len(history) - 6)
for index, item in enumerate(history):
    recent = index >= recent_start
    ...
    limit = 900 if recent else 180   # for tool output
```

The last 6 history items are "recent" and receive a generous 900-character limit. Everything before them is "old" and is compressed to 180 characters for tool output or 220 characters for messages. The rationale is that the most recent tool results are directly relevant to what the model should do next, while older results are typically context that only needs to be summarized.

The number 6 is not arbitrary. With `--max-steps 6` (the default), the last 6 items span roughly one full round of tool use. A typical sequence is: `[user] request → [tool] result → [assistant] response → [user] followup → [tool] result → [tool] result`. The recent window keeps this immediate context in full fidelity.

### Duplicate read_file suppression

```python
if item["role"] == "tool" and item["name"] == "read_file" and not recent:
    path = str(item["args"].get("path", ""))
    if path in seen_reads:
        continue  # skip this history item entirely
    seen_reads.add(path)
```

If the model read `src/utils.py` on turn 2 and again on turn 5, the old (turn 2) read event is skipped entirely from the transcript if turn 5's read is also old. Only the most recent read of each file path survives in the old section of the transcript. This is a targeted optimization for the most common expensive operation: file reads. A large file read produces hundreds of characters of output; keeping every historical read of the same file would waste context budget on information already superseded by the latest read.

> **Note:** Suppression only applies to items in the "old" zone (before the last 6 items). If both reads are in the recent window, both are included.

### Before and after example

Suppose the session history contains these items after 4 tool steps:

```
[user]      List the files in the project
[tool:list_files]  [D] src   [F] README.md   [F] pyproject.toml
[assistant] I can see the project structure. Let me read utils.py.
[tool:read_file]   # src/utils.py
                   1: def linear_search(nums, target):
                   2:     for i, v in enumerate(nums):
                   3:         if v == target:
                   4:             return i
                   5:     return -1
                   ...200 lines of content...
[tool:read_file]   (same file, second read after edits)
                   # src/utils.py (updated content, 200 lines)
[tool:write_file]  wrote src/utils.py (412 chars)
```

With `recent_start = max(0, 6 - 6) = 0` this would be all-recent (only 6 items). But in a longer session, the first four items would fall into the "old" zone and be compressed:

**Old zone output (before compression):**
```
[user] List the files in the project
[tool:list_files] {"path":"."}
[D] src   [F] README.md   [F] pyproject.toml

[assistant] I can see the project structure. Let me read utils.py.
[tool:read_file] {"path":"src/utils.py","start":1,"end":200}
# src/utils.py
   1: def linear_search...
   ...1800 chars of file content...
```

**Old zone output (after compression):**
```
[user] List the files in the project
[tool:list_files] {"path":"."}
[D] src   [F] README.md   [F] pyp...[truncated 12 chars]

[assistant] I can see the project structure. Let me read utils.py.
[tool:read_file] {"path":"src/utils.py","start":1,"end":200}   <- SKIPPED (duplicate)
```

The first `read_file` result is suppressed because a newer read of the same path exists. The `list_files` result is clipped from ~50 chars to 180 chars. The assistant message is clipped from ~60 chars to 220 chars (fits without truncation here). The net saving is roughly 1800 characters — the full file content from the first read — which the model does not need to see because it will see the updated file in the recent section.

### The 12,000 character ceiling

After all item-level compression, the entire transcript string is clipped to 12,000 characters:

```python
return clip("\n".join(lines), MAX_HISTORY)  # MAX_HISTORY = 12000
```

This is the last safety valve. If a session runs for many steps with large tool outputs, item-level compression may not be sufficient to fit everything. The ceiling ensures the transcript section never exceeds a fixed budget regardless of session length. When truncation occurs, the oldest items — which are already compressed — are dropped first because `clip()` truncates from the right end of the string, and the string is built oldest-first.

---

## Part 4: Current Request Repetition

The user's message appears twice in the final prompt: once in the transcript section (as a `[user]` history item) and again at the very end under the "Current user request" heading.

```
...
[user] Add a binary search function to utils.py and write tests.
[tool:read_file] {"path":"src/utils.py"}
  1: def linear_search...
...

Current user request:
Add a binary search function to utils.py and write tests.
```

This repetition is intentional and addresses a specific failure mode in long-context inference. After reading the full transcript — potentially thousands of characters of tool results, assistant narration, and intermediate steps — the model's attention distribution is spread across all of that content. The last message in the transcript is often several turns back. By placing the original request at the very bottom of the prompt, immediately before the model generates its next token, the implementation ensures the model's next action is anchored to what was actually asked rather than to whatever appeared most recently in the transcript.

Think of it as a reminder card attached to the end of a long briefing document. Everything in the briefing is relevant, but the card at the end says: "and ultimately, this is what you need to produce."

---

## The Rules Section

The rules section in the prefix lists 12 instructions. Each rule addresses a specific failure mode observed in local model behavior.

```
Rules:
1.  Use tools instead of guessing about the workspace.
2.  Return exactly one <tool>...</tool> or one <final>...</final>.
3.  Tool calls must look like:
      <tool>{"name":"tool_name","args":{...}}</tool>
4.  For write_file and patch_file with multi-line text, prefer XML style:
      <tool name="write_file" path="file.py"><content>...</content></tool>
5.  Final answers must look like:
      <final>your answer</final>
6.  Never invent tool results.
7.  Keep answers concise and concrete.
8.  If the user asks you to create or update a specific file and the path
    is clear, use write_file or patch_file instead of repeatedly listing files.
9.  Before writing tests for existing code, read the implementation first.
10. When writing tests, match the current implementation unless the user
    explicitly asked you to change the code.
11. New files should be complete and runnable, including obvious imports.
12. Do not repeat the same tool call with the same arguments if it did not
    help. Choose a different tool or return a final answer.
13. Required tool arguments must not be empty. Do not call read_file,
    write_file, patch_file, run_shell, or delegate with args={}.
```

> **Note:** The source code contains 13 listed bullet points. The prompt header says "Rules:" without numbering; the numbering above is added here for reference.

**Why each rule exists:**

Rule 1 ("Use tools instead of guessing") addresses hallucination. Without this instruction, a model asked "what functions are in utils.py?" might answer from training data rather than reading the actual file. This rule pushes the model toward grounding every claim in observed tool results.

Rule 2 ("Return exactly one") prevents compound outputs. Some models produce `<tool>...</tool><final>...</final>` in a single response. The `parse()` function handles this by checking which tag appears first, but the rule reduces the frequency of this ambiguity.

Rules 3 and 4 are format specifications with examples. They are combined with the examples section below the rules to create a multi-exposure format training signal. The model sees the format described in prose (rules) and demonstrated in code (examples).

Rule 5 mirrors Rule 3 for the final-answer tag. Keeping it as a separate rule — rather than grouping it with rule 2 — makes it easier for the model to locate the final-answer format when it is ready to conclude.

Rule 6 ("Never invent tool results") is a guardrail against a common hallucination pattern where a model, rather than calling a tool to check something, fabricates a plausible-looking tool result in its response. The `parse()` function catches fabricated results that are formatted as tool calls, but rule 6 discourages the underlying behavior.

Rule 7 ("Keep answers concise and concrete") manages `max_new_tokens`. With a default limit of 512 tokens, the model must be concise. This rule reduces the chance that the model wastes tokens on preamble ("Certainly! I'd be happy to help with that...") instead of the actual answer.

Rule 8 ("use write_file or patch_file instead of repeatedly listing files") targets a common failure sequence: the model lists files, reads a file, then lists files again instead of writing the edit it already knows how to make. This rule short-circuits that loop.

Rules 9 and 10 ("Before writing tests, read the implementation first" and "match the current implementation") prevent a common test-writing failure: generating tests against an imagined interface rather than the actual code. Rule 10 specifically prevents the model from silently changing function signatures in tests without changing the implementation.

Rule 11 ("New files should be complete and runnable, including obvious imports") prevents the model from writing files that look correct in isolation but fail at runtime due to missing `import` statements.

Rule 12 ("Do not repeat the same tool call") reinforces the `repeated_tool_call()` check in `run_tool()`. The check is enforced at runtime, but the rule discourages the behavior before execution.

Rule 13 ("Required tool arguments must not be empty") catches a specific failure where the model emits `<tool>{"name":"read_file","args":{}}</tool>`, omitting the required `path` argument. Without this rule, the model sometimes produces empty `args` as a placeholder when it is uncertain about the right value.

---

## Token Budget Awareness

Local models typically have effective context windows of 4,000 to 8,000 tokens. The table below shows approximate token costs (at ~4 chars per token) for each prompt section in a typical 6-step session.

| Section | Approximate characters | Approximate tokens |
|---------|----------------------|-------------------|
| Part 1: Agent identity + rules | ~700 | ~175 |
| Part 1: Tool catalog (8 tools) | ~560 | ~140 |
| Part 1: Response examples (6 examples) | ~500 | ~125 |
| Part 1: Workspace snapshot (git + 1 doc) | ~1,200 | ~300 |
| **Part 1 total (prefix)** | **~2,900** | **~725** |
| Part 2: Memory (saturated) | ~2,000 | ~500 |
| Part 3: Transcript (6 steps, compressed) | ~4,000-8,000 | ~1,000-2,000 |
| Part 4: Current request | ~100-300 | ~25-75 |
| **Total** | **~9,000-13,200** | **~2,250-3,300** |

These estimates show why the 12,000-character `MAX_HISTORY` ceiling was chosen. Keeping the transcript under 12,000 characters keeps the total prompt under roughly 15,000 characters (~3,750 tokens), which fits comfortably in a 4,096-token context window even in the worst case.

The prefix is the single largest fixed cost at ~725 tokens per call. This is why it is built once and cached. Rebuilding the prefix on every turn would not change its content — it is entirely static — but it would add unnecessary work. Caching `self.prefix` as a string means `prompt()` is a simple string concatenation on every call.

The transcript section is the only part that grows with session length. All other compression strategies exist to slow that growth and keep the total prompt within the model's context window for as long as possible.
