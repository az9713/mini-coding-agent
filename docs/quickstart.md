# Quickstart

This guide takes you from a blank machine to a running coding agent in eight steps. It assumes no prior experience with Python environments, Ollama, or uv. Follow each step in order and check the expected output before continuing.

---

## Step 1 — Install Python 3.10 or later

mini-coding-agent requires Python 3.10 or later. If you already have it, skip to the verify step.

**Windows:** Open the Microsoft Store, search for "Python 3.12", and install it. Alternatively, download the installer from [python.org/downloads](https://www.python.org/downloads/) and run it — check "Add python.exe to PATH" before clicking Install.

**macOS:** Install [Homebrew](https://brew.sh) if you have not already, then run:

```bash
brew install python@3.12
```

**Linux (Debian/Ubuntu):**

```bash
sudo apt update && sudo apt install python3.12 python3.12-venv
```

**Linux (Fedora/RHEL):**

```bash
sudo dnf install python3.12
```

Verify the installation:

```bash
python --version
```

Expected output (version number will vary, but must be 3.10 or higher):

```
Python 3.12.3
```

> **Note:** On some systems the command is `python3` instead of `python`. Either works throughout this guide as long as the version is 3.10+.

---

## Step 2 — Install uv

**uv** is a fast Python package manager and script runner. It handles dependency installation and provides the `uv run` command that launches mini-coding-agent without you needing to activate a virtualenv manually.

**macOS and Linux:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

After installation, open a new terminal (or restart your existing one) so the `uv` binary is on your PATH, then verify:

```bash
uv --version
```

Expected output:

```
uv 0.5.21 (or later)
```

---

## Step 3 — Install Ollama

**Ollama** is a local LLM server. It downloads model weights to your machine and exposes a simple HTTP API that mini-coding-agent calls for every inference request. Nothing is sent to the cloud.

Go to [ollama.com/download](https://ollama.com/download) and install the package for your operating system. The installer adds the `ollama` CLI and a background service.

After installation, start the Ollama server in a terminal:

```bash
ollama serve
```

Expected output:

```
time=2026-04-04T14:40:00.000Z level=INFO source=server.go msg="Listening on 127.0.0.1:11434"
```

> **Note:** Keep this terminal open for the entire session. The agent connects to `http://127.0.0.1:11434` by default. If you close it, the agent will fail to reach the model.

---

## Step 4 — Pull the default model

mini-coding-agent defaults to `qwen3.5:4b`. Pull it with:

```bash
ollama pull qwen3.5:4b
```

This downloads approximately 2.5 GB of model weights to your local machine (exact size depends on quantization). It only happens once — Ollama caches the weights and reuses them on every subsequent run.

Expected output (progress will vary):

```
pulling manifest
pulling 3b5f9ce6b1b3... 100% |###########| 2.48 GB
pulling 5ef82f63e5a4...  100% |###########|  525 B
verifying sha256 digest
writing manifest
success
```

**Hardware notes:** The 4b model fits comfortably in 8 GB of RAM and runs acceptably on a CPU-only machine, though a GPU significantly improves response speed. If you have 16 GB of RAM and want higher quality responses, try `ollama pull qwen3.5:9b` and pass `--model qwen3.5:9b` when launching the agent.

---

## Step 5 — Get the code

Clone the repository and enter the project directory:

```bash
git clone https://github.com/rasbt/mini-coding-agent.git
cd mini-coding-agent
```

Verify you see the project files:

```bash
ls
```

Expected output (order may vary):

```
CLAUDE.md   LICENSE   README.md   docs   mini_coding_agent.py   pyproject.toml   tests
```

The entire agent lives in `mini_coding_agent.py`. Everything else is supporting material.

---

## Step 6 — Run the agent

Launch the agent with uv:

```bash
uv run mini-coding-agent
```

uv reads `pyproject.toml`, installs the package into an isolated environment on first run (this takes a few seconds), then starts the agent. On subsequent runs it launches immediately.

Expected output — the welcome screen:

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
|  WORKSPACE  /home/you/mini-coding-agent                            |
|  MODEL      qwen3.5:4b          BRANCH   main                     |
|  APPROVAL   ask                 SESSION  20260404-144025-2dd0aa    |
|                                                                    |
+====================================================================+

mini-coding-agent>
```

The welcome screen confirms four things at a glance: which directory the agent is watching (`WORKSPACE`), which model it will call (`MODEL`), the current git branch (`BRANCH`), and the unique ID assigned to this session (`SESSION`). See `index.md` for a full field-by-field explanation.

---

## Step 7 — Your first task

At the prompt, ask the agent to list the files in the current directory:

```
mini-coding-agent> what files are in this project?
```

The agent will reason about the request, call the `list_files` tool, and return a final answer. You will see something like:

```
mini-coding-agent> what files are in this project?

The project contains the following files and directories:

[D] docs
[D] tests
[F] CLAUDE.md
[F] LICENSE
[F] README.md
[F] mini_coding_agent.py
[F] pyproject.toml
```

What happened under the hood: the model received a prompt containing the system instructions, the current workspace context (git branch, recent commits, project docs), and your question. It responded with a structured `<tool>{"name":"list_files","args":{"path":"."}}</tool>` call. The agent executed `list_files`, captured the directory listing, injected the result back into the conversation, sent a second inference request, and the model returned a `<final>...</final>` answer. The agent printed that answer and returned control to you.

Notice that `list_files` required no approval — it is a **safe tool** (read-only, no side effects). Risky tools behave differently, as the next step shows.

---

## Step 8 — Approving a risky action

Ask the agent to create a small test file:

```
mini-coding-agent> create a file called hello.py that prints "Hello, world!"
```

Because `write_file` modifies the filesystem, it is classified as a **risky tool**. With the default `ask` approval policy, the agent pauses before executing and shows you the exact call it wants to make:

```
approve write_file {"path": "hello.py", "content": "print(\"Hello, world!\")\n"}? [y/N]
```

Type `y` and press Enter to allow it. The agent will write the file and confirm:

```
Done. Created hello.py with a single print statement.
```

If you type anything other than `y` or `yes`, the tool call is blocked and the agent receives an `approval denied` error, which it will report back to you.

**The three approval modes** control how the agent handles every risky tool call (`write_file`, `patch_file`, `run_shell`):

| Mode | Behavior | When to use |
|---|---|---|
| `ask` (default) | Pauses and prompts you before each risky action | Normal interactive use — you stay in control |
| `auto` | Executes risky tools immediately without prompting | Automated pipelines or when you trust the task fully |
| `never` | Blocks all risky tools — agent is effectively read-only | Exploration and code review where writes must not happen |

Switch modes with the `--approval` flag at launch:

```bash
uv run mini-coding-agent --approval auto
```

---

## Step 9 — Exiting and resuming

To exit the agent cleanly, type:

```
mini-coding-agent> /exit
```

The session is already saved — the agent writes a JSON file to `.mini-coding-agent/sessions/` in your workspace root after every exchange. To continue where you left off, use `--resume latest`:

```bash
uv run mini-coding-agent --resume latest
```

The agent reloads the full conversation history and working memory (the task description, recently touched files, and recent notes). You can also resume a specific session by ID:

```bash
uv run mini-coding-agent --resume 20260404-144025-2dd0aa
```

Session IDs are shown in the welcome screen and can also be found with:

```
mini-coding-agent> /session
```

Which prints the full path to the current session file, for example:

```
/home/you/mini-coding-agent/.mini-coding-agent/sessions/20260404-144025-2dd0aa.json
```

---

## What's Next?

You have a running agent. Here are the natural next reads depending on your goal:

| Goal | Doc |
|---|---|
| Understand how the agent thinks and acts | `how-it-works.md` |
| See every tool and what it can do | `tools.md` |
| Learn about the step-by-step agent loop | `agent-loop.md` |
| Understand how prompts are built | `prompt-engineering.md` |
| Learn about session files and memory | `session-memory.md` |
| See all CLI flags and REPL commands | `cli-reference.md` |
| Add a new tool or contribute code | `contributing.md` |
| Understand why the code is designed this way | `design-decisions.md` |
