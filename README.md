# AI Story Writer

A durable AI story-writing agent with human-in-the-loop approval, built with
FastAPI, a Deep Agent (LangGraph), MCP tools, and Temporal for fault tolerance.

## Architecture

```
POST /chat  →  StoryWorkflow (Temporal)
               ├─ write_story_activity        Deep Agent + SKILL.md files
               ├─ call_flaky_tool_activity     MCP flaky_tool, retried ≤ 8× (exponential backoff)
               ├─ [wait for /approve signal — up to APPROVAL_TIMEOUT_SECONDS]
               └─ publish_story_activity       writes published/<story_id>.md (idempotent)
```

## The three steps

| Step | Module | What it adds |
|------|--------|-------------|
| **Step 1** | `app/step1_app.py` | FastAPI + Deep Agent (no tools) + HITL in memory |
| **Step 2** | `app/agent.py::write_story` | MCP `flaky_tool` wired into the agent; failures surface as `{"status":"failed"}` |
| **Step 3** | `app/main.py` + `app/workflows.py` | Temporal wraps the whole run; LLM call and tool call are separate activities; 8-attempt retry; durable approval signal |

## Workflow status values

| Status | Meaning |
|--------|---------|
| `generating` | Agent is writing the story |
| `processing` | Story submitted to MCP tool (may be retrying) |
| `pending_approval` | Waiting for a human `/approve` signal |
| `published` | Approved; written to `published/` |
| `rejected` | Reviewer sent `approved: false` |
| `failed` | MCP tool exhausted all 8 retry attempts |
| `expired` | No review received within the timeout window |

---

## Design decisions

### Step 3: MCP tool call moved out of the agent

In Step 2 the agent calls `flaky_tool` internally during its LangGraph run.
In Step 3 the tool call is lifted out into a separate Temporal activity
(`call_flaky_tool_activity`) so that Temporal — not the agent — controls the
retry loop.

**Why this matters:**

- The agent's retry would be bounded by `AGENT_RECURSION_LIMIT` (safety stop,
  not configurable retry logic). The Temporal activity retry is configured with
  `RetryPolicy(maximum_attempts=8, initial_interval=1s, backoff_coefficient=2)`,
  which gives the required 7-8 attempts with exponential backoff.
- Temporal's retry is durable: if the worker crashes mid-backoff, the retry
  counter is preserved in the workflow history and the next worker picks up
  exactly where it left off. An in-agent retry would restart from zero.
- Separating concerns makes each activity independently observable in the
  Temporal UI (timing, attempt count, error messages per attempt).

**Gap against the spec:** The spec says "The Deep Agent calls the MCP tool
during its run" (Step 2 intent). In Step 3 the agent run (`write_story_activity`)
does NOT call the tool — it just writes the story. The `call_flaky_tool_activity`
calls the tool separately. This is an intentional deviation, documented here and
accepted per the decision already made: "Step 3 moves the MCP tool call out of
the agent run into its own Temporal activity."

**Kind/Helm:** Not set up. Temporal runs on a local dev server
(`temporal server start-dev`). Kubernetes deployment is a later step.

---

## Prerequisites

