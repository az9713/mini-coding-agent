# Delegation

This document explains the delegation system: how a parent agent spawns a
child agent, what constraints the child operates under, how context flows
between them, and when delegation is and is not the right tool.

---

## Overview

**Delegation** is the mechanism that lets the agent ask itself a question. When
the model calls `delegate`, it creates a new `MiniAgent` instance — the
**child agent** — assigns it a focused task, and waits for it to return a
result. The child uses the same model, the same workspace, and the same session
store, but it starts with a fresh history, a bounded step count, and a strict
read-only policy. The parent receives the child's final answer as a tool
result string and continues from there.

The primary use case is investigation without side effects. When the model is
uncertain whether a function exists, what a file contains, or whether there are
tests for a module, it can delegate that question to a child agent rather than
directly reading files itself. Because the child cannot write, patch, or run
shell commands, delegation is a safe way to gather information before
committing to an action.

---

## When To Use Delegation

Delegation is well-suited to tasks where the answer requires reading multiple
files or searching the codebase and where the parent agent should not interrupt
its own tool-step budget to do that reading:

- "What does the `process()` function in `main.py` actually do?"
- "Are there any existing tests for the `auth` module?"
- "Which files import from `utils.py`?"
- "Summarize the error handling approach in the codebase."

Delegation is the wrong tool when the task requires writing, patching, or
running commands. The child is unconditionally read-only: it cannot create
files, modify files, or execute shell commands. Any `write_file`, `patch_file`,
or `run_shell` call the child attempts will be denied at the `approve()`
gate before any execution occurs. The child silently receives
`"error: approval denied for <tool>"` and must find another way or give up.

---

## The Delegation Call

The model invokes delegation using the same two formats as any other tool.

**JSON format:**

```
<tool>{"name":"delegate","args":{"task":"inspect the error handling in main.py","max_steps":3}}</tool>
```

**XML format:**

```
<tool name="delegate" max_steps="2">inspect the README and summarize dependencies</tool>
```

In the XML format, the task text can appear either as a `<task>` child element
or as the raw body text of the `<tool>` element. Both are accepted by
`parse_xml_tool()`. The `max_steps` argument is optional; it defaults to `3`
if not specified.

---

## What Happens Inside `tool_delegate()`

The following diagram shows the full lifecycle of a delegation call:

```
Parent agent calls delegate(task, max_steps)
        |
        v
  validate_tool()
    - depth < max_depth?  (if not: error, delegation blocked)
    - task not empty?     (if not: error)
        |
        v
  Create child MiniAgent:
    model_client  = parent.model_client   (same Ollama connection)
    workspace     = parent.workspace      (same repo snapshot)
    session_store = parent.session_store  (writes its own .json file)
    approval_policy = "never"             (risky tools auto-denied)
    max_steps     = args["max_steps"]     (default 3)
    max_new_tokens = parent.max_new_tokens
    depth         = parent.depth + 1      (increments depth counter)
    max_depth     = parent.max_depth
    read_only     = True                  (approve() always False)
        |
        v
  Inject context into child memory:
    child.session["memory"]["task"]  = task
    child.session["memory"]["notes"] = [clip(parent.history_text(), 300)]
        |
        v
  child.ask(task)
    - child runs its own tool loop
    - reads files, searches, lists
    - returns a <final> answer
        |
        v
  Return "delegate_result:\n" + child_answer
        |
        v
  Parent records result as a tool event
  Parent continues its own loop
```

The child's `ask()` call is synchronous and blocking. The parent's loop is
paused while the child runs to completion. The child's entire execution —
all its tool calls, model turns, and its final answer — happens before the
parent receives any result.

---

## Safety Constraints

Five distinct constraints work together to make delegation safe. Each
addresses a different failure mode.

### 1. `read_only=True` — blocks all file modification

`approve()` checks `self.read_only` first, before checking the approval
policy:

```python
def approve(self, name, args):
    if self.read_only:
        return False
    ...
```

Because `read_only=True` for all child agents, `approve()` returns `False`
for any risky tool, regardless of the approval policy. The risky tools are
`run_shell`, `write_file`, and `patch_file`. If the child attempts any of
them, it receives `"error: approval denied for <tool>"` and must proceed
differently or issue a final answer.

### 2. `approval_policy="never"` — belt-and-suspenders

Even if `read_only` were somehow bypassed, `approval_policy="never"` would
deny risky tools at the policy check. Both checks must be cleared for a risky
tool to execute, and child agents fail at the first check.

### 3. `depth = parent.depth + 1` — prevents runaway nesting

`build_tools()` only registers the `delegate` tool when
`self.depth < self.max_depth`. A child agent at `depth=1` with `max_depth=1`
does not have `delegate` in its tool list at all. The tool simply does not
exist from the child's perspective.

```
depth=0 (root agent)   →  max_depth=1  →  delegate tool: PRESENT
depth=1 (child agent)  →  max_depth=1  →  delegate tool: ABSENT
```

If the model somehow produces a `delegate` call from the child, it receives
`"error: unknown tool 'delegate'"` from `run_tool()`. The depth check in
`validate_tool()` provides a second line of defence (it raises
`"delegate depth exceeded"`), but it is never reached because `delegate` is
not in the child's tool registry.

### 4. `max_steps` cap — prevents runaway child execution

