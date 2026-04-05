import json
import pytest
from unittest.mock import patch

from mini_coding_agent import (
    CheckpointStore,
    FakeModelClient,
    MiniAgent,
    OllamaModelClient,
    SessionStore,
    WorkspaceContext,
    build_welcome,
    detect_test_command,
)


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    agent_dir = tmp_path / ".mini-coding-agent"
    store = SessionStore(agent_dir / "sessions")
    checkpoint_store = CheckpointStore(agent_dir / "checkpoints")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        checkpoint_store=checkpoint_store,
        approval_policy=approval_policy,
        **kwargs,
    )


def test_agent_runs_tool_then_final(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":2}}</tool>',
            "<final>Read the file successfully.</final>",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Read the file successfully."
    assert any(item["role"] == "tool" and item["name"] == "read_file" for item in agent.session["history"])
    assert "hello.txt" in agent.session["memory"]["files"]


def test_agent_retries_after_empty_model_output(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "<final>Recovered after retry.</final>",
        ],
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after retry."
    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("empty response" in item for item in notices)


def test_agent_retries_after_malformed_tool_payload(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":"bad"}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Recovered after malformed tool output.</final>",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Recovered after malformed tool output."
    assert any(item["role"] == "tool" and item["name"] == "read_file" for item in agent.session["history"])
    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("valid <tool> call" in item for item in notices)


def test_agent_accepts_xml_write_file_tool(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.py"><content>print("hi")\n</content></tool>',
            "<final>Done.</final>",
        ],
    )

    answer = agent.ask("Create hello.py")

    assert answer == "Done."
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == 'print("hi")\n'


def test_retries_do_not_consume_the_whole_budget(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "",
            "<final>Recovered after several retries.</final>",
        ],
        max_steps=1,
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after several retries."


def test_agent_saves_and_resumes_session(tmp_path):
    agent = build_agent(tmp_path, ["<final>First pass.</final>"])
    assert agent.ask("Start a session") == "First pass."

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.session["history"][0]["content"] == "Start a session"
    assert resumed.ask("Continue") == "Resumed."


def test_delegate_uses_child_agent(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"task":"inspect README","max_steps":2}}</tool>',
            "<final>Child result.</final>",
            "<final>Parent incorporated the child result.</final>",
        ],
    )

    answer = agent.ask("Use delegation")

    assert answer == "Parent incorporated the child result."
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate"
    assert "delegate_result" in tool_events[0]["content"]


def test_patch_file_replaces_exact_match(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello world\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "patch_file",
        {
            "path": "sample.txt",
            "old_text": "world",
            "new_text": "agent",
        },
    )

    assert result == "patched sample.txt"
    assert file_path.read_text(encoding="utf-8") == "hello agent\n"


def test_invalid_risky_tool_does_not_prompt_for_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")

    with patch("builtins.input") as mock_input:
        result = agent.run_tool("write_file", {})

    assert result.startswith("error: invalid arguments for write_file: 'path'")
    assert 'example: <tool name="write_file"' in result
    mock_input.assert_not_called()


def test_list_files_hides_internal_agent_state(tmp_path):
    agent = build_agent(tmp_path, [])
    (tmp_path / ".mini-coding-agent").mkdir(exist_ok=True)
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")

    result = agent.run_tool("list_files", {})

    assert ".mini-coding-agent" not in result
    assert ".git" not in result
    assert "[F] hello.txt" in result


