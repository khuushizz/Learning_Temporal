"""
AI Story Writer — Step 3 FastAPI application (Temporal-backed).

Step 1 (in-memory, for tests): see app/step1_app.py
Step 2 (MCP in agent):         write_story() in app/agent.py
Step 3 (Temporal, this file):  StoryWorkflow + three activities

Endpoints
---------
POST /chat                         Start a StoryWorkflow → 202 + workflow_id
GET  /stories/{workflow_id}        Application-level state + story text
POST /stories/{workflow_id}/approve  Approve or reject (Temporal signal)

Additional convenience endpoints
GET  /stories                      List workflows (filterable by Temporal status)
GET  /published                    List published story files
GET  /skills                       List skill directories available to the agent
GET  /health                       API + Temporal connectivity check
"""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app.workflows import ReviewSignal, StoryWorkflow, StoryWorkflowInput

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Temporal client — created once at startup, shared across all requests
# ---------------------------------------------------------------------------

_temporal_client: Optional[Client] = None


logger = logging.getLogger(__name__)


async def _ensure_temporal_client() -> Client:
    """Connect lazily and retry a few times when the Temporal server is starting up."""
    global _temporal_client
    if _temporal_client is not None:
        return _temporal_client

    address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")

    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            _temporal_client = await Client.connect(address, namespace=namespace)
            return _temporal_client
        except Exception as exc:  # pragma: no cover - startup depends on local Temporal server
            last_error = exc
            if attempt < 3:
                await asyncio.sleep(0.5 * attempt)
                continue

            _temporal_client = None
            logger.warning(
                "Temporal server not reachable at %s; continuing without it. Start the Temporal dev server to enable workflow endpoints. Detail: %s",
                address,
                exc,
            )
            raise RuntimeError(
                f"Temporal server not reachable at {address}. Start the Temporal dev server to enable workflow endpoints."
            ) from exc

    if last_error is not None:
        raise RuntimeError(
            f"Temporal server not reachable at {address}. Start the Temporal dev server to enable workflow endpoints."
        ) from last_error

    raise RuntimeError(
        f"Temporal server not reachable at {address}. Start the Temporal dev server to enable workflow endpoints."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await _ensure_temporal_client()
    except RuntimeError:
        pass
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

_DESCRIPTION = """
A durable AI story-writing agent with a human-in-the-loop approval gate,
backed by [Temporal](https://temporal.io) for fault tolerance.

## Architecture (Step 3)

```
POST /chat  →  StoryWorkflow (Temporal)
               ├─ write_story_activity        Deep Agent + SKILL.md files
               ├─ call_flaky_tool_activity     MCP flaky_tool, retried ≤ 8× with exponential backoff
               ├─ [wait for /approve signal — up to APPROVAL_TIMEOUT_SECONDS]
               └─ publish_story_activity       writes published/<story_id>.md (idempotent)
```

## Workflow status values

| Status | Meaning |
|--------|---------|
| `generating` | Agent is writing the story |
| `processing` | Story submitted to MCP tool (may be retrying) |
| `pending_approval` | Waiting for a human `/approve` signal |
| `published` | Approved; story written to `published/` |
| `rejected` | Reviewer sent `approved: false` |
| `failed` | MCP tool exhausted all 8 retry attempts |
| `expired` | No review received within the timeout window |
"""

app = FastAPI(
    title="AI Story Writer",
    description=_DESCRIPTION,
    version="3.0.0",
    lifespan=lifespan,
)


def _client() -> Client:
    if _temporal_client is None:
        raise RuntimeError(
            "Temporal client not initialized. Start the Temporal dev server with 'temporal server start-dev' before using workflow endpoints."
        )
    return _temporal_client


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    prompt: str

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "prompt": "Write a Harry Potter Season 1 Movie 1 story"
                },
                {
                    "prompt": "Write a Bollywood romance story set during a monsoon in Jaipur"
                },
            ]
        }
    }