The child's step budget defaults to `3` if not specified. The parent can
increase it by passing `max_steps`, but the child is always bounded by an
explicit limit. A child that cannot solve its task in the allotted steps
returns `"Stopped after reaching the step limit without a final answer."` as
its result. The parent receives this string as the delegate result and can
decide what to do.

### 5. Conditional tool registration — structural enforcement

The `delegate` tool is not registered when `depth >= max_depth`. This is a
structural guarantee: the tool literally does not exist in the child's tool
registry, as opposed to existing but being blocked at runtime. Structural
prevention is more robust than runtime checking because it removes the
possibility of a code path that bypasses the runtime check.

---

## Depth Limit

The depth system uses two instance variables: `self.depth` (current nesting
level) and `self.max_depth` (the maximum permitted level).

```
Top-level agent:  depth=0, max_depth=1
                  Has: list_files, read_file, search, run_shell,
                       write_file, patch_file, delegate

Child agent:      depth=1, max_depth=1
                  Has: list_files, read_file, search
                  (run_shell, write_file, patch_file denied at approve())
                  (delegate absent from tool registry)
```

The default `max_depth=1` permits exactly one level of delegation. The agent
that the user interacts with is at depth 0 and can delegate. Any agent it
spawns is at depth 1 and cannot delegate further.

Increasing `max_depth` to `2` would permit grandchildren, but this is not the
default and adds complexity: each level of nesting doubles the potential number
of model calls and session files. For most investigation tasks, one level of
delegation is sufficient.

---

## Context Passing

The child receives two pieces of context from the parent before its first
model call:

```python
child.session["memory"]["task"] = task
child.session["memory"]["notes"] = [clip(self.history_text(), 300)]
```

**The task string** is set directly as `memory["task"]`. This is the same
field that the agent normally populates from the first user message. Because
it is set before `child.ask(task)` is called, and because `ask()` only sets
`memory["task"]` when the field is empty, the injected task string is
preserved unchanged throughout the child's execution.

**The parent's history** is compressed via `history_text()` (which applies the
tiered truncation described in `session-memory.md`) and then further clipped
to 300 characters. This clipped string is injected as the only entry in
`memory["notes"]`. It gives the child just enough context to understand what
the parent has already done without receiving the full transcript.

The 300-character limit is deliberately tight. It is enough for a sentence or
two summarizing recent events ("read main.py, found process() at line 42") but
not enough to recreate the parent's full working context. The child is meant
to perform a focused investigation, not to continue the parent's task.

Context flows one way: from parent to child via the notes injection, and from
child to parent via the return value. The child cannot read the parent's
session directly, and the parent does not see the child's session history
unless it reads the session file directly.

---

## What the Parent Sees Back

The return value of `tool_delegate()` is always a string beginning with
`"delegate_result:\n"` followed by whatever the child returned as its final
answer.

**Example — successful investigation:**

```
delegate_result:
main.py contains a single `process()` function starting at line 42. It reads
from stdin line by line and calls `transform()` on each line. Error handling
catches only `IOError`; no `ValueError` or `TypeError` guards are present.
No test files for this module exist under tests/.
```

**Example — child hit step limit:**

```
delegate_result:
Stopped after reaching the step limit without a final answer.
```

**Example — child found nothing relevant:**

```
delegate_result:
No imports of utils.py were found anywhere in the src/ directory.
```

The parent receives this string as the `content` field in a `"tool"` history
item, exactly like any other tool result. It is clipped to 4,000 characters
by the standard `clip()` call in `run_tool()` before being stored. From the
parent's perspective, delegation is indistinguishable from any other tool
result: a string appears, and the parent decides what to do next.

---

## Session Side Effects

Each `tool_delegate()` call creates a new child `MiniAgent` instance, and the
child's `__init__` immediately calls `session_store.save()`. This means every
delegation creates a new session file on disk, even if the child runs for only
one step.

The child's session file is stored in the same `.mini-coding-agent/sessions/`
directory as the parent's, with a different session ID. A session with two
delegation calls will produce two additional session files, named by the time
the child agents were created.

These child session files are useful for debugging: if a delegation produces
an unexpected result, the child's session file contains the full record of
every tool call the child made and every model response it received. The files
accumulate over time and are not automatically cleaned up.

---

## Limitations

Delegation is a bounded tool and has several inherent limitations that are
worth understanding before relying on it.

**The child cannot write files.** This is a hard constraint enforced by
`read_only=True`. If the investigation reveals that a fix is needed, the child
cannot make it. The child can only describe what it found; the parent must do
the writing.

**The child has a limited step budget.** With the default `max_steps=3`, a
complex investigation that requires reading many files may be truncated. When
this happens, the child returns the step-limit message and the parent has
received an incomplete answer. The parent can either increase `max_steps` on
a subsequent delegation call or handle the investigation directly.

**Context passing is one-way and lossy.** The child receives at most 300
characters of the parent's history. For a parent that has done extensive work
before delegating, the child may lack important context. The child has no way
to ask the parent for more context.

**The child creates its own session file.** This is a side effect that
accumulates over time. In a long session with many delegations, the sessions
directory fills with child session files. There is no built-in mechanism to
associate a child session file with the parent delegation call that created it.

**Delegation adds model call overhead.** Each step the child takes is a full
Ollama API call. A delegation with `max_steps=3` can add up to three
additional model calls to the parent's request. For slow models or limited
hardware, this latency is noticeable.
