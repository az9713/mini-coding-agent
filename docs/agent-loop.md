# The Agent Loop

## Overview

The agent loop is the core control flow that turns a single user message into a final answer. It works by repeatedly asking the language model what to do next, interpreting the model's response as either a tool call, a malformed output that needs correction, or a finished answer. Each iteration narrows the distance between the initial request and the solution: the model reads tool results, updates its understanding of the workspace, and decides whether it needs more information or is ready to respond. The loop is bounded by two independent budget counters so that neither runaway tool use nor a broken model can spin forever.

---

## The Loop At A Glance

```
User message
     |
     v
record(user)  -- save message to history
set task      -- store first message as task in memory
     |
     v
+------------------------------------------------------------+
|  while tool_steps < max_steps                             |
|         and attempts < max_attempts                       |
|                                                            |
|    attempts += 1                                          |
|    raw = model.complete(prompt(user_message))             |
|         |                                                  |
|    kind, payload = parse(raw)                             |
|         |                                                  |
|    +----+----------+------------------+                   |
|    |               |                  |                   |
|  "tool"         "retry"           "final"                 |
|    |               |                  |                   |
|  tool_steps+=1  record(notice)    record(answer)          |
|  run_tool()     (no tool_steps    remember(notes)         |
|  record(result)  increment)       return answer -----+    |
|  note_tool()    continue          |                  |    |
|  continue          |              |                  |    |
|    +---------------+              |                  |    |
|    |                              |                  |    |
+----+------------------------------+------------------+    |
     |                                                      |
     v                                                      |
budget exhausted:                                           |
  if attempts >= max_attempts                               |
     and tool_steps < max_steps:                           |
       "Stopped after too many malformed                    |
        model responses..."                                 |
  else:                                                     |
       "Stopped after reaching the step                     |
        limit without a final answer."                      |
     |                                                      |
     v                                                      |
  record(error message)                                     |
  return error message <------------------------------------+
```

