# Persistent Agent Memory Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give the agent a persistent, human-editable long-term memory that survives across sessions and `/reset`, stored as `AGENT_MEMORY.md` in the workspace root.

**Architecture:** Add a safe `update_memory(note)` tool that appends a dated bullet to `AGENT_MEMORY.md`. At startup, `build_prefix()` reads the file (if it exists) and injects its content into the system prompt so the model always has access to past observations. A `/forget` REPL command clears the file. No new classes; ~60 lines of production code.

**Tech Stack:** Python stdlib only. All changes in `mini_coding_agent.py` and `tests/test_mini_coding_agent.py`.

---

## Task 1: `update_memory` tool — write and append to AGENT_MEMORY.md

**Files:**
- Modify: `mini_coding_agent.py` — `build_tools()` (around line 432), add `tool_update_memory()` method, add to `validate_tool()`, add to `tool_example()`
- Test: `tests/test_mini_coding_agent.py` (append at end)

**Step 1: Write the failing tests**

```python
def test_update_memory_creates_file(tmp_path):
    """update_memory creates AGENT_MEMORY.md when it does not exist."""
    agent = build_agent(tmp_path, [])
    agent.run_tool("update_memory", {"note": "user prefers pytest over unittest"})
    mem_file = tmp_path / "AGENT_MEMORY.md"
    assert mem_file.exists()
    assert "user prefers pytest over unittest" in mem_file.read_text(encoding="utf-8")

def test_update_memory_appends_dated_bullet(tmp_path):
    """update_memory appends a new dated bullet on each call."""
    agent = build_agent(tmp_path, [])
    agent.run_tool("update_memory", {"note": "first note"})
    agent.run_tool("update_memory", {"note": "second note"})
    content = (tmp_path / "AGENT_MEMORY.md").read_text(encoding="utf-8")
    assert "first note" in content
    assert "second note" in content
    assert content.index("first note") < content.index("second note")

def test_update_memory_rejects_empty_note(tmp_path):
    """update_memory raises ValueError for blank notes."""
    agent = build_agent(tmp_path, [])
    with pytest.raises(ValueError, match="note"):
        agent.run_tool("update_memory", {"note": "   "})
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_update_memory_creates_file -v
```
Expected: FAIL — `KeyError` or similar (tool doesn't exist yet)

**Step 3: Add `tool_update_memory` method to `MiniAgent` (after `tool_patch_file`)**

```python
def tool_update_memory(self, args):
    note = args["note"].strip()
    if not note:
        raise ValueError("note must not be empty")
    mem_path = self.root / "AGENT_MEMORY.md"
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bullet = f"- [{date_str}] {note}\n"
    with mem_path.open("a", encoding="utf-8") as fh:
        fh.write(bullet)
    return f"memory updated: {note}"
```

**Step 4: Register in `build_tools()` (after `patch_file`, before the `delegate` block)**

```python
"update_memory": {
    "schema": {"note": "str"},
    "risky": False,
    "description": "Append a persistent note to AGENT_MEMORY.md in the workspace root.",
    "run": self.tool_update_memory,
},
```

**Step 5: Add to `validate_tool()` (after the `patch_file` block)**

```python
if name == "update_memory":
    if not args.get("note", "").strip():
        raise ValueError("note must not be empty")
    return
```

**Step 6: Add to `tool_example()` (after the `patch_file` entry)**

```python
"update_memory": '<tool>{"name":"update_memory","args":{"note":"project uses pytest and uv"}}</tool>',
```

**Step 7: Run tests**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_update_memory_creates_file tests/test_mini_coding_agent.py::test_update_memory_appends_dated_bullet tests/test_mini_coding_agent.py::test_update_memory_rejects_empty_note -v
```
Expected: 3 passed

**Step 8: Commit**

```bash
git add mini_coding_agent.py tests/test_mini_coding_agent.py
git commit -m "feat: add update_memory tool that appends dated bullets to AGENT_MEMORY.md"
```

---

## Task 2: Inject AGENT_MEMORY.md into system prompt via `build_prefix()`

**Files:**
- Modify: `mini_coding_agent.py:483–530` (`build_prefix()`)
- Test: `tests/test_mini_coding_agent.py`

**Step 1: Write the failing tests**

```python
def test_persistent_memory_injected_into_prefix(tmp_path):
    """When AGENT_MEMORY.md exists, its content appears in the system prompt prefix."""
    (tmp_path / "AGENT_MEMORY.md").write_text(
        "- [2026-01-01] user prefers black formatter\n", encoding="utf-8"
    )
    agent = build_agent(tmp_path, [])
    assert "user prefers black formatter" in agent.prefix

def test_no_persistent_memory_file_leaves_prefix_clean(tmp_path):
    """When AGENT_MEMORY.md is absent, the prefix contains no persistent memory section."""
    agent = build_agent(tmp_path, [])
    assert "Persistent memory" not in agent.prefix
    assert "AGENT_MEMORY" not in agent.prefix
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_persistent_memory_injected_into_prefix -v
```
Expected: FAIL — `AssertionError` (prefix doesn't include file content yet)

**Step 3: Add memory injection at the end of `build_prefix()`**

In `build_prefix()`, replace the final `return textwrap.dedent(...)` call so that the f-string includes a `{persistent_memory_section}` placeholder. Before the return, build the section:

```python
mem_path = self.root / "AGENT_MEMORY.md"
if mem_path.is_file():
    mem_content = mem_path.read_text(encoding="utf-8").strip()
    persistent_memory_section = f"\nPersistent memory (from AGENT_MEMORY.md):\n{mem_content}\n"
else:
    persistent_memory_section = ""
```

Then add `{persistent_memory_section}` inside the return f-string, after `{self.workspace.text()}` and before the closing `"""`:

```python
            {self.workspace.text()}
            {persistent_memory_section}
            """
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_persistent_memory_injected_into_prefix tests/test_mini_coding_agent.py::test_no_persistent_memory_file_leaves_prefix_clean -v
```
Expected: 2 passed

**Step 5: Commit**

```bash
git add mini_coding_agent.py tests/test_mini_coding_agent.py
git commit -m "feat: inject AGENT_MEMORY.md content into system prompt prefix"
```

---

## Task 3: Persistent memory survives `/reset` and new sessions

**Files:**
- Modify: `mini_coding_agent.py:903` (`reset()` method)
- Test: `tests/test_mini_coding_agent.py`

**Step 1: Write the failing tests**

```python
def test_persistent_memory_survives_reset(tmp_path):
    """AGENT_MEMORY.md is not cleared by /reset."""
    agent = build_agent(tmp_path, [])
    agent.run_tool("update_memory", {"note": "survives reset"})
    agent.reset()
    mem_file = tmp_path / "AGENT_MEMORY.md"
    assert mem_file.exists()
    assert "survives reset" in mem_file.read_text(encoding="utf-8")

def test_persistent_memory_visible_to_new_agent_instance(tmp_path):
    """A second agent pointing at the same workspace sees existing AGENT_MEMORY.md in prefix."""
    # Write the memory file directly (simulating a prior session)
    (tmp_path / "AGENT_MEMORY.md").write_text(
        "- [2026-01-01] always use type hints\n", encoding="utf-8"
    )
    agent2 = build_agent(tmp_path, [])
    assert "always use type hints" in agent2.prefix
```

**Step 2: Run to verify they pass (or fail)**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_persistent_memory_survives_reset tests/test_mini_coding_agent.py::test_persistent_memory_visible_to_new_agent_instance -v
```
Expected: both should PASS already — `reset()` only clears session history/memory, not workspace files. Confirm. If they pass, no code change needed.

**Step 3: Run full suite**

```bash
uv run pytest tests/test_mini_coding_agent.py -v
```
Expected: all existing + new tests pass

**Step 4: Commit**

```bash
git add tests/test_mini_coding_agent.py
git commit -m "test: verify persistent memory survives reset and new sessions"
```

---

## Task 4: `/forget` REPL command + update HELP_TEXT and HELP_DETAILS

**Files:**
- Modify: `mini_coding_agent.py:18` (`HELP_TEXT` constant)
- Modify: `mini_coding_agent.py:27` (`HELP_DETAILS` constant)
- Modify: `mini_coding_agent.py:1153` (`main()` — REPL command handlers)
- Test: `tests/test_mini_coding_agent.py`

**Step 1: Write the failing test**

```python
def test_forget_clears_agent_memory_file(tmp_path):
    """/forget deletes AGENT_MEMORY.md from the workspace."""
    mem_file = tmp_path / "AGENT_MEMORY.md"
    mem_file.write_text("- [2026-01-01] some note\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])
    # Simulate /forget by calling the helper directly (tested at unit level)
    agent.forget_persistent_memory()
    assert not mem_file.exists()

def test_forget_is_noop_when_no_file(tmp_path):
    """/forget does not raise when AGENT_MEMORY.md does not exist."""
    agent = build_agent(tmp_path, [])
    agent.forget_persistent_memory()  # must not raise
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_forget_clears_agent_memory_file -v
```
Expected: FAIL — `AttributeError: 'MiniAgent' object has no attribute 'forget_persistent_memory'`

**Step 3: Add `forget_persistent_memory()` method to `MiniAgent` (after `reset()`)**

```python
def forget_persistent_memory(self):
    """Delete AGENT_MEMORY.md if present; rebuild prefix to remove injected content."""
    mem_path = self.root / "AGENT_MEMORY.md"
    if mem_path.is_file():
        mem_path.unlink()
    self.prefix = self.build_prefix()
```

**Step 4: Update `HELP_TEXT` (line 18)**

```python
HELP_TEXT = "/help, /memory, /session, /rewind, /diff, /forget, /reset, /exit"
```

**Step 5: Update `HELP_DETAILS` (line 27) — add `/forget` after `/diff N`**

```
/forget    Clear persistent memory (deletes AGENT_MEMORY.md).
```

**Step 6: Add REPL handler in `main()` (after the `/diff` handler block)**

```python
if user_input == "/forget":
    agent.forget_persistent_memory()
    print("persistent memory cleared")
    continue
```

**Step 7: Run tests**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_forget_clears_agent_memory_file tests/test_mini_coding_agent.py::test_forget_is_noop_when_no_file -v
```
Expected: 2 passed

**Step 8: Run full suite**

```bash
uv run pytest tests/test_mini_coding_agent.py -v
```
Expected: all tests pass

**Step 9: Commit**

```bash
git add mini_coding_agent.py tests/test_mini_coding_agent.py
git commit -m "feat: /forget command clears AGENT_MEMORY.md and updates HELP_TEXT"
```

---

## Task 5: End-to-end test + full suite

**Files:**
- Test: `tests/test_mini_coding_agent.py`

**Step 1: Write the end-to-end test**

```python
def test_end_to_end_update_memory_then_read_in_next_session(tmp_path):
    """Full flow: model calls update_memory, second agent sees it in prefix."""
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"update_memory","args":{"note":"always write docstrings"}}</tool>',
            "<final>Memory saved.</final>",
        ],
        approval_policy="auto",
    )
    answer = agent.ask("remember to always write docstrings")
    assert answer == "Memory saved."

    # New agent instance for the same workspace — simulates a fresh session
    agent2 = build_agent(tmp_path, [])
    assert "always write docstrings" in agent2.prefix
