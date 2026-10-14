import json
import os
import sys
from pathlib import Path

from fastmcp import FastMCP

STATE_FILE = Path(__file__).parent / ".flaky_state.json"

mcp = FastMCP("flaky-server")


def _read_call_count() -> int:
    try:
        return json.loads(STATE_FILE.read_text()).get("call_count", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def _write_call_count(n: int) -> None:
    STATE_FILE.write_text(json.dumps({"call_count": n}))


@mcp.tool()
def flaky_tool(story: str) -> str:
    """Submit the story draft for downstream processing. May fail with a simulated outage."""
    flaky_failures = os.getenv("FLAKY_FAILURES", "always")

    if flaky_failures == "always":
        # Default: every call raises — simulates a permanently broken downstream service.
        print("flaky_tool called (always-fail mode)", file=sys.stderr)
        raise RuntimeError("flaky_tool failed: simulated downstream outage")

    # Integer mode: raise for the first N calls, then succeed.
    try:
        n = int(flaky_failures)
    except ValueError:
        n = 0  # treat unrecognised value as "never fail"

    call_count = _read_call_count() + 1
    _write_call_count(call_count)
    print(f"flaky_tool call {call_count} (FLAKY_FAILURES={n})", file=sys.stderr)

    if n > 0 and call_count <= n:
        raise RuntimeError(
            f"flaky_tool failed: simulated downstream outage (call {call_count} of {n})"
        )

    print(f"flaky_tool succeeded on call {call_count}", file=sys.stderr)
    return "ok"


if __name__ == "__main__":
    mcp.run(transport="stdio")
