"""
StoryWorkflow — durable story generation, tool retry, and human approval.

Flow
----
1. write_story_activity  — Deep Agent + skills, no retries needed (LLM)
2. call_flaky_tool_activity — MCP flaky_tool, retried up to 8× with exponential backoff
3. wait_condition — blocks until a review signal arrives or the timeout expires
4. publish_story_activity  — writes published/<story_id>.md (idempotent)
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.activities import (
        call_flaky_tool_activity,
        publish_story_activity,
        write_story_activity,
    )


# ---------------------------------------------------------------------------
# Input / signal dataclasses
# ---------------------------------------------------------------------------


@dataclass
class StoryWorkflowInput:
    prompt: str
    story_id: str
    timeout_seconds: int = 86400


@dataclass
class ReviewSignal:
    approved: bool
    approved_by: str = ""


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow.defn
class StoryWorkflow:
    def __init__(self) -> None:
        self._status: str = "generating"
        self._story_id: str = ""
        self._prompt: str = ""
        self._story: str = ""
        self._error: Optional[str] = None
        self._approved: Optional[bool] = None
        self._approved_by: Optional[str] = None
        self._approved_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Signal — sent by the human reviewer
    # ------------------------------------------------------------------

    @workflow.signal
    async def review(self, sig: ReviewSignal) -> None:
        self._approved = sig.approved
        self._approved_by = sig.approved_by
        self._approved_at = workflow.now()

    # ------------------------------------------------------------------
    # Query — read current state without advancing the workflow
    # ------------------------------------------------------------------

    @workflow.query
    def get_state(self) -> dict:
        return {
            "story_id": self._story_id,
            "prompt": self._prompt,
            "status": self._status,
            "story": self._story,
            "error": self._error,
            "approved_by": self._approved_by,
            "approved_at": (
                self._approved_at.isoformat() if self._approved_at else None
            ),
        }

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, inp: StoryWorkflowInput) -> dict:
        self._story_id = inp.story_id
        self._prompt = inp.prompt

        # (a) Write the story — Deep Agent + skills, no tool call here
        self._story = await workflow.execute_activity(
            write_story_activity,
            inp.prompt,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=1),  # fail fast; story writing isn't retried
        )

        if not self._story:
            self._status = "failed"
            self._error = "Agent returned an empty story"
            return {"status": "failed", "error": self._error}

        # (b) Submit via flaky_tool — retried up to 8 times with exponential backoff
        self._status = "processing"
        try:
            await workflow.execute_activity(
                call_flaky_tool_activity,
                self._story,
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    backoff_coefficient=2.0,
                    maximum_attempts=8,
                ),
                start_to_close_timeout=timedelta(seconds=30),
            )
        except ActivityError as e:
            cause = e.cause
            self._error = (
                cause.message if isinstance(cause, ApplicationError)
                else str(cause) if cause
                else str(e)
            )
            self._status = "failed"
            return {"status": "failed", "error": self._error}

        # (c) Wait for human review signal
        self._status = "pending_approval"
        try:
            await workflow.wait_condition(
                lambda: self._approved is not None,
                timeout=float(inp.timeout_seconds),
            )
        except asyncio.TimeoutError:
            self._status = "expired"
            return {"status": "expired"}

        # (d) Approved → publish
        if self._approved:
            await workflow.execute_activity(
                publish_story_activity,
                args=[inp.story_id, self._story],
                start_to_close_timeout=timedelta(seconds=30),
            )
            self._status = "published"
            return {"status": "published", "story": self._story}

        # (e) Rejected
        self._status = "rejected"
        return {"status": "rejected"}