def test_repeated_identical_tool_call_is_rejected(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record({"role": "tool", "name": "list_files", "args": {}, "content": "(empty)", "created_at": "1"})
    agent.record({"role": "tool", "name": "list_files", "args": {}, "content": "(empty)", "created_at": "2"})

    result = agent.run_tool("list_files", {})

    assert result == "error: repeated identical tool call for list_files; choose a different tool or return a final answer"


def test_welcome_screen_keeps_box_shape_for_long_paths(tmp_path):
    deep = tmp_path / "very" / "long" / "path" / "for" / "the" / "mini" / "agent" / "welcome" / "screen"
    deep.mkdir(parents=True)
    agent = build_agent(deep, [])

    welcome = build_welcome(agent, model="qwen3.5:4b", host="http://127.0.0.1:11434")
    lines = welcome.splitlines()

    assert len(lines) >= 5
    assert len({len(line) for line in lines}) == 1
    assert "..." in welcome
    assert "O   O" in welcome
    assert "MINI-CODING-AGENT" not in welcome
    assert "MINI CODING AGENT" in welcome
    assert "// READY" not in welcome
    assert "SLASH" not in welcome
    assert "READY      " not in welcome
    assert "commands: Commands:" not in welcome


def test_ollama_client_posts_expected_payload():
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"response": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OllamaModelClient(
        model="qwen3.5:4b",
        host="http://127.0.0.1:11434",
        temperature=0.2,
        top_p=0.9,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["timeout"] == 30
    assert captured["body"]["model"] == "qwen3.5:4b"
    assert captured["body"]["prompt"] == "hello"
    assert captured["body"]["stream"] is False
    assert captured["body"]["raw"] is False
    assert captured["body"]["think"] is False
    assert captured["body"]["options"]["num_predict"] == 42


# ---------------------------------------------------------------------------
# Checkpoint tests
# ---------------------------------------------------------------------------

def test_checkpoint_and_rewind_write_file(tmp_path):
    """write_file snapshots original; rewind restores it."""
    (tmp_path / "hello.txt").write_text("original\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.txt"><content>modified\n</content></tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Overwrite hello.txt")
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "modified\n"

    results = agent.checkpoint_store.rewind(1)
    assert results is not None
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "original\n"
    assert any(action == "restored" for _, action in results)


def test_rewind_deletes_new_file(tmp_path):
    """Rewinding a write_file that created a new file deletes it."""
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="brand_new.py"><content>print("hi")\n</content></tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Create brand_new.py")
    assert (tmp_path / "brand_new.py").exists()

    results = agent.checkpoint_store.rewind(1)
    assert results is not None
    assert not (tmp_path / "brand_new.py").exists()
    assert any(action == "deleted" for _, action in results)


def test_checkpoint_and_rewind_patch_file(tmp_path):
    """patch_file snapshots original; rewind restores it."""
    (tmp_path / "sample.txt").write_text("hello world\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"patch_file","args":{"path":"sample.txt","old_text":"world","new_text":"agent"}}</tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Patch sample.txt")
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == "hello agent\n"

    results = agent.checkpoint_store.rewind(1)
    assert results is not None
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == "hello world\n"


def test_no_duplicate_snapshots(tmp_path):
    """Same file written twice in one turn: checkpoint holds the very first state."""
    (tmp_path / "hello.txt").write_text("v1\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.txt"><content>v2\n</content></tool>',
            '<tool name="write_file" path="hello.txt"><content>v3\n</content></tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Write hello.txt twice")
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "v3\n"

    # Checkpoint must hold the original v1, not the intermediate v2
    path_key = str((tmp_path / "hello.txt").resolve())
    assert agent.checkpoint_store._data[1][path_key] == "v1\n"

    # Rewind should restore to v1
    agent.checkpoint_store.rewind(1)
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "v1\n"


def test_double_rewind_is_noop(tmp_path):
    """Rewinding the same turn twice: second call returns None."""
    (tmp_path / "hello.txt").write_text("original\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.txt"><content>changed\n</content></tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Edit hello.txt")
    agent.checkpoint_store.rewind(1)
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "original\n"

    result = agent.checkpoint_store.rewind(1)
    assert result is None  # turn data was deleted after first rewind


def test_diff_shows_changes(tmp_path):
    """diff() returns a unified diff containing the added line."""
    (tmp_path / "hello.txt").write_text("line1\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.txt"><content>line1\nline2\n</content></tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Add a line")
    diff = agent.checkpoint_store.diff(1, str(tmp_path))
    assert diff is not None
    assert "+line2" in diff
    assert "hello.txt" in diff


def test_checkpoints_survive_resume(tmp_path):
    """Checkpoint data persists on disk and is loadable by a resumed agent."""
    (tmp_path / "hello.txt").write_text("original\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.txt"><content>changed\n</content></tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Edit hello.txt")
    session_id = agent.session["id"]

    # Simulate resuming in a fresh agent instance
    workspace = build_workspace(tmp_path)
    agent_dir = tmp_path / ".mini-coding-agent"
    store = SessionStore(agent_dir / "sessions")
    checkpoint_store = CheckpointStore(agent_dir / "checkpoints")
    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>ok</final>"]),
        workspace=workspace,
        session_store=store,
        checkpoint_store=checkpoint_store,
        session_id=session_id,
        approval_policy="auto",
    )

    results = resumed.checkpoint_store.rewind(1)
    assert results is not None
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "original\n"


# ---------------------------------------------------------------------------
# Streaming tests
# ---------------------------------------------------------------------------

def test_fake_model_client_calls_on_token(tmp_path):
    """FakeModelClient invokes on_token with the full output string."""
    received = []
    client = FakeModelClient(["<final>hello</final>"])
    result = client.complete("prompt", 512, on_token=received.append)
    assert result == "<final>hello</final>"
    assert received == ["<final>hello</final>"]


def test_fake_model_client_skips_on_token_when_none(tmp_path):
    """FakeModelClient works normally when on_token is None."""
    client = FakeModelClient(["<final>hello</final>"])
    result = client.complete("prompt", 512, on_token=None)
    assert result == "<final>hello</final>"


def test_ollama_client_sets_stream_true_when_on_token_given():
    """OllamaModelClient sends stream=true in payload when on_token is provided."""
    captured = {}

    class FakeStreamResponse:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __iter__(self):
            yield json.dumps({"response": "hi", "done": False}).encode() + b"\n"
            yield json.dumps({"response": "", "done": True}).encode() + b"\n"

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeStreamResponse()

    client = OllamaModelClient(model="m", host="http://127.0.0.1:11434", temperature=0.2, top_p=0.9, timeout=30)
    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42, on_token=lambda t: None)

    assert captured["body"]["stream"] is True
    assert result == "hi"


def test_ollama_client_sets_stream_false_when_no_on_token():
    """OllamaModelClient sends stream=false in payload when on_token is None."""
    captured = {}

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"response": "ok"}).encode()

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OllamaModelClient(model="m", host="http://127.0.0.1:11434", temperature=0.2, top_p=0.9, timeout=30)
    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42, on_token=None)

    assert captured["body"]["stream"] is False
    assert result == "ok"


def test_ollama_client_accumulates_streamed_tokens():
    """OllamaModelClient concatenates all token chunks and calls on_token for each."""
    received = []

    class FakeStreamResponse:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __iter__(self):
            for word in ["<final>", "hello", " world", "</final>"]:
                yield json.dumps({"response": word, "done": False}).encode() + b"\n"
            yield json.dumps({"response": "", "done": True}).encode() + b"\n"

    with patch("urllib.request.urlopen", lambda req, timeout: FakeStreamResponse()):
        client = OllamaModelClient(model="m", host="http://127.0.0.1:11434", temperature=0.2, top_p=0.9, timeout=30)
        result = client.complete("p", 512, on_token=received.append)

    assert result == "<final>hello world</final>"
    assert received == ["<final>", "hello", " world", "</final>"]


def test_verbose_agent_calls_on_token(tmp_path):
    """verbose=True agent passes an on_token callback so FakeModelClient records it."""
    received = []
    original_complete = FakeModelClient.complete

    def patched_complete(self, prompt, max_new_tokens, on_token=None):
        result = original_complete(self, prompt, max_new_tokens, on_token=on_token)
        if on_token is not None:
            received.append("callback_was_set")
        return result

    agent = build_agent(tmp_path, ["<final>Done.</final>"], verbose=True)
    with patch.object(FakeModelClient, "complete", patched_complete):
        agent.ask("do something")

    assert "callback_was_set" in received


def test_non_verbose_agent_does_not_set_on_token(tmp_path):
    """verbose=False agent passes on_token=None so no streaming output occurs."""
    callback_set = []
    original_complete = FakeModelClient.complete

    def patched_complete(self, prompt, max_new_tokens, on_token=None):
        if on_token is not None:
            callback_set.append(True)
        return original_complete(self, prompt, max_new_tokens, on_token=on_token)

    agent = build_agent(tmp_path, ["<final>Done.</final>"], verbose=False)
    with patch.object(FakeModelClient, "complete", patched_complete):
        agent.ask("do something")

    assert callback_set == []


# ---------------------------------------------------------------------------
# Auto-verify tests
# ---------------------------------------------------------------------------

def test_detect_test_command_finds_pytest(tmp_path):
    """detect_test_command returns a pytest command when pyproject.toml mentions pytest."""
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    cmd = detect_test_command(tmp_path)
    assert cmd is not None
    assert "pytest" in cmd


def test_detect_test_command_finds_npm_test(tmp_path):
    """detect_test_command returns npm test when package.json has a test script."""
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    cmd = detect_test_command(tmp_path)
    assert cmd == "npm test"


def test_detect_test_command_finds_makefile(tmp_path):
    """detect_test_command returns make test when Makefile has a test target."""
    (tmp_path / "Makefile").write_text("test:\n\tpython -m pytest\n", encoding="utf-8")
    cmd = detect_test_command(tmp_path)
    assert cmd == "make test"


def test_detect_test_command_returns_none_when_no_config(tmp_path):
    """detect_test_command returns None when no recognisable test config is present."""
    assert detect_test_command(tmp_path) is None


def test_auto_verify_appended_after_write_file(tmp_path):
    """auto_verify=True appends test results to write_file tool output."""
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.py"><content>print("hi")\n</content></tool>',
            "<final>Done.</final>",
        ],
        auto_verify=True,
    )

    with patch.object(agent, "_auto_verify", return_value="tests passed (exit 0):\n1 passed"):
        answer = agent.ask("Create hello.py")

    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    write_event = next(e for e in tool_events if e["name"] == "write_file")
    assert "auto-verify:" in write_event["content"]
    assert "tests passed" in write_event["content"]


