# mini-coding-agent

A minimal, single-file coding agent that runs entirely on your local machine using Ollama LLMs — no API keys, no cloud, no external Python dependencies.

---

## Why This Project Is Worth Studying

Most production AI agents are framework-wrapped black boxes. mini-coding-agent is the opposite: everything that makes a coding agent work — the prompt assembly, the tool dispatch, the approval gate, the session memory, the context compression — lives in one readable Python file (`mini_coding_agent.py`, ~1017 lines). No magic. No hidden layers.

That makes it an unusually good teaching artifact. When you read the code, you are reading a complete, working answer to the question: "What is the minimum viable implementation of a local coding agent?" Each of the six components (described in `how-it-works.md`) maps to a named section in the source, so you can follow the architecture directly into the code.

The zero-dependency constraint is also deliberate. Every import in the file is from the Python standard library. This means you can understand every line without chasing third-party packages, and the entire agent is portable anywhere Python 3.10+ runs.

---

## Documentation Map

| File | What it covers |
|---|---|
| `quickstart.md` | Zero-to-running setup guide — install Python, Ollama, pull the model, run the agent |
| `how-it-works.md` | The six named components and how they compose into a working agent |
| `agent-loop.md` | Deep-dive into the `ask()` loop: steps, retries, stopping conditions, and the parse-dispatch cycle |
| `tools.md` | All seven tools, their schemas, which are risky, validation rules, and how approval works |
| `prompt-engineering.md` | How the static prefix, live workspace context, memory, and transcript are assembled into a single prompt string |
| `session-memory.md` | Session IDs, JSON persistence, working memory structure, history compression, and the `/memory` view |
| `delegation.md` | How the `delegate` tool spawns a read-only child agent, depth limits, and why subagents are always `approval=never` |
| `cli-reference.md` | Every CLI flag and REPL command with types, defaults, and examples |
| `contributing.md` | Dev environment setup, how to add a new tool, testing with `FakeModelClient`, and the linter configuration |
| `design-decisions.md` | The reasoning behind single-file design, stdlib-only, XML tool format, the `patch_file` uniqueness constraint, and more |

---

## What You Will Need

| Requirement | Notes |
|---|---|
| Python 3.10+ | Required. The code uses `match`-compatible syntax and modern `pathlib`. |
| [Ollama](https://ollama.com/download) | The local LLM server. Runs the model on your own hardware. |
| `uv` (optional but recommended) | A fast Python package manager and script runner. Lets you run the agent with a single command without manually managing a virtualenv. |
| ~2.5 GB disk space | For the default `qwen3.5:4b` model weights. |
| 8 GB RAM (minimum) | Sufficient for the 4b model. The 9b model needs approximately 16 GB. |

> **Note:** `uv` is optional. If you prefer, you can install the package with `pip install -e .` and run `python mini_coding_agent.py` directly. The quickstart uses `uv` because it is the faster path for most people.

---

## 30-Second Orientation

When you launch the agent, the first thing you see is the welcome screen:

```
+====================================================================+
|                          /\     /\\                                |
|                         {  `---'  }                                |
|                         {  O   O  }                                |
|                        ~~>  V  <~~                                 |
|                         \  \|/  /                                  |
|                          `-----'__                                 |
|                         MINI CODING AGENT                          |
|--------------------------------------------------------------------|
|                                                                    |
|  WORKSPACE  /path/to/your/project                                  |
|  MODEL      qwen3.5:4b          BRANCH   main                     |
|  APPROVAL   ask                 SESSION  20260401-144025-2dd0aa    |
|                                                                    |
+====================================================================+
```

Each field tells you something concrete about the running session:

| Field | Meaning |
|---|---|
| `WORKSPACE` | The directory the agent treats as its root. All file paths are resolved relative to this location, and no tool is permitted to read or write outside it. |
| `MODEL` | The Ollama model that will handle every inference call. Swap this with `--model` to try a different model without changing any other configuration. |
| `BRANCH` | The current git branch detected in the workspace. The agent injects this into the system prompt as live repository context. |
| `APPROVAL` | The active approval policy. `ask` means the agent will pause and prompt you before executing any risky tool (write, patch, shell). `auto` executes everything without prompting. `never` blocks all risky tools. |
| `SESSION` | A unique session identifier, formatted as `YYYYMMDD-HHMMSS-<6 hex chars>`. This ID corresponds to a JSON file saved at `.mini-coding-agent/sessions/<id>.json` in your workspace root. Pass it to `--resume` to continue the session later. |

After the welcome screen, you land at the REPL prompt:

```
mini-coding-agent>
```

Type any natural-language task and press Enter. The agent will call tools, observe results, and return a final answer. Type `/help` at any time to see the available REPL commands.