class ApproveRequest(BaseModel):
    approved: bool
    approved_by: str = ""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"approved": True, "approved_by": "alice"},
                {"approved": False, "approved_by": "bob"},
            ]
        }
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TEMPORAL_STATUS_MAP = {
    WorkflowExecutionStatus.RUNNING: "running",
    WorkflowExecutionStatus.COMPLETED: "completed",
    WorkflowExecutionStatus.FAILED: "failed",
    WorkflowExecutionStatus.CANCELED: "cancelled",
    WorkflowExecutionStatus.TERMINATED: "terminated",
    WorkflowExecutionStatus.TIMED_OUT: "timed_out",
}

_STATUS_FILTER_MAP = {
    "running": "Running",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "terminated": "Terminated",
    "timed_out": "TimedOut",
}


async def _query_workflow(workflow_id: str) -> dict:
    """Return application-level state from a running or completed workflow."""
    await _ensure_temporal_client()
    handle = _client().get_workflow_handle(workflow_id)
    try:
        state = await handle.query(StoryWorkflow.get_state)
        return {"workflow_id": workflow_id, **state}
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail="Workflow not found")
        raise HTTPException(status_code=500, detail=str(e))


def _read_skill_frontmatter(skill_dir: Path) -> Optional[dict]:
    """Parse name and description from a SKILL.md YAML frontmatter block."""
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        return None
    raw = skill_file.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        return None
    name = skill_dir.name
    description = ""
    for line in raw.splitlines()[1:]:
        if line.strip() == "---":
            break
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("description:"):
            description = line.split(":", 1)[1].strip()
    return {"name": name, "description": description}


# ---------------------------------------------------------------------------
# System endpoints
# ---------------------------------------------------------------------------


@app.get("/health", tags=["System"], summary="Health check")
async def health():
    """Returns API status and whether the Temporal server is reachable."""
    try:
        await _ensure_temporal_client()
    except RuntimeError as exc:
        return {"api": "ok", "temporal": "unreachable", "detail": str(exc)}

    try:
        async for _ in _client().list_workflows(
            query="WorkflowType='StoryWorkflow'", page_size=1
        ):
            break
        return {"api": "ok", "temporal": "ok"}
    except Exception as exc:
        _temporal_client = None
        return {"api": "ok", "temporal": "unreachable", "detail": str(exc)}


@app.post("/chat/sync", tags=["Chat"], summary="Chat with the LLM — instant response")
async def chat_sync(request: ChatRequest):
    """
    Send a prompt and get the story back immediately — no workflow, no polling.

    The Deep Agent picks the right skill automatically (Harry Potter, Bollywood, etc.)
    and returns the full story text in one response.
    """
    from app.agent import write_story_only
    story = await write_story_only(request.prompt)
    return {"story": story, "prompt": request.prompt, "status": "ok"}


@app.get("/skills", tags=["System"], summary="List available agent skills")
async def list_skills():
    """
    Lists skill directories found under `SKILLS_DIR`.

    Each skill needs a `SKILL.md` with YAML frontmatter (`name` + `description`).
    The Deep Agent loads matching skills automatically based on the user's prompt.
    """
    skills_dir = Path(os.getenv("SKILLS_DIR", "skills/"))
    if not skills_dir.exists():
        return {"skills": [], "skills_dir": str(skills_dir)}

    skills = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        meta = _read_skill_frontmatter(child)
        if meta is None:
            continue
        skills.append(
            {
                "name": meta["name"],
                "description": meta["description"],
                "directory": str(child),
            }
        )

    return {"skills": skills, "skills_dir": str(skills_dir)}


# ---------------------------------------------------------------------------
# Core story endpoints (Step 3 — Temporal)
# ---------------------------------------------------------------------------


