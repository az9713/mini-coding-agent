# Structured Planning Phase Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Before executing a multi-step task, the agent emits a numbered plan and pauses for user confirmation; execution only begins once the plan is approved.

**Architecture:** Add `<plan>` as a fourth valid response kind alongside `<tool>`, `<final>`, and retry. When `plan_mode=True`, inject a planning rule into the system prompt; the model's first response to any new task will be a `<plan>` block. The agent displays it, prompts for confirmation (respecting `approval_policy`), injects an "approved" message into history so the model stops re-planning, then continues the normal tool loop. No new classes; ~80 lines of production code.

**Tech Stack:** Python stdlib only. All changes in `mini_coding_agent.py` and `tests/test_mini_coding_agent.py`.

---

## Task 1: Parse `<plan>` tags

**Files:**
- Modify: `mini_coding_agent.py:760–830` (the `parse()` static method and related helpers)
- Test: `tests/test_mini_coding_agent.py` (append at end)

**Step 1: Write the failing tests**

```python
def test_parse_returns_plan_kind():
    kind, payload = MiniAgent.parse("<plan>\n1. Read file\n2. Write fix\n</plan>")
    assert kind == "plan"
    assert "Read file" in payload

def test_parse_plan_not_shadowed_by_final():
    # <plan> takes precedence over any stray <final> that appears after it
    kind, payload = MiniAgent.parse("<plan>1. Do it</plan><final>done</final>")
    assert kind == "plan"

def test_parse_empty_plan_becomes_retry():
    kind, _ = MiniAgent.parse("<plan>   </plan>")
    assert kind == "retry"
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_parse_returns_plan_kind -v
```
Expected: FAIL — `AssertionError` (parse returns "retry" or "final", not "plan")

**Step 3: Add `<plan>` handling to `parse()` (before the `<tool>` check)**

In `mini_coding_agent.py`, inside `parse()` (around line 780), add at the very top of the method body before the existing `if "<tool>"` line:

```python
if "<plan>" in raw and ("<tool>" not in raw or raw.find("<plan>") < raw.find("<tool>")):
    plan_text = MiniAgent.extract(raw, "plan").strip()
    if plan_text:
        return "plan", plan_text
    return "retry", MiniAgent.retry_notice("model returned an empty <plan> block")
```

**Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_parse_returns_plan_kind tests/test_mini_coding_agent.py::test_parse_plan_not_shadowed_by_final tests/test_mini_coding_agent.py::test_parse_empty_plan_becomes_retry -v
```
Expected: 3 passed

**Step 5: Commit**

```bash
git add mini_coding_agent.py tests/test_mini_coding_agent.py
git commit -m "feat: parse <plan> tag as new response kind"
```

---

## Task 2: Confirm plan in `ask()` — new `_confirm_plan()` method

**Files:**
- Modify: `mini_coding_agent.py:606–658` (`ask()` method) and `mini_coding_agent.py:903` (near `reset()`, add new method)
- Test: `tests/test_mini_coding_agent.py`

**Step 1: Write the failing tests**

```python
def test_plan_confirmation_prompts_user_when_ask_policy(tmp_path):
    """approval_policy='ask' calls input() for plan confirmation."""
    agent = build_agent(
        tmp_path,
        [
            "<plan>\n1. Read file\n2. Write fix\n</plan>",
            "<final>Done.</final>",
        ],
        approval_policy="ask",
    )
    with patch("builtins.input", return_value="y"):
        answer = agent.ask("fix the bug")
    assert answer == "Done."

def test_plan_cancelled_returns_early(tmp_path):
    """Saying 'n' at plan confirmation returns 'Plan cancelled.' immediately."""
    agent = build_agent(
        tmp_path,
        ["<plan>\n1. Read file\n2. Write fix\n</plan>"],
        approval_policy="ask",
    )
    with patch("builtins.input", return_value="n"):
        answer = agent.ask("fix the bug")
    assert answer == "Plan cancelled."

