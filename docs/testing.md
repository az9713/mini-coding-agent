# Manual Feature Testing Guide

This guide walks through manually verifying the six major features of
mini-coding-agent against a real Ollama model. The automated test suite
(`pytest`) covers these features with a fake model client — this guide
confirms they work end-to-end with actual inference.

---

## Prerequisites

### 1. Install Ollama

Download and install from [ollama.com/download](https://ollama.com/download).

Start the server in a terminal and **leave it running**:

```bash
ollama serve
```

Pull the default model:

```bash
ollama pull qwen3.5:4b
```

Verify it responds:

```bash
ollama run qwen3.5:4b "say hello"
```

### 2. Install mini-coding-agent globally

```bash
uv tool install --editable ~/Downloads/agent_harness_raschka
```

Verify:

```bash
which mini-coding-agent
mini-coding-agent --help
```

### 3. Create a fresh test workspace

```bash
rm -rf ~/Downloads/test-agent
mkdir ~/Downloads/test-agent
cd ~/Downloads/test-agent
git init
```

All commands below assume your working directory is `~/Downloads/test-agent`.

---

## Feature 1 — Clean output (buffered streaming)

The agent buffers each model response and only prints validated output —
no raw XML tags, no repeated failed attempts, no trailing tool calls after
a plan block.

```bash
mini-coding-agent
```
```
mini-coding-agent> what files are in this directory?
mini-coding-agent> /exit
```

**Pass:** the answer appears without `<tool>`, `<final>`, or any raw XML.
Only `[tool_name]` brackets and clean answer text are printed.  
**Fail:** raw `<tool>...</tool>` or `<final>...</final>` tags appear in the output.

---

## Feature 2 — Session Resume (`--resume`)

Sessions are saved automatically and can be reloaded in a new process.

```bash
mini-coding-agent --approval auto
```
```
mini-coding-agent> create a file hello.py that prints Hello World
mini-coding-agent> /exit
```

```bash
mini-coding-agent --resume latest
```
```
mini-coding-agent> what did you do in the previous session?
mini-coding-agent> /exit
```

**Pass:** the model describes creating `hello.py` without being told.  
**Fail:** the model says it has no memory of previous actions.

---

## Feature 3 — File Checkpointing (`/diff` and `/rewind`)

The agent snapshots files before editing them, enabling inspection and undo.

```bash
mini-coding-agent --approval auto
```
```
mini-coding-agent> create a file notes.txt containing the line "version one"
mini-coding-agent> /diff
```

**Pass:** output shows a unified diff with `+version one`.

```
mini-coding-agent> patch notes.txt to say "version two" instead
mini-coding-agent> /rewind
mini-coding-agent> /exit
```

```bash
cat notes.txt
```

**Pass:** file contains `version one` — the edit was undone.

---

## Feature 4 — Auto-Verify (`--auto-verify`)

After every file write or patch, the agent automatically runs the project's
test suite and appends the result to the tool output.

Set up a minimal project with a passing test:

```bash
cat > pyproject.toml << 'EOF'
[build-system]
requires = ["setuptools"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
EOF

mkdir -p tests

cat > hello.py << 'EOF'
def greet(name):
    return f"Hello, {name}!"
EOF

cat > tests/test_hello.py << 'EOF'
from hello import greet

def test_greet():
    assert greet("world") == "Hello, world!"
EOF
```

Confirm the tests pass before involving the agent:

```bash
uv run pytest -q
```

Now run the agent with `--auto-verify`:

```bash
mini-coding-agent --approval auto --auto-verify
```
```
mini-coding-agent> add a shout() function to hello.py that returns the greeting in uppercase
mini-coding-agent> /exit
```

**Pass:** after the file write, output includes:

```
auto-verify:
tests passed (exit 0):
...
```

**Fail:** no `auto-verify:` section appears after the write.

---

## Feature 5 — Structured Planning (`--plan`)

The model emits a numbered plan before acting, and the user confirms before
any tools are called.

### 5a — Cancel

```bash
mini-coding-agent --approval ask --plan
```
```
mini-coding-agent> add a farewell() function to hello.py and write a test for it
```

**Pass:** model prints a numbered plan and prompts `execute plan? [Y/n]`.

Type `n`.

**Pass:** agent cancels without touching any files.

```
mini-coding-agent> /exit
```

### 5b — Execute

```bash
mini-coding-agent --approval auto --plan
```
```
mini-coding-agent> add a farewell() function to hello.py and write a test for it
```

**Pass:** plan is printed, then the agent proceeds through the steps with tool calls.

```
mini-coding-agent> /exit
```

---

## Understanding repeated lines in the output

You may see the same tool call printed two or three times before a `[tool_name]`
bracket appears. This is normal. Because tokens stream to the terminal as the
model generates them, every attempt is printed before the agent validates it.
A failed attempt is rejected silently, a retry notice is sent to the model,
and the model tries again — each attempt appearing as another line.

Only calls followed by a `[tool_name]` bracket actually executed. Everything
else was a failed attempt absorbed by the retry budget.

This happens most often with `patch_file` on a 4b model because the XML
escaping is error-prone. If you see many retries, try a larger model with
`--model qwen3.5:9b`. For a full explanation with a worked example, see
`agent-loop.md` — "What retries look like in the terminal".

---

## Summary

| Feature | Command | Pass condition |
|---|---|---|
| Clean output | `mini-coding-agent` | No raw XML tags; only brackets and answer text printed |
| Resume | `mini-coding-agent --resume latest` | Prior session history visible to model |
| `/diff` | `/diff` inside REPL | Unified diff shows agent's edits |
| `/rewind` | `/rewind` inside REPL | File restored to pre-edit state |
| Auto-verify | `--auto-verify` | `auto-verify: tests passed` printed after writes |
| Planning | `--plan` | Numbered plan printed; `[Y/n]` prompt appears |