- Python 3.11+
- [Temporal CLI](https://docs.temporal.io/cli) (`temporal server start-dev`)
- OpenAI API key (`OPENAI_API_KEY`)

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
cp .env.example .env
# Edit .env — add your OPENAI_API_KEY
```

## Running

**Terminal 1 — Temporal dev server**
```bash
temporal server start-dev
# UI:   http://localhost:8233
# gRPC: localhost:7233
```

**Terminal 2 — Worker**
```bash
python -m app.worker
```

**Terminal 3 — Step 3 API**
```bash
uvicorn app.main:app --reload --port 8000
# Swagger UI: http://localhost:8000/docs
```

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | API + Temporal connectivity check |
| `GET` | `/skills` | List skill directories available to the agent |
| `POST` | `/chat` | Start a story workflow (202, returns immediately) |
| `GET` | `/stories` | List workflows (`?status=running\|completed\|…`, `?limit=N`) |
| `GET` | `/stories/{workflow_id}` | Application-level state + story text |
| `POST` | `/stories/{workflow_id}/approve` | Approve or reject a pending story |
| `GET` | `/published` | List published story files |

---

## Quick walkthrough

### 1. Start a story

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write a Harry Potter Season 1 Movie 1 story"}'
```
```json
{"workflow_id": "story-<uuid>", "status": "started"}
```

### 2. Poll until pending_approval

```bash
curl http://localhost:8000/stories/story-<uuid>
```
```json
{
  "workflow_id": "story-<uuid>",
  "story_id": "<uuid>",
  "status": "pending_approval",
  "story": "Harry stepped into the Forbidden Forest...",
  "prompt": "Write a Harry Potter Season 1 Movie 1 story",
  "approved_by": null,
  "approved_at": null
}
```

### 3. Approve

```bash
curl -X POST http://localhost:8000/stories/story-<uuid>/approve \
  -H "Content-Type: application/json" \
  -d '{"approved": true, "approved_by": "alice"}'
```
```json
{"status": "signal_sent"}
```

### 4. Reject

```bash
curl -X POST http://localhost:8000/stories/story-<uuid>/approve \
  -H "Content-Type: application/json" \
  -d '{"approved": false, "approved_by": "bob"}'
```

### 5. List published

```bash
curl http://localhost:8000/published
```

---

## Running the tests

```bash
# Install deps
pip install -r requirements.txt

# Run all automated tests (no real Temporal server or API key needed)
pytest tests/ -v

# Run just step 1
pytest tests/test_step1.py -v

# Run just step 2
pytest tests/test_step2.py -v

# Run just step 3
pytest tests/test_step3.py -v
```

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | — | OpenAI API key (required) |
| `MODEL_NAME` | `gpt-4o` | LLM model name |
| `SKILLS_DIR` | `skills/` | Path to skill directories |
| `AGENT_RECURSION_LIMIT` | `10` | LangGraph recursion cap (safety stop) |
| `MCP_SERVER_COMMAND` | `python` | Command to start the MCP server |
| `MCP_SERVER_ARGS` | `mcp_server/flaky_server.py` | MCP server script path |
| `FLAKY_FAILURES` | `always` | `always` = always fail; `N` = fail first N calls then succeed |
| `TEMPORAL_ADDRESS` | `localhost:7233` | Temporal server gRPC address |
| `TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `TASK_QUEUE` | `story-queue` | Temporal task queue name |
| `APPROVAL_TIMEOUT_SECONDS` | `86400` | Seconds the workflow waits before expiring (default 24 h) |

---

## Testing scenarios (manual)

### A — Tool always fails (exhaust retries)
```
FLAKY_FAILURES=always  (default)
```
Start a story → workflow ends as `failed` after 8 attempts.
Check attempt count in Temporal UI → Event History for the workflow.

### B — Tool fails twice then succeeds
```
FLAKY_FAILURES=2
# Delete mcp_server/.flaky_state.json first to reset counter
```
Start a story → 2 failed attempts, 3rd succeeds → `pending_approval`.

### C — Durability: worker restart while pending
1. Start a story; wait for `pending_approval`
2. Kill worker and API
3. Restart both
4. Send approve → workflow completes and publishes

### D — Durability: worker restart during retries
1. `FLAKY_FAILURES=always`
2. Start a story; wait ~3 retry attempts
3. Kill worker; restart
4. Total attempts must still be 8, not 8+3

### E — Approval timeout
```
APPROVAL_TIMEOUT_SECONDS=60
```
Start a story; reach `pending_approval`; do nothing for 60 s → status becomes `expired`.

---

## Skills

Each skill lives in its own directory under `SKILLS_DIR`:

```
skills/
  harry-potter-story/
    SKILL.md       ← YAML frontmatter: name + description + skill instructions
  bollywood-story/
    SKILL.md
```

The `name:` field in the frontmatter **must match the directory name exactly**.
The agent loads matching skills automatically based on the prompt.

---

## Project structure

```
app/
  main.py          Step 3 FastAPI app (Temporal-backed)
  step1_app.py     Step 1 FastAPI app (in-memory, used in tests)
  agent.py         Deep Agent setup: write_story_only (Step 1/3), write_story (Step 2)
  mcp_client.py    MCP stdio client helper (list_tools, call_flaky_tool)
  activities.py    Temporal activities (write_story, call_flaky_tool, publish_story)
  workflows.py     StoryWorkflow definition
  worker.py        Temporal worker entry point
mcp_server/
  flaky_server.py  FastMCP server with flaky_tool
skills/
  harry-potter-story/SKILL.md
tests/
  test_step1.py    S1-01 to S1-10
  test_step2.py    S2-01 to S2-03
  test_step3.py    S3-01 to S3-09
published/         Created at runtime; stores approved story .md files
.env.example
requirements.txt
README.md
```

## Published stories

Approved stories are written to `published/<story_id>.md`.
`publish_story_activity` is idempotent: if the file already exists with
identical content it does nothing, so re-running the activity (e.g. after a
worker crash) is safe.