The loop has exactly one entry point (a user message) and exactly one exit point (a `return` statement that delivers either the model's answer or a budget-exhaustion message). Every path through the diagram eventually reaches that exit.

---

## Budget Accounting

The loop uses two counters, and understanding why they are separate is essential to understanding the loop's safety properties.

**`tool_steps`** counts the number of times a tool was actually executed. It increments only when `parse()` returns `"tool"` and `run_tool()` is called. This counter maps directly to the `--max-steps` CLI flag. When `tool_steps` reaches `max_steps`, the loop exits. The intent is to limit how much the agent can *do* — read files, write files, run shell commands — in response to a single user message.

**`attempts`** counts every call to `model_client.complete()`, regardless of what the model returned. This includes tool calls, final answers, and malformed outputs that trigger a retry. The loop exits if `attempts` reaches `max_attempts` even if `tool_steps` is still below the limit.

The formula that ties them together is:

```python
max_attempts = max(self.max_steps * 3, self.max_steps + 4)
```

**Concrete example with `--max-steps 6`:**

```
max_attempts = max(6 * 3, 6 + 4)
             = max(18, 10)
             = 18
```

This means the agent can make up to 18 total model calls while executing at most 6 tool steps. The remaining 12 calls (18 - 6) are available as a retry budget for malformed model responses. If the model consistently produces garbage output, the loop will still terminate after 18 calls rather than spinning indefinitely.

**Why not use a single counter?** If retries consumed the tool budget, a model that repeatedly produced malformed output would exhaust the agent's ability to take useful actions before it ever ran a tool. Separating the counters means malformed output costs retry budget, not tool budget. The model gets more chances to correct itself without reducing the agent's capacity to do useful work.

| Counter | Incremented when | Caps at |
|---------|-----------------|---------|
| `tool_steps` | `parse()` returns `"tool"` | `max_steps` (default: 6) |
| `attempts` | every `model.complete()` call | `max_attempts = max(max_steps * 3, max_steps + 4)` |

---

## The Three Response Kinds

`parse()` always returns a two-element tuple `(kind, payload)`. The `kind` string is one of exactly three values. The loop handles each differently.

**`"tool"`** means the model wants to call a function. The payload is a dict with at least a `"name"` key and an `"args"` key. The loop increments `tool_steps`, calls `run_tool(name, args)`, records the result to history, and calls `note_tool()` to update working memory. Then it `continue`s — the model will see the tool result on the next iteration.

**`"retry"`** means the model's output was structurally malformed: invalid JSON, missing tool name, empty `<final>` tag, completely empty response. The payload is a retry notice string (see the next section). The loop records this notice as an assistant-role item in history and `continue`s. The notice is now visible to the model on the next iteration, which should prompt it to correct its output. `tool_steps` is not incremented.

**`"final"`** means the model is done. The payload is the answer text. The loop records it to history, adds a clipped version to the notes in working memory, and returns it. The loop terminates.

---

## Parse Decision Tree

`parse()` checks the raw model output against a fixed priority order. The first matching branch wins.

```
raw model output (string)
        |
        v
  Contains "<tool>" tag?
  AND ("<final>" absent OR "<tool>" appears first?)
        |
      yes --> Extract body between <tool>...</tool>
                |
              Valid JSON?
                |
              no  --> return ("retry", notice: "malformed tool JSON")
                |
              yes --> Is it a dict?
                        |
                      no  --> return ("retry", notice: "payload must be a JSON object")
                        |
                      yes --> Has non-empty "name" field?
                                |
                              no  --> return ("retry", notice: "missing tool name")
                                |
                              yes --> Normalize args
                                       return ("tool", payload)
        |
      no
        |
        v
  Contains "<tool" (attribute-style XML tag)?
  AND ("<final>" absent OR "<tool" appears first?)
        |
      yes --> parse_xml_tool(raw)
                |
              returns None? --> return ("retry", notice)
                |
              returns dict  --> return ("tool", payload)
        |
      no
        |
        v
  Contains "<final>" tag?
        |
      yes --> Extract body between <final>...</final>
                |
              Non-empty? --> return ("final", text)
                |
              Empty     --> return ("retry", notice: "empty <final> answer")
        |
      no
        |
        v
  Bare text (no recognized tags)?
        |
      non-empty --> return ("final", raw)
        |
      empty     --> return ("retry", notice: "model returned an empty response")
```

The priority order matters. The `<tool>` check precedes the `<final>` check, so a response that contains both tags is treated as a tool call if the tool tag appears first, and as a final answer if the final tag appears first. This handles model outputs that accidentally include both tags.

The bare-text fallback at the bottom is intentional. Some models — especially smaller local models — do not reliably wrap their answers in tags. Rather than forcing a retry every time, the agent accepts bare non-empty text as a final answer. This is a pragmatic concession to the reliability characteristics of small local models.

---

## Retry Mechanics

When `parse()` returns `"retry"`, the payload is a string generated by `retry_notice()`. The full text looks like this:

```
Runtime notice: model returned malformed tool JSON. Reply with a valid
<tool> call or a non-empty <final> answer. For multi-line files, prefer
<tool name="write_file" path="file.py"><content>...</content></tool>.
```

The notice does three things simultaneously. First, it names the specific problem so the model knows exactly what went wrong. Second, it restates the valid output formats so the model has a concrete template to follow. Third, it hints at the XML format for multi-line content, which is the most common source of JSON-encoding errors.

This notice is recorded to history with `role: "assistant"`. That placement is deliberate. Because the transcript is built from the actual history, the notice appears in the conversation at the position where the bad response would have been. On the next model call, the model sees the notice as part of its own prior output and is prompted to correct course.

```python
# In the loop, a retry is handled like this:
if kind == "retry":
    self.record({"role": "assistant", "content": payload, "created_at": now()})
    continue  # attempts already incremented; tool_steps unchanged
```

The notice is a lightweight self-healing mechanism. It does not require any external intervention — the model sees its "mistake" and the expected correction format, and the next iteration begins.

---

## Format Flexibility

The agent accepts tool calls in two distinct formats. Both produce the same `(kind, payload)` tuple from `parse()`, so the rest of the loop is identical regardless of which format was used.

**JSON format** (checked first):

```
<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>
```

This format is compact and easy for the model to generate for simple tools with scalar arguments. The entire payload is a single JSON object.

**XML attribute format** (checked second):

```xml
<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):
    lo, hi = 0, len(nums) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if nums[mid] == target:
            return mid
        elif nums[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1
</content></tool>
```

The XML format solves a real problem. When a model needs to write a multi-line Python file, encoding that content as a JSON string requires escaping every newline as `\n`, every quote as `\"`, and every backslash as `\\`. Small local models frequently make mistakes in this escaping, producing invalid JSON. The XML format sidesteps the problem entirely: the content sits between tags with no escaping required, and `extract_raw()` retrieves it verbatim.

The `parse_xml_tool()` method handles `write_file`, `patch_file`, `run_shell`, `delegate`, and `search` via this format. The prompt includes an explicit instruction to prefer XML for multi-line content:

```
For write_file and patch_file with multi-line text, prefer XML style:
<tool name="write_file" path="file.py"><content>...</content></tool>
```

The rules section in the prompt lists both formats with examples so the model can see exactly what each looks like.

---

## The `run_tool` Pipeline

After `parse()` returns `("tool", payload)`, the loop calls `run_tool(name, args)`. This method is a sequential pipeline of five gates. If any gate rejects the call, it returns an error string rather than raising an exception — the error string goes into history as the tool result, and the model reads it on the next iteration.

```
run_tool(name, args)
      |
      v
1. Tool lookup
   tools.get(name)
   |
   None? --> return "error: unknown tool 'name'"
   |
   found
      |
      v
2. validate_tool(name, args)
   Schema + precondition checks:
   - required args present
   - path within workspace
   - line range valid (read_file)
   - old_text occurs exactly once (patch_file)
   - timeout in [1, 120] (run_shell)
   |
   raises? --> return "error: invalid arguments..."
              + example of correct call
      |
      v
3. repeated_tool_call(name, args)
   Checks last 2 tool events in history.
   Both identical to this call?
   |
   yes --> return "error: repeated identical tool call..."
      |
      v
4. approve(name, args)
   Only for risky tools (write_file, patch_file, run_shell).
   approval_policy == "auto"  --> True
   approval_policy == "never" --> False
   approval_policy == "ask"   --> prompt user at terminal
   |
   denied? --> return "error: approval denied for name"
      |
      v
5. tool["run"](args)
   Execute the actual tool function.
   |
   exception? --> return "error: tool name failed: ..."
   |
   success --> clip(result, 4000)
               return clipped string
```

**Gate 3 (repeated call detection)** deserves special mention. It checks whether the two most recent tool events in history have the same name and the same args dict. If they do, it refuses the call. This prevents the most common infinite loop pattern: a model that reads the same file twice in a row without making progress. The check looks only at the last two tool events, so the model can legitimately call the same tool again after doing something different in between.

**Gate 4 (approval)** is the human-in-the-loop mechanism. Any tool marked `"risky": True` — `write_file`, `patch_file`, and `run_shell` — requires the approval gate to pass before execution. The default policy is `ask`, which pauses and prompts the user at the terminal. Automated pipelines can use `--approval auto` to skip prompting. Child agents spawned by `delegate` always receive `approval_policy="never"` and cannot execute risky tools regardless of the parent's policy.

**Gate 5 output clipping** limits the tool result to 4000 characters via `clip()`. Without this limit, a `read_file` call on a large file or a `run_shell` call that produces verbose output would flood the model's context window. If the output is truncated, `clip()` appends a note: `...[truncated N chars]`.

After `run_tool()` returns, the loop records the result and calls `note_tool()`, which updates the working memory: the accessed file path is added to the LRU file list, and a one-line summary of the result is added to the notes list.

---

## Exit Conditions

The loop has four exit paths. Two are normal, two are error conditions.

**Exit 1: Final answer (normal)**

```python
final = (payload or raw).strip()
self.record({"role": "assistant", "content": final, "created_at": now()})
self.remember(memory["notes"], clip(final, 220), 5)
return final
```

`parse()` returned `"final"`. The answer is recorded, a clipped version is stored in the notes, and the method returns. This is the expected happy path.

**Exit 2: Step limit exhausted**

```python
final = "Stopped after reaching the step limit without a final answer."
```

`tool_steps` reached `max_steps` (default: 6) and the model never produced a final answer. The agent completed its full tool budget without concluding. This typically means the task was too complex for the step limit, or the model was stuck in a tool-calling loop.

**Exit 3: Retry limit exhausted**

```python
final = "Stopped after too many malformed model responses without a valid tool call or final answer."
```

`attempts` reached `max_attempts` while `tool_steps < max_steps`. The model used up all of its retry budget producing malformed output. This typically means the model does not follow the output format instructions reliably, and a larger or better-tuned model should be tried.

**Exit 4: Implicit (never reached in practice)**

The `while` condition `tool_steps < max_steps and attempts < max_attempts` exits the loop when either counter is exhausted. The `if/else` block after the loop covers both cases. The two error messages are deliberately distinct so users can diagnose which limit was hit.

| Exit path | Condition | Message style |
|-----------|-----------|---------------|
| Final answer | `parse()` returns `"final"` | The model's answer text |
| Step limit | `tool_steps >= max_steps` | "Stopped after reaching the step limit..." |
| Retry limit | `attempts >= max_attempts` and `tool_steps < max_steps` | "Stopped after too many malformed model responses..." |
