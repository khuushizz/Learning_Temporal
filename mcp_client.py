"""
MCP client helper — connects to flaky_server via stdio and calls flaky_tool.

Used by:
  - app/activities.py (call_flaky_tool_activity, Step 3)
  - tests/test_step2.py (direct tool tests)
"""

import os
import sys
from pathlib import Path
from typing import Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _server_params(flaky_failures: Optional[str] = None) -> StdioServerParameters:
    """Build StdioServerParameters for the flaky_server MCP process."""
    failures = flaky_failures if flaky_failures is not None else os.getenv("FLAKY_FAILURES", "always")
    mcp_script = Path(os.getenv("MCP_SERVER_ARGS", "mcp_server/flaky_server.py"))
    return StdioServerParameters(
        command=sys.executable,
        args=[str(mcp_script)],
        # The MCP SDK only whitelists a small set of OS env vars for the
        # subprocess. Inject application vars explicitly so they are forwarded.
        env={"FLAKY_FAILURES": failures},
    )


async def list_tools(flaky_failures: Optional[str] = None) -> list:
    """Return the list of Tool objects the MCP server exposes."""
    params = _server_params(flaky_failures)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
    return result.tools


async def call_flaky_tool(story: str, flaky_failures: Optional[str] = None) -> str:
    """
    Call flaky_tool and return 'ok' on success.

    Raises RuntimeError on failure so callers (and Temporal's retry policy)
    can treat the exception as a retriable error.
    """
    params = _server_params(flaky_failures)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("flaky_tool", {"story": story})

    # mcp v2.x uses snake_case: is_error (not isError)
    if getattr(result, "is_error", False):
        texts = [c.text for c in result.content if hasattr(c, "text")]
        raise RuntimeError(" ".join(texts) or "flaky_tool failed")

    texts = [c.text for c in result.content if hasattr(c, "text")]
    return " ".join(texts) or "ok"
