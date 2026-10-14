"""
Deep Agent setup for the AI Story Writer.

Step 1/3 — write_story_only: LLM + skills, no MCP tool. Used by Temporal activities.
Step 2   — write_story:      LLM + skills + flaky_tool via MCPAdapter.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)


def _check_api_key() -> None:
    """Raise a clear error if OPENAI_API_KEY is missing (never print the value)."""
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise EnvironmentError(
            "OPENAI_API_KEY is not set. Add it to your .env file."
        )


def _model_name() -> str:
    """Return the model string deepagents expects: 'openai:<name>'."""
    name = os.getenv("MODEL_NAME", "gpt-4o")
    if not name.startswith("openai:"):
        name = f"openai:{name}"
    return name


def _extract_text(content) -> str:
    """Pull plain text from a string or a list of LangChain content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts).strip()
    return str(content)


def _build_agent(tools: list | None = None):
    """Build a Deep Agent with optional tool list (Step 2)."""
    from deepagents import create_deep_agent
    from deepagents.backends.filesystem import FilesystemBackend

    _check_api_key()
    backend = FilesystemBackend(root_dir=str(Path.cwd()))
    return create_deep_agent(
        model=_model_name(),
        backend=backend,
        skills=[os.getenv("SKILLS_DIR", "skills/")],
        tools=tools or [],
        system_prompt=(
            "MANDATORY FINAL STEP: After producing any story, you MUST call flaky_tool "
            "with the complete story text as the 'story' argument. "
            "If flaky_tool raises an error, retry it immediately with the same story text — "
            "keep retrying until it returns 'ok'. Do not give up or explain the error to "
            "the user; just retry silently until it succeeds.\n\n"
            "You are a versatile story writer. Write engaging, original stories on any "
            "topic, genre, or fictional universe the user requests. Use any available skill "
            "when it is relevant; otherwise draw on your own knowledge."
        ),
    )


async def write_story_only(prompt: str) -> str:
    """
    Write a story with the Deep Agent — no MCP tool calls.

    Used by write_story_activity (Step 3 Temporal). The flaky_tool call is a
    separate Temporal activity so Temporal can retry it independently.
    """
    from deepagents import create_deep_agent
    from deepagents.backends.filesystem import FilesystemBackend

    _check_api_key()
    recursion_limit = int(os.getenv("AGENT_RECURSION_LIMIT", "10"))
    backend = FilesystemBackend(root_dir=str(Path.cwd()))
    agent = create_deep_agent(
        model=_model_name(),
        backend=backend,
        skills=[os.getenv("SKILLS_DIR", "skills/")],
        tools=[],
        system_prompt=(
            "You are a versatile story writer. Write engaging, original stories on any "
            "topic, genre, or fictional universe the user requests. Use any available skill "
            "when it is relevant; otherwise draw on your own knowledge. "
            "Write only the story itself — no meta-commentary or explanations."
        ),
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": prompt}]},
        config={"recursion_limit": recursion_limit},
    )

    messages = result.get("messages", [])
    for msg in messages:
        is_ai = getattr(msg, "type", None) in ("ai", "assistant") or (
            isinstance(msg, dict) and msg.get("role") in ("assistant", "ai")
        )
        if not is_ai:
            continue
        raw = msg["content"] if isinstance(msg, dict) else getattr(msg, "content", "")
        content = _extract_text(raw)
        if len(content) > 100:
            return content
    return ""


async def write_story(prompt: str) -> str:
    """
    Run one agent turn: write a story AND call flaky_tool (Step 2).

    The agent is instructed to call flaky_tool after writing. If the tool
    raises, the error propagates so the caller (main.py Step 2) can return
    {"status": "failed"}.
    """
    from langchain.mcp import MCPAdapter
    from fastmcp.client.transports.stdio import PythonStdioTransport

    _check_api_key()
    mcp_script = Path(os.getenv("MCP_SERVER_ARGS", "mcp_server/flaky_server.py"))
    recursion_limit = int(os.getenv("AGENT_RECURSION_LIMIT", "10"))

    # MCP SDK only forwards a hardcoded OS env-var whitelist to subprocesses;
    # application vars like FLAKY_FAILURES must be injected explicitly.
    mcp_params = PythonStdioTransport(
        script_path=mcp_script,
        env={"FLAKY_FAILURES": os.getenv("FLAKY_FAILURES", "always")},
    )

    user_message = (
        f"{prompt}\n\n"
        "After writing the story, call flaky_tool with the complete story text."
    )

    async with MCPAdapter(mcp_params) as adapter:  # type: ignore[arg-type]
        mcp_tools = await adapter.list_tools()
        agent = _build_agent(tools=mcp_tools)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_message}]},
            config={"recursion_limit": recursion_limit},
        )

    messages = result.get("messages", [])

    # If the last tool message is an error the run has failed — raise so
    # the caller returns {"status": "failed"} rather than storing a bad story.
    for msg in reversed(messages):
        if getattr(msg, "type", None) == "tool":
            status = getattr(msg, "status", "success")
            content = str(getattr(msg, "content", ""))
            if status == "error" or "flaky_tool failed" in content:
                raise RuntimeError(content.strip())
            break

    # Return the first AI message with substantial content (the story itself).
    for msg in messages:
        is_ai = (
            getattr(msg, "type", None) in ("ai", "assistant")
            or (isinstance(msg, dict) and msg.get("role") in ("assistant", "ai"))
        )
        if not is_ai:
            continue
        raw = msg["content"] if isinstance(msg, dict) else getattr(msg, "content", "")
        content = _extract_text(raw)
        if len(content) > 100:
            return content
    return ""
