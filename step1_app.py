"""
Step 1 FastAPI application — synchronous, in-memory story storage.

This module implements the Step 1 spec exactly:
  POST /chat              → run agent once, store in memory, return story immediately
  GET  /stories/{id}     → retrieve from memory (404 if unknown)
  POST /stories/{id}/approve → approve or reject (409 if already decided)

Step 2 wires flaky_tool into the agent (app/agent.py::write_story).
Step 3 wraps everything in Temporal (app/main.py).
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

_stories: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    prompt: str


class ApproveRequest(BaseModel):
    approved: bool
    approved_by: str = ""


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

step1_app = FastAPI(
    title="AI Story Writer — Step 1",
    description="Synchronous story writing with in-memory HITL approval.",
    version="1.0.0",
)


@step1_app.post("/chat", status_code=200, summary="Write a story and return for review")
async def chat(request: ChatRequest):
    """
    Run the Deep Agent once with the given prompt and return the story.

    The story is stored in memory with status `pending_approval` until a
    reviewer calls `/stories/{story_id}/approve`.
    """
    from app.agent import write_story_only

    story = await write_story_only(request.prompt)
    story_id = str(uuid.uuid4())
    _stories[story_id] = {
        "story_id": story_id,
        "story": story,
        "status": "pending_approval",
        "approved_by": None,
        "approved_at": None,
        "error": None,
    }
    return {"story_id": story_id, "story": story, "status": "pending_approval"}


@step1_app.get("/stories/{story_id}", summary="Get story status and content")
async def get_story(story_id: str):
    """Return a story by ID, or 404 if unknown."""
    if story_id not in _stories:
        raise HTTPException(status_code=404, detail="Story not found")
    return _stories[story_id]


@step1_app.post("/stories/{story_id}/approve", summary="Approve or reject a story")
async def approve_story(story_id: str, request: ApproveRequest):
    """
    Set the story status to `approved` or `rejected`.

    Returns 404 if the story does not exist.
    Returns 409 if the story has already been decided.
    """
    if story_id not in _stories:
        raise HTTPException(status_code=404, detail="Story not found")

    entry = _stories[story_id]
    if entry["status"] in ("approved", "rejected"):
        raise HTTPException(
            status_code=409,
            detail=f"Story already decided: {entry['status']}",
        )

    entry["status"] = "approved" if request.approved else "rejected"
    entry["approved_by"] = request.approved_by or None
    entry["approved_at"] = datetime.now(timezone.utc).isoformat()
    return entry
