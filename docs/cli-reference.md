# CLI Reference

## Synopsis

```
uv run mini-coding-agent [OPTIONS] [PROMPT...]
python mini_coding_agent.py [OPTIONS] [PROMPT...]
```

`PROMPT` is optional. When provided, the agent runs in **one-shot mode** — it executes the request, prints the answer, and exits. When omitted, the agent opens an interactive **REPL** where you can send multiple requests in a session.

---

## Positional Argument: PROMPT

| Argument | Arity | Description |
|----------|-------|-------------|
| PROMPT | zero or more words | Task or question for the agent. Multiple words are joined with a single space before being sent. |

When `PROMPT` is supplied, the agent sends it to the model, runs any tool calls required to fulfil the request, prints the final answer to stdout, and exits with code `0`. If the Ollama server is unreachable or returns an HTTP error, the process exits with code `1`.

**Example**

```bash
uv run mini-coding-agent "list all Python files in src/"
```

---

## Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--cwd` | path | `.` | Working directory for the agent's workspace. All tool paths are resolved relative to the repository root derived from this directory. |
| `--model` | string | `qwen3.5:4b` | Name of the Ollama model to use. Must match a model already pulled on the Ollama server. |
| `--host` | URL | `http://127.0.0.1:11434` | Base URL of the Ollama server. The agent appends `/api/generate` to this value. |
| `--ollama-timeout` | int (seconds) | `300` | Maximum time in seconds to wait for a single `/api/generate` response before raising a timeout error. |
| `--resume` | string | _(none)_ | Session ID to continue, or the literal string `latest` to resume the most recent session. |
| `--approval` | `ask` \| `auto` \| `never` | `ask` | Controls whether the agent prompts before executing risky tools such as `write_file` or `run_command`. |
| `--max-steps` | int | `6` | Maximum number of tool calls the agent may make per user request. |
| `--max-new-tokens` | int | `512` | Maximum tokens the model may produce in a single generation call. |
| `--temperature` | float | `0.2` | Sampling temperature passed to Ollama. Lower values produce more deterministic output. |
| `--top-p` | float | `0.9` | Nucleus sampling probability mass passed to Ollama. |

---

## Flag Deep-Dives

### `--cwd`

**Type:** path | **Default:** `.`

Sets the directory the agent treats as its workspace. The agent attempts to find the enclosing Git repository root by walking up from `--cwd`; if no `.git` directory is found, the path itself becomes the root. All tool operations — reading, writing, patching, listing files — resolve paths relative to this root. Symlinks are not followed outside the root.

Change this when you want to point the agent at a specific project directory without changing your shell's working directory. Useful in scripts:

```bash
uv run mini-coding-agent --cwd /projects/myapp "summarise recent changes"
```

### `--model`

**Type:** string | **Default:** `qwen3.5:4b`

Any model name recognised by your Ollama server (run `ollama list` to see available models). The model must already be pulled with `ollama pull <name>` before starting the agent. Larger models follow the structured tool-call format more reliably; `qwen3.5:9b` is a practical upgrade from the `4b` default without a large latency increase.

> **Note:** If the model does not produce output in the expected `<tool>…</tool>` or `<final>…</final>` format, the agent counts each malformed response against `--max-steps`. Models with stronger instruction-following (e.g. larger parameter counts or fine-tuned variants) reduce wasted steps.

### `--host`

**Type:** URL | **Default:** `http://127.0.0.1:11434`

Full base URL of the Ollama server. The agent calls `POST {host}/api/generate`. Change this when Ollama is running on a remote host, a different port, or behind a reverse proxy:

```bash
uv run mini-coding-agent --host http://gpu-box.local:11434 "..."
```

No trailing slash is needed; the agent constructs the endpoint path itself.

### `--ollama-timeout`

**Type:** int (seconds) | **Default:** `300`

The maximum number of seconds to wait for a single response from Ollama before raising a timeout error and exiting with code `1`. The default of 300 seconds is generous and accommodates slow hardware or large models. Lower this value (e.g. `60`) if you want faster feedback when the server is unresponsive. Note that this is a per-request timeout, not a total session timeout; a single agent step that generates many tokens can legitimately take several minutes on CPU-only hardware.

### `--resume`

**Type:** string | **Default:** _(none)_

Pass a session ID (shown in `/session` output) to reload a previous conversation, including its history and working memory. Pass the special value `latest` to automatically select the most recently created session:

```bash
uv run mini-coding-agent --resume latest "continue where we left off"
```

When this flag is omitted, the agent starts a fresh session.

### `--approval`

**Type:** choice (`ask` | `auto` | `never`) | **Default:** `ask`

Controls how the agent handles **risky tools** — tools that write to disk, execute commands, or otherwise modify state.

| Value | Behaviour |
|-------|-----------|
| `ask` | Before each risky tool call, prints `approve <tool> <args>? [y/N]` and waits for input. Type `y` or `yes` to allow; anything else denies and returns an error to the model. |
| `auto` | All risky tools execute immediately without prompting. Use in trusted workspaces or non-interactive scripts. |
| `never` | Risky tools always fail with `error: approval denied`. Use for read-only auditing — the agent can read and list files but cannot modify anything. |

Read-only tools (such as `read_file` and `list_files`) are never gated by the approval policy.

### `--max-steps`