def test_plan_auto_approval_skips_input(tmp_path):
    """approval_policy='auto' approves the plan without calling input()."""
    agent = build_agent(
        tmp_path,
        [
            "<plan>\n1. Read file\n2. Write fix\n</plan>",
            "<final>Done.</final>",
        ],
        approval_policy="auto",
    )
    with patch("builtins.input") as mock_input:
        answer = agent.ask("fix the bug")
    assert answer == "Done."
    mock_input.assert_not_called()
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_plan_confirmation_prompts_user_when_ask_policy -v
```
Expected: FAIL — `parse()` returns `("plan", ...)` but `ask()` doesn't handle it yet, falling through to retry

**Step 3: Add `_confirm_plan()` method to `MiniAgent` (after `_auto_verify`, around line 700)**

```python
def _confirm_plan(self):
    if self.approval_policy == "auto":
        return True
    if self.approval_policy == "never":
        return False
    try:
        answer = input("execute plan? [Y/n] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"", "y", "yes"}
```

**Step 4: Handle `"plan"` kind in `ask()` loop (around line 644, after the `if kind == "tool":` block)**

Add after `if kind == "retry":` block and before the `final = ...` line:

```python
if kind == "plan":
    if self.verbose:
        print(f"\nProposed plan:\n{payload}\n")
    if not self._confirm_plan():
        final = "Plan cancelled."
        self.record({"role": "assistant", "content": final, "created_at": now()})
        return final
    # Inject approval into history so model knows to proceed with tools
    approval_msg = f"Plan approved:\n{payload}\nProceed with execution."
    self.record({"role": "assistant", "content": approval_msg, "created_at": now()})
    self.remember(memory["notes"], clip(f"approved plan: {payload}", 300), 5)
    continue
```

**Step 5: Run tests**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_plan_confirmation_prompts_user_when_ask_policy tests/test_mini_coding_agent.py::test_plan_cancelled_returns_early tests/test_mini_coding_agent.py::test_plan_auto_approval_skips_input -v
```
Expected: 3 passed

**Step 6: Commit**

```bash
git add mini_coding_agent.py tests/test_mini_coding_agent.py
git commit -m "feat: handle plan confirmation in ask() loop"
```

---

## Task 3: Plan does not consume tool-step budget

**Files:**
- Modify: `mini_coding_agent.py:614–658` (`ask()` loop counters)
- Test: `tests/test_mini_coding_agent.py`

**Step 1: Write the failing test**

```python
def test_plan_does_not_consume_tool_step_budget(tmp_path):
    """A <plan> response does not count against max_steps."""
    agent = build_agent(
        tmp_path,
        [
            "<plan>\n1. Read\n2. Write\n</plan>",
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"list_files","args":{"path":"docs"}}</tool>',
            "<final>Done.</final>",
        ],
        max_steps=2,  # only 2 tool steps allowed — would fail if plan counts
        approval_policy="auto",
    )
    answer = agent.ask("do the task")
    assert answer == "Done."
```

**Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_plan_does_not_consume_tool_step_budget -v
```
Expected: FAIL if plan currently counts (or pass if `continue` already skips increment — verify behaviour)

**Step 3: Verify `tool_steps` is NOT incremented for plan responses**

In `ask()` the `tool_steps += 1` line is only inside `if kind == "tool":`. The `if kind == "plan":` block we added in Task 2 uses `continue` without incrementing — this is already correct. Run the test; if it passes, no code change needed.

**Step 4: Run full suite to confirm no regressions**

```bash
uv run pytest tests/test_mini_coding_agent.py -v
```
Expected: all existing + new tests pass

**Step 5: Commit**

```bash
git add tests/test_mini_coding_agent.py
git commit -m "test: verify plan responses do not consume tool-step budget"
```

---

## Task 4: `--plan` CLI flag + system prompt planning rule

**Files:**
- Modify: `mini_coding_agent.py:362–403` (`MiniAgent.__init__`)
- Modify: `mini_coding_agent.py:483–530` (`build_prefix()`)
- Modify: `mini_coding_agent.py:1094–1130` (`build_agent()`)
- Modify: `mini_coding_agent.py:1133–1150` (`build_arg_parser()`)
- Test: `tests/test_mini_coding_agent.py`

**Step 1: Write the failing tests**

```python
def test_plan_rule_in_prefix_when_plan_mode(tmp_path):
    """plan_mode=True injects planning instruction into the system prompt prefix."""
    agent = build_agent(tmp_path, [], plan_mode=True)
    assert "<plan>" in agent.prefix

def test_plan_rule_absent_from_prefix_by_default(tmp_path):
    """plan_mode=False (default) keeps the prefix free of planning instructions."""
    agent = build_agent(tmp_path, [])
    assert "<plan>" not in agent.prefix
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_plan_rule_in_prefix_when_plan_mode -v
```
Expected: FAIL — `plan_mode` param doesn't exist yet

**Step 3: Add `plan_mode=False` to `MiniAgent.__init__`**

In the `__init__` signature (line 363), add `plan_mode=False` after `auto_verify=False`:

```python
    auto_verify=False,
    plan_mode=False,
```

And in the body (after `self.auto_verify = auto_verify`):

```python
        self.plan_mode = plan_mode
```

**Step 4: Inject planning rule into `build_prefix()` when `plan_mode=True`**

In `build_prefix()` (line 500), inside the rules string, append a conditional rule after the existing rules list:

```python
plan_rule = (
    "\n- For any task requiring multiple steps, first respond with a <plan> block listing"
    " the numbered steps before using any tools:\n"
    "  <plan>\n  1. step one\n  2. step two\n  </plan>"
) if self.plan_mode else ""
```

Then include `{plan_rule}` at the end of the rules section in the f-string (before `Tools:`).

**Step 5: Add `plan_mode` to `build_agent()` and `build_arg_parser()`**

In `build_arg_parser()` (line 1149), add after `--auto-verify`:
```python
parser.add_argument("--plan", action="store_true", default=False,
                    help="Before executing, emit a numbered plan and wait for approval.")
```

In `build_agent()` (lines 1110–1130), pass `plan_mode=args.plan` to both `MiniAgent.from_session()` and `MiniAgent()`.

**Step 6: Run tests**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_plan_rule_in_prefix_when_plan_mode tests/test_mini_coding_agent.py::test_plan_rule_absent_from_prefix_by_default -v
```
Expected: 2 passed

**Step 7: Run full suite**

```bash
uv run pytest tests/test_mini_coding_agent.py -v
```
Expected: all tests pass

**Step 8: Commit**

```bash
git add mini_coding_agent.py tests/test_mini_coding_agent.py
git commit -m "feat: --plan flag injects planning rule and wires plan_mode through CLI"
```

---

## Task 5: End-to-end plan flow test + full suite

**Files:**
- Test: `tests/test_mini_coding_agent.py`

**Step 1: Write the end-to-end test**

```python
def test_end_to_end_plan_then_tool_then_final(tmp_path):
    """Full flow: plan emitted, approved, tool runs, final answer returned."""
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            "<plan>\n1. Read hello.txt\n2. Return contents\n</plan>",
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>File contains: alpha</final>",
        ],
        plan_mode=True,
        approval_policy="auto",
    )
    answer = agent.ask("show me hello.txt")
    assert answer == "File contains: alpha"
    history_kinds = [(i["role"], i.get("name")) for i in agent.session["history"]]
    assert ("assistant", None) in history_kinds   # plan approval message recorded
    assert ("tool", "read_file") in history_kinds

def test_plan_approval_recorded_in_history(tmp_path):
    """Approved plan is stored as assistant message so model sees context on next step."""
    agent = build_agent(
        tmp_path,
        [
            "<plan>\n1. Do it\n</plan>",
            "<final>Done.</final>",
        ],
        plan_mode=True,
        approval_policy="auto",
    )
    agent.ask("do something")
    assistant_msgs = [i["content"] for i in agent.session["history"] if i["role"] == "assistant"]
    assert any("Plan approved" in m for m in assistant_msgs)
```

**Step 2: Run**

```bash
uv run pytest tests/test_mini_coding_agent.py::test_end_to_end_plan_then_tool_then_final tests/test_mini_coding_agent.py::test_plan_approval_recorded_in_history -v
```
Expected: 2 passed

**Step 3: Run full suite**

```bash
uv run pytest tests/test_mini_coding_agent.py -v
```
Expected: all tests pass (at minimum 42 — 36 prior + 6 new)

**Step 4: Commit**

```bash
git add mini_coding_agent.py tests/test_mini_coding_agent.py
git commit -m "feat: complete structured planning phase with end-to-end tests"
```

---

## Task 6: Update docs and push

**Files:**
- Modify: `README.md`
- Modify: `docs/cli-reference.md`
- Modify: `docs/how-it-works.md`
- Modify: `docs/design-decisions.md`
- Modify: `HELP_TEXT` and `HELP_DETAILS` constants in `mini_coding_agent.py` (line 18, 27)

**Step 1: Update `HELP_DETAILS` in `mini_coding_agent.py`**

Add to the commands block (line 27):
```
/plan      Show the pending plan (if any) for the current turn.
```

**Step 2: Update `docs/cli-reference.md`**

Add `--plan` to the flags table. Add a deep-dive section explaining the two-phase flow, the `<plan>` format, and how it interacts with `--approval`.

**Step 3: Update `README.md`**

Add a "Structured Planning" section after "Auto-Verify Tests" following the same style as the existing feature sections.

**Step 4: Update `docs/design-decisions.md`**

Add "Why Structured Planning?" section at end.

**Step 5: Update `docs/how-it-works.md`**

Add one sentence to the Component 3 walkthrough noting `plan_mode` and `<plan>` as a pre-execution phase.

**Step 6: Run full suite one final time**

```bash
uv run pytest tests/test_mini_coding_agent.py -v
```
Expected: all tests pass

**Step 7: Commit and push**

```bash
git add mini_coding_agent.py tests/test_mini_coding_agent.py README.md docs/cli-reference.md docs/how-it-works.md docs/design-decisions.md
git commit -m "docs: structured planning phase feature documentation"
git push origin main
```
