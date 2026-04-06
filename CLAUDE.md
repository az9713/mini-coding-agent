# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Mini-Coding-Agent** is a minimal, standalone local coding agent that runs against [Ollama](https://ollama.com/) local LLM models. It provides an interactive REPL for coding tasks using structured tools (read/write files, search, run shell commands) with approval gating for risky operations.

Requirements: Python 3.10+, Ollama running locally, optional `uv` for CLI entry point.

## Commands

```bash
# Run the agent
uv run mini-coding-agent                        # Interactive REPL (default model: qwen3.5:4b)
uv run mini-coding-agent --approval auto        # Auto-approve risky operations
uv run mini-coding-agent --resume latest        # Resume last session
python mini_coding_agent.py                     # Direct execution without uv

# Run tests
pytest
pytest tests/test_mini_coding_agent.py

# Lint
ruff check mini_coding_agent.py
```

Key CLI flags: `--cwd`, `--model`, `--host`, `--approval` (ask/auto/never), `--max-steps`, `--max-new-tokens`, `--temperature`, `--resume`.

## Architecture

The entire agent lives in a single file: `mini_coding_agent.py` (~1345 lines). This is intentional — the project prioritizes readability over modularity.

The code is organized around **6 explicitly documented components**:

### Component 1: Live Repo Context (`WorkspaceContext`)
Snapshots git branch/status/commits and reads project docs (AGENTS.md, README.md, pyproject.toml) to provide workspace context injected into each prompt.

### Component 2: Prompt Shape & Cache Reuse
- `build_prefix()` — static system prompt with tool definitions (built once, reused across turns)
- `memory_text()` — formats rolling working memory (task, files touched, notes)
- `prompt()` — assembles final prompt: prefix + memory + history + current request

### Component 3: Structured Tools & Permissions
Seven tools defined in `build_tools()`, executed via `run_tool()`:

| Tool | Risk | Description |
|------|------|-------------|
| `list_files(path)` | Safe | List directory contents |
| `read_file(path, start, end)` | Safe | Read file line range |
| `search(pattern, path)` | Safe | Search via ripgrep or stdlib fallback |
| `run_shell(command, timeout)` | **Risky** | Execute shell command |
| `write_file(path, content)` | **Risky** | Create/overwrite file |
| `patch_file(path, old_text, new_text)` | **Risky** | Exact-match text replacement |
| `delegate(task, max_steps)` | Safe | Bounded read-only child agent |

`approve()` gates risky tools based on `--approval` policy. `parse()` accepts both XML and JSON tool formats for robustness.

### Component 4: Context Reduction
- `clip()` — truncates long tool outputs with character count
- `history_text()` — compresses history by deduplicating old reads and harder-clipping old items; limits total to 12,000 chars

### Component 5: Session Management (`SessionStore`)
Sessions saved as JSON in `.mini-coding-agent/sessions/`. `record()` appends to history and persists. `remember()` maintains a rolling memory of recently touched files/notes.

### Component 6: Delegation (`tool_delegate`)
Child agents inherit the parent's workspace and model but run with a reduced step budget, `read_only=True`, and `approval_policy="never"`. They receive parent history context as notes.

### Agent Loop (`ask` method)
`user message → build prompt → call Ollama → parse response → (tool: validate/approve/execute) or (final: return) → loop`

### Testing
`FakeModelClient` provides deterministic testing by returning preset model outputs. Tests cover: tool execution, malformed output retries, XML parsing, session loading, delegation, and budget tracking.