**Type:** int | **Default:** `6`

The maximum number of tool calls the agent may execute for a single user request. Once this limit is reached without a `<final>` response, the agent returns an error message. Each successful or failed tool call consumes one step.

Internally, the agent also allocates `max(max_steps * 3, max_steps + 4)` total model-generation attempts to absorb format retries (where the model outputs malformed XML that does not parse as a valid tool call or final answer). Raising `--max-steps` therefore also raises the retry budget proportionally. Use a higher value for complex multi-file tasks; use a lower value in scripts where you want predictable runtime.

### `--max-new-tokens`

**Type:** int | **Default:** `512`

Maximum number of tokens the model may generate in a single call. This maps directly to `num_predict` in the Ollama payload (see [Ollama API Parameters](#ollama-api-parameters)). It controls per-step output length, not total session token usage.

512 tokens is sufficient for most tool calls and short final answers. Increase this (e.g. to `1024` or `2048`) if the model is truncating mid-JSON or producing incomplete `write_file` content. Very high values increase latency per step without benefit if the model naturally stops sooner.

### `--temperature`

**Type:** float | **Default:** `0.2`

Sampling temperature for the model. Lower values make the output more deterministic and focused; higher values introduce more variety. For coding tasks, `0.2` strikes a good balance between reliability and flexibility. Use `0.0` for maximum repeatability in automated pipelines. Avoid values above `0.8` — they tend to produce malformed tool-call syntax.

This flag interacts with `--top-p`: both are passed to the model simultaneously. When `temperature` is very low, the effect of `top-p` diminishes because the probability mass is already concentrated on a small number of tokens.

### `--top-p`

**Type:** float | **Default:** `0.9`

**Nucleus sampling** threshold. The model considers only the smallest set of tokens whose cumulative probability equals `top-p`. A value of `0.9` means the bottom 10% of the probability distribution is excluded from sampling. Pair with a low `--temperature` (e.g. `0.2`) to reinforce reliable format adherence. Values closer to `1.0` widen the candidate set; values closer to `0.0` narrow it aggressively.

---

## REPL Commands

When the agent is started without a positional `PROMPT`, it enters an interactive REPL. The following slash-commands are available at any prompt:

| Command | Effect |
|---------|--------|
| `/help` | Print a summary of all available REPL commands. |
| `/memory` | Print the agent's current working memory: active task description, tracked files, and notes accumulated during the session. |
| `/session` | Print the absolute path to the current session JSON file on disk. |
| `/rewind` | Revert all file changes made by the agent in the most recent turn. Restores modified files to their pre-edit state; deletes files that the agent created. |
| `/rewind N` | Revert file changes from turn number N specifically. Turn numbers start at 1. |
| `/diff` | Show a unified diff of all file changes the agent has made across all turns in this session. |
| `/diff N` | Show a unified diff of file changes from turn N only. |
| `/reset` | Clear conversation history and working memory while keeping the same session file and REPL process alive. Also clears all checkpoint data. Useful when you want to start a fresh task without restarting the process. |
| `/exit` | Exit the REPL cleanly (exit code `0`). |
| `/quit` | Alias for `/exit`. |

---

## One-Shot Mode

When one or more words are passed as positional arguments, the agent operates in **one-shot mode**: it processes the request, prints the final answer, and exits immediately without opening a REPL.

This mode is designed for scripting and automation. All `--flags` work identically in one-shot and REPL modes.

**Examples**

```bash
# Ask a question and exit
uv run mini-coding-agent "what does main.py do?"

# Run a task non-interactively with automatic approval
uv run mini-coding-agent --approval auto "write a hello.py that prints Hello World"

# Use a larger model for a complex task
uv run mini-coding-agent --model qwen3.5:9b --max-steps 12 "refactor utils.py to use dataclasses"

# Pipe output to another command
uv run mini-coding-agent "list all .py files" | grep test

# Target a specific project directory
uv run mini-coding-agent --cwd /projects/myapp --approval auto "add type hints to helpers.py"
```

> **Note:** Because one-shot mode exits after a single request, `--resume` is most useful here — it lets you continue building on previous session context without starting a REPL.

---

## Ollama API Parameters

Each model call sends a `POST` request to `{host}/api/generate` with the following JSON payload:

```json
{
  "model":  "<--model value>",
  "prompt": "<constructed prompt string>",
  "stream": false,
  "raw":    false,
  "think":  false,
  "options": {
    "num_predict": 512,
    "temperature": 0.2,
    "top_p":       0.9
  }
}
```

| Field | Value | Meaning |
|-------|-------|---------|
| `stream` | `false` | The entire response is returned in one HTTP response body, not as a sequence of server-sent events. |
| `raw` | `false` | Ollama applies the model's built-in chat template to the prompt before inference. |
| `think` | `false` | Disables chain-of-thought scratchpad output for models that support it (e.g. QwQ). The agent does not parse or use `<think>` blocks. |
| `num_predict` | `--max-new-tokens` | Maps the CLI flag directly to Ollama's token-limit parameter. |
| `temperature` | `--temperature` | Passed through unchanged. |
| `top_p` | `--top-p` | Passed through unchanged. |

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — REPL exited cleanly, or one-shot request completed without error. |
| `1` | Ollama connection error, HTTP error, or request timeout. |
