"""
Temporal activities for the AI Story Writer.

Each function is a pure side-effectful unit. The workflow orchestrates them;
retry logic lives in the workflow's execute_activity calls, not here.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from temporalio import activity

load_dotenv(override=True)


@activity.defn
async def write_story_activity(prompt: str) -> str:
    """Run the Deep Agent to write a story. Does NOT call flaky_tool (Step 3)."""
    from app.agent import write_story_only

    return await write_story_only(prompt)


@activity.defn
async def call_flaky_tool_activity(story: str) -> str:
    """
    Call flaky_tool via MCP.

    Raises RuntimeError on failure so Temporal applies the RetryPolicy defined
    in the workflow (max 8 attempts, exponential backoff).
    """
    from app.mcp_client import call_flaky_tool

    return await call_flaky_tool(story)


@activity.defn
async def publish_story_activity(story_id: str, story: str) -> None:
    """Write the approved story to published/<story_id>.md. Idempotent."""
    published_dir = Path("published")
    published_dir.mkdir(exist_ok=True)
    file_path = published_dir / f"{story_id}.md"

    if file_path.exists() and file_path.read_text(encoding="utf-8") == story:
        return  # already published with identical content — no-op

    file_path.write_text(story, encoding="utf-8")
