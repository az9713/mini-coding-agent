# Contributing

This guide covers everything you need to extend or modify **mini-coding-agent**: setting up a development environment, understanding the test infrastructure, adding new tools, swapping model backends, and avoiding common pitfalls.

---

## Development Setup

The project has no mandatory install step. `uv` resolves dependencies on demand.

```bash
git clone https://github.com/rasbt/mini-coding-agent.git
cd mini-coding-agent

# Run the full test suite (no prior install needed)
uv run pytest

# Run the linter
uv run ruff check mini_coding_agent.py

# Auto-fix lint issues
uv run ruff check --fix mini_coding_agent.py

# Run a single named test with verbose output
uv run pytest tests/test_mini_coding_agent.py::test_agent_runs_tool_then_final -v
```

Python 3.10 or later is required (declared in `pyproject.toml` as `requires-python = ">=3.10"`). No Ollama server is needed to run tests — the test suite replaces the model with a deterministic fake.

---

## Project Structure

```
mini-coding-agent/
├── mini_coding_agent.py          # Entire agent implementation (~1017 lines)
├── pyproject.toml                # Build config, dependencies, entry point
├── tests/
│   └── test_mini_coding_agent.py # All tests
└── docs/                         # This documentation
    ├── cli-reference.md
    └── contributing.md
```

The entire agent lives in a single file, `mini_coding_agent.py`. The entry point `mini-coding-agent` (declared in `[project.scripts]`) calls the `main()` function in that module. There are no subpackages.

---

## How Tests Work

### The FakeModelClient

The test suite never contacts Ollama. Instead, every test constructs a **`FakeModelClient`** — a drop-in replacement for `OllamaModelClient` that returns preset strings from a queue:

```python
class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)   # queue of preset response strings
        self.prompts = []              # records every prompt sent to the client

    def complete(self, prompt, max_new_tokens):
        self.prompts.append(prompt)    # store for later assertions
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)     # consume next preset response
```

Each call to `.complete()` removes and returns the first item in `outputs`. Tests are therefore **fully deterministic** — no network calls, no randomness, no dependency on a running LLM.

### The build_agent Helper

Every test creates an agent through the `build_agent` helper:

```python
def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )
```

Key points:

- `tmp_path` is a pytest built-in fixture that supplies a fresh temporary directory for each test. Files written during the test are automatically cleaned up afterwards.
- `approval_policy` defaults to `"auto"` so tests never pause waiting for keyboard input.
- Extra keyword arguments (e.g. `max_steps=3`) are forwarded to `MiniAgent`.

### How Outputs Map to Agent Steps

The agent loop alternates between calling the model and executing tool calls. The `outputs` list mirrors this sequence directly:

```
FakeModelClient outputs = [
    '<tool>{"name":"read_file","args":{"path":"hello.txt"}}</tool>',  # step 1: model returns a tool call
    "<final>Read the file successfully.</final>",                      # step 2: model returns the final answer
]

# agent.ask() flow:
#   1. calls model.complete() -> gets outputs[0] -> parses tool call -> executes read_file
#   2. calls model.complete() -> gets outputs[1] -> parses final answer -> returns text
```

If the agent makes more model calls than there are items in `outputs`, `FakeModelClient` raises `RuntimeError("fake model ran out of outputs")`, which immediately fails the test. This behaviour is intentional — it catches unbounded loops early.

---

## Writing a New Test

### Step-by-Step Worked Example

The following test verifies that the agent can list files in the workspace:

```python
def test_agent_lists_files(tmp_path):
    # 1. Populate the workspace with known files.
    (tmp_path / "main.py").write_text("print('hello')\n")
    (tmp_path / "utils.py").write_text("def helper(): pass\n")

    # 2. Build the agent with a scripted response sequence.
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>Found main.py and utils.py.</final>",
        ],
    )

    # 3. Run the agent.
    answer = agent.ask("What files are in the workspace?")

    # 4. Assert the returned answer.
    assert answer == "Found main.py and utils.py."

    # 5. Assert that the expected tool was actually called.
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "list_files"
```

**Why each assertion matters:**

- `assert answer == "Found main.py and utils.py."` confirms that the agent extracted the `<final>` block correctly and returned it verbatim to the caller. This is the primary user-visible output.
- `assert tool_events[0]["name"] == "list_files"` confirms that a tool call actually happened in the session history, not just that the model happened to produce text mentioning the tool. Checking `session["history"]` gives you ground truth about what the agent loop executed.

### General Guidelines

- Create all files the agent will read before calling `agent.ask()`. The workspace is a real directory on disk.
- One `outputs` entry per model call. If you expect two tool calls before a final answer, provide three entries: two tool calls and one final.
- Use `agent.model_client.prompts` to inspect the exact prompt strings sent to the model if you need to assert on prompt content.
- Test failure paths by making the tool output an error format string and asserting that the session history contains an `"error"` role event.

---

## Adding a New Tool

Adding a tool requires changes in four places in `mini_coding_agent.py`, plus a test. The example below implements a `count_lines` tool that counts the lines in a file.

### Step 1 — Write the Implementation Method

Add a `tool_<name>` method to `MiniAgent`:

```python
def tool_count_lines(self, args):
    path = self.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return f"{path.relative_to(self.root)}: {count} lines"
```

`self.path()` resolves the caller-supplied string against `self.root` and enforces the path-safety check. Raise `ValueError` for user-correctable errors (wrong path, wrong type); the agent feeds the error message back to the model as context.

### Step 2 — Register in `build_tools()`

Add an entry to the dict returned by `build_tools()`:

```python
"count_lines": {
    "schema":      {"path": "str"},
    "risky":       False,
    "description": "Count the number of lines in a file.",
    "run":         self.tool_count_lines,
},
```