```

**Step 2: Run**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_end_to_end_update_memory_then_read_in_next_session -v
```
Expected: 1 passed

**Step 3: Run full suite**

```bash
uv run pytest tests/test_mini_coding_agent.py -v
```
Expected: all tests pass (at minimum 44 — 36 prior + 8 new from Tasks 1–5)

**Step 4: Commit**

```bash
git add tests/test_mini_coding_agent.py
git commit -m "test: end-to-end persistent memory flow"
```

---

## Task 6: Update docs and push

**Files:**
- Modify: `README.md`
- Modify: `docs/cli-reference.md`
- Modify: `docs/how-it-works.md`
- Modify: `docs/design-decisions.md`
- Modify: `HELP_TEXT` and `HELP_DETAILS` are already done in Task 4

**Step 1: Update `README.md`**

Add a "Persistent Agent Memory" section after "Auto-Verify Tests":

```markdown
## Persistent Agent Memory

The agent can accumulate long-term memory that persists across sessions and survives `/reset`.

Ask the model to remember something:

```
mini-coding-agent> remember that this project uses black for formatting
```

The model calls `update_memory("this project uses black for formatting")`, which appends a dated bullet to `AGENT_MEMORY.md` in the workspace root. On every subsequent session the file is injected into the system prompt automatically.

Clear all persistent memory:

```
mini-coding-agent> /forget
```

`AGENT_MEMORY.md` is a plain Markdown file — you can edit it by hand at any time.
```

**Step 2: Update `docs/cli-reference.md`**

Add `/forget` to the REPL commands table and add an `update_memory` entry to the tools reference section.

**Step 3: Update `docs/how-it-works.md`**

Add one sentence to the Component 2 walkthrough noting that `build_prefix()` reads `AGENT_MEMORY.md` when present and injects its content.

**Step 4: Update `docs/design-decisions.md`**

Add "Why Persistent Agent Memory?" section at end:

> Distilled session memory (Component 5) is volatile — it resets when the session does. Users working on the same codebase across multiple sessions repeatedly re-explain context that the agent already learned. `AGENT_MEMORY.md` solves this by giving the model a write path to its own long-term context. The file is human-readable and editable, so users stay in control. Storing it in the workspace root (rather than the agent's dot-directory) keeps it visible and version-controllable.

**Step 5: Run full suite one final time**

```bash
uv run pytest tests/test_mini_coding_agent.py -v
```
Expected: all tests pass

**Step 6: Commit and push**

```bash
git add mini_coding_agent.py tests/test_mini_coding_agent.py README.md docs/cli-reference.md docs/how-it-works.md docs/design-decisions.md
git commit -m "docs: persistent agent memory feature documentation"
git push origin main
```