def test_auto_verify_appended_after_patch_file(tmp_path):
    """auto_verify=True appends test results to patch_file tool output."""
    (tmp_path / "sample.txt").write_text("hello world\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"patch_file","args":{"path":"sample.txt","old_text":"world","new_text":"agent"}}</tool>',
            "<final>Done.</final>",
        ],
        auto_verify=True,
    )

    with patch.object(agent, "_auto_verify", return_value="tests passed (exit 0):\n1 passed"):
        agent.ask("Patch sample.txt")

    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    patch_event = next(e for e in tool_events if e["name"] == "patch_file")
    assert "auto-verify:" in patch_event["content"]


def test_auto_verify_not_called_without_flag(tmp_path):
    """auto_verify=False (default) never calls _auto_verify."""
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.py"><content>print("hi")\n</content></tool>',
            "<final>Done.</final>",
        ],
    )

    with patch.object(agent, "_auto_verify", return_value="should not appear") as mock_verify:
        agent.ask("Create hello.py")

    mock_verify.assert_not_called()


def test_auto_verify_not_called_for_read_only_tools(tmp_path):
    """auto_verify is not triggered by safe tools like list_files or read_file."""
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Done.</final>",
        ],
        auto_verify=True,
    )

    with patch.object(agent, "_auto_verify", return_value="should not appear") as mock_verify:
        agent.ask("Read hello.txt")

    mock_verify.assert_not_called()