@app.post(
    "/chat",
    tags=["Stories"],
    summary="Start a story-writing workflow",
    status_code=202,
)
async def start_story(request: ChatRequest):
    """
    Start a durable `StoryWorkflow` and return immediately with the `workflow_id`.

    The workflow will:
    1. Write a story via the Deep Agent (loads SKILL.md skill files automatically)
    2. Submit it through the MCP `flaky_tool` (retried ≤ 8 × with exponential backoff)
    3. Wait up to `APPROVAL_TIMEOUT_SECONDS` for a human review signal
    4. If approved, publish the story to `published/<story_id>.md`

    Poll `GET /stories/{workflow_id}` for progress, then send a decision to
    `POST /stories/{workflow_id}/approve`.
    """
    story_id = str(uuid.uuid4())
    workflow_id = f"story-{story_id}"
    timeout_seconds = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "86400"))

    await _ensure_temporal_client()
    await _client().start_workflow(
        StoryWorkflow.run,
        StoryWorkflowInput(
            prompt=request.prompt,
            story_id=story_id,
            timeout_seconds=timeout_seconds,
        ),
        id=workflow_id,
        task_queue=os.getenv("TASK_QUEUE", "story-queue"),
    )

    return {"workflow_id": workflow_id, "status": "started"}


@app.get(
    "/stories/{workflow_id}",
    tags=["Stories"],
    summary="Get story status and content",
)
async def get_story(workflow_id: str):
    """
    Query the workflow for its current application-level state.

    Returns `status`, the `story` text (once written), `prompt`,
    and review metadata (`approved_by`, `approved_at`) when available.
    Returns 404 if the workflow does not exist.
    """
    return await _query_workflow(workflow_id)


@app.post(
    "/stories/{workflow_id}/approve",
    tags=["Stories"],
    summary="Approve or reject a pending story",
)
async def approve_story(workflow_id: str, request: ApproveRequest):
    """
    Send a human review signal to the workflow.

    The workflow must be in `pending_approval` status.

    * `approved: true`  → story is published to `published/<story_id>.md`
    * `approved: false` → workflow moves to `rejected`

    Signals are durable: they are persisted by the Temporal server and
    delivered to the workflow even if the worker was restarted.
    """
    await _ensure_temporal_client()
    handle = _client().get_workflow_handle(workflow_id)
    try:
        await handle.signal(
            StoryWorkflow.review,
            ReviewSignal(
                approved=request.approved,
                approved_by=request.approved_by,
            ),
        )
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail="Workflow not found")
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "signal_sent"}


# ---------------------------------------------------------------------------
# Convenience listing endpoints
# ---------------------------------------------------------------------------


@app.get("/stories", tags=["Stories"], summary="List story workflows")
async def list_stories(
    limit: int = Query(default=20, ge=1, le=100),
    status: Optional[str] = Query(
        default=None,
        description="Filter by Temporal execution status: running, completed, failed, cancelled, terminated, timed_out",
    ),
):
    """
    List `StoryWorkflow` executions (most recently started first).

    `status` filters by **Temporal** execution status, not the application-level
    status. Use `GET /stories/{workflow_id}` for the application-level status.
    """
    query = "WorkflowType='StoryWorkflow'"
    if status:
        ts = _STATUS_FILTER_MAP.get(status.lower())
        if ts is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown status '{status}'. Valid: {', '.join(_STATUS_FILTER_MAP)}",
            )
        query += f" AND ExecutionStatus='{ts}'"

    await _ensure_temporal_client()
    results = []
    async for wf in _client().list_workflows(query=query, page_size=limit):
        results.append(
            {
                "workflow_id": wf.id,
                "execution_status": _TEMPORAL_STATUS_MAP.get(wf.status, str(wf.status).lower()),
                "started_at": wf.start_time.isoformat() if wf.start_time else None,
                "closed_at": wf.close_time.isoformat() if wf.close_time else None,
            }
        )
        if len(results) >= limit:
            break

    return {"stories": results, "count": len(results)}


@app.get("/published", tags=["Published"], summary="List published stories")
async def list_published():
    """
    List all stories approved and written to `published/`.

    Stories are sorted most recently published first. The story text is not
    included — use `GET /stories/{workflow_id}` or read the file directly.
    """
    published_dir = Path("published")
    if not published_dir.exists():
        return {"published": [], "count": 0}

    stories = []
    for file in sorted(
        published_dir.glob("*.md"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    ):
        stories.append(
            {
                "story_id": file.stem,
                "workflow_id": f"story-{file.stem}",
                "file": str(file),
                "published_at": datetime.fromtimestamp(
                    file.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )

    return {"published": stories, "count": len(stories)}