| Key | Purpose |
|-----|---------|
| `schema` | Maps argument names to type strings. Used to validate that the model supplied all required arguments. |
| `risky` | `True` triggers the approval policy check. `False` executes unconditionally. |
| `description` | Included in the system prompt so the model knows when to use this tool. |
| `run` | The callable to invoke when the tool is triggered. |

### Step 3 — Add Validation in `validate_tool()`

Pre-flight checks run before `run` is called. They should mirror the checks inside the method itself so that errors surface early with clear messages:

```python
if name == "count_lines":
    path = self.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    return
```

### Step 4 — Add an Example in `tool_example()`

`tool_example()` returns a dict of canonical tool-call strings used in error messages when the model produces a malformed call. Add one entry for your tool:

```python
"count_lines": '<tool>{"name":"count_lines","args":{"path":"main.py"}}</tool>',
```

### Step 5 — Write a Test

```python
def test_count_lines_tool(tmp_path):
    (tmp_path / "sample.py").write_text("line1\nline2\nline3\n")
    agent = build_agent(tmp_path, [])
    result = agent.run_tool("count_lines", {"path": "sample.py"})
    assert result == "sample.py: 3 lines"
```

This test calls `run_tool()` directly rather than going through `agent.ask()`, which avoids the need to script model responses when you only want to verify tool behaviour in isolation. `FakeModelClient` receives an empty `outputs` list because no model calls happen.

---

## Swapping the Model Client

**`MiniAgent`** accepts any object as `model_client` provided it implements one method:

```
complete(prompt: str, max_new_tokens: int) -> str
```

That is the entire interface contract. The return value must be a plain string containing the model's response. No streaming, no token counts, no metadata — just text.

To use a different backend, implement the interface and pass an instance to `MiniAgent`:

```python
class AnthropicModelClient:
    def __init__(self, api_key, model="claude-haiku-4-5-20251001"):
        self.api_key = api_key
        self.model = model

    def complete(self, prompt, max_new_tokens):
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key)
        message = client.messages.create(
            model=self.model,
            max_tokens=max_new_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
```

Wire it in when constructing the agent:

```python
agent = MiniAgent(
    model_client=AnthropicModelClient(api_key="sk-..."),
    workspace=workspace,
    session_store=store,
    approval_policy="ask",
)
```

The same pattern applies to OpenAI, llama.cpp's HTTP server, or any other inference backend. The `FakeModelClient` used in tests is itself just another implementation of this same one-method interface.

> **Note:** The system prompt passed to `complete()` instructs the model to emit `<tool>…</tool>` and `<final>…</final>` XML tags. Models with strong instruction-following (large parameter counts or fine-tuned chat variants) are more likely to honour this format consistently.

---

## Making a Risky Tool

Set `"risky": True` in the `build_tools()` entry for any tool that modifies state (writes files, runs commands, deletes content, makes network requests). The approval policy then applies:

```
Approval policy         Behaviour for risky tools
----------------------  ---------------------------------------------------------------
ask (default)           Prints "approve <tool> <args>? [y/N]" and waits for user input.
                        Type "y" or "yes" to allow. Any other input denies the call and
                        returns an error string to the model.

auto                    Executes immediately without prompting.
                        Use in trusted workspaces or non-interactive scripts.

never                   Always returns "error: approval denied" without executing.
                        Use for read-only auditing sessions.
```

Additionally, if the agent is running as a **child delegate** with `read_only=True`, risky tools are blocked regardless of the approval policy. This prevents delegated sub-agents from making unreviewed modifications.

When writing tests for risky tools, always use `approval_policy="auto"` (the `build_agent` default) so the test does not block waiting for stdin.

---

## Linting

The project uses **ruff** with default settings (no custom configuration in `pyproject.toml`):

```bash
# Check for issues
uv run ruff check mini_coding_agent.py

# Apply auto-fixable corrections
uv run ruff check --fix mini_coding_agent.py
```

All contributions should pass `ruff check` with zero errors before opening a pull request. Ruff's default rule set covers standard PEP 8 style, unused imports, and several categories of potential bugs.

---

## Common Pitfalls

### `patch_file` Requires an Exact Unique Match

The `patch_file` tool replaces `old_text` with `new_text`. The `old_text` value must appear **exactly once** in the target file — character-perfect, including whitespace and line endings. Tests that exercise `patch_file` should create the file with a known, explicit string and use that exact string as `old_text`. If `old_text` appears zero times or more than once, the tool raises an error.

### FakeModelClient Raises on Empty Queue

If the agent makes more model calls than there are entries in `outputs`, `FakeModelClient` raises `RuntimeError("fake model ran out of outputs")`. This is intentional: it surfaces unbounded loops immediately rather than hanging. Count the expected number of model calls (one per tool call plus one for the final answer) and supply exactly that many entries.

### Paths Must Stay Inside `self.root`

`self.path()` enforces that the resolved path does not escape the workspace root. Any path argument that would resolve outside the root raises a `ValueError`. Tests using `tmp_path` are automatically sandboxed because the workspace is created inside `tmp_path`. Do not use absolute paths in tool argument strings in tests.

### The `delegate` Tool Requires Depth Headroom

The `delegate` tool (which spawns a child agent to handle a sub-task) is only available when the current agent's `depth` is less than `max_depth`. In a default top-level agent, `depth` is `0` and `max_depth` is `1`, so delegation is available. To write tests for `delegate`, construct the agent with default depth settings and do not set `depth` manually to `max_depth` or above.

### Approval Prompts Block Tests

Any test that exercises a risky tool without setting `approval_policy="auto"` will block indefinitely waiting for stdin. The `build_agent` helper defaults to `"auto"` for this reason. Only override to `"ask"` or `"never"` when you are specifically testing the approval gate itself.