def test_auto_verify_real_pytest_passes(tmp_path):
    """_auto_verify detects pytest from pyproject.toml and returns a pass result."""
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "test_trivial.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], auto_verify=True)

    result = agent._auto_verify()

    assert result is not None
    assert "passed" in result


# ---------------------------------------------------------------------------
# Structured planning phase tests
# ---------------------------------------------------------------------------

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


def test_plan_rule_in_prefix_when_plan_mode(tmp_path):
    """plan_mode=True injects planning instruction into the system prompt prefix."""
    agent = build_agent(tmp_path, [], plan_mode=True)
    assert "<plan>" in agent.prefix


def test_plan_rule_absent_from_prefix_by_default(tmp_path):
    """plan_mode=False (default) keeps the prefix free of planning instructions."""
    agent = build_agent(tmp_path, [])
    assert "<plan>" not in agent.prefix


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


# ---------------------------------------------------------------------------
# Persistent agent memory tests
# ---------------------------------------------------------------------------

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
    """update_memory returns an error for blank notes."""
    agent = build_agent(tmp_path, [])
    result = agent.run_tool("update_memory", {"note": "   "})
    assert "error" in result.lower()
    assert "note" in result.lower()


def test_persistent_memory_injected_into_prefix(tmp_path):
    """When AGENT_MEMORY.md exists, its content appears in the system prompt prefix."""
    (tmp_path / "AGENT_MEMORY.md").write_text(
        "- [2026-01-01] user prefers black formatter\n", encoding="utf-8"
    )
    agent = build_agent(tmp_path, [])
    assert "user prefers black formatter" in agent.prefix


def test_no_persistent_memory_file_leaves_prefix_clean(tmp_path):
    """When AGENT_MEMORY.md is absent, the prefix has no persistent memory section header."""
    agent = build_agent(tmp_path, [])
    assert "Persistent memory (from AGENT_MEMORY.md)" not in agent.prefix


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
    (tmp_path / "AGENT_MEMORY.md").write_text(
        "- [2026-01-01] always use type hints\n", encoding="utf-8"
    )
    agent2 = build_agent(tmp_path, [])
    assert "always use type hints" in agent2.prefix


def test_plan_does_not_consume_tool_step_budget(tmp_path):
    """A <plan> response does not count against max_steps.

    With max_steps=2 the loop allows calling the model while tool_steps<2.
    Plan sets tool_steps=0 (not 1), so after 1 tool call (tool_steps=1<2)
    the loop still enters and can read the <final>. If plan counted as a tool
    step the sequence would exhaust the budget before reaching <final>.
    """
    agent = build_agent(
        tmp_path,
        [
            "<plan>\n1. Read\n2. Write\n</plan>",
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>Done.</final>",
        ],
        max_steps=2,
        approval_policy="auto",
    )
    answer = agent.ask("do the task")
    assert answer == "Done."
