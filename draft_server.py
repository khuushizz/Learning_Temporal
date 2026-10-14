import json
import os
import sys
from pathlib import Path

from fastmcp import FastMCP

# Paths anchored to project root (parent of this file's directory)
_PROJECT_ROOT = Path(__file__).parent.parent
STATE_FILE = Path(__file__).parent / ".draft_state.json"
DRAFTS_DIR = _PROJECT_ROOT / "drafts"

mcp = FastMCP("draft-server")


def _read_call_count() -> int:
    try:
        return json.loads(STATE_FILE.read_text()).get("call_count", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def _write_call_count(n: int) -> None:
    STATE_FILE.write_text(json.dumps({"call_count": n}))


@mcp.tool()
def save_draft(story_id: str, story: str) -> str:
    """Save the story text to a local draft file. Returns the file path on success."""
    flaky_failures = int(os.getenv("FLAKY_FAILURES", "2"))
    call_count = _read_call_count() + 1
    _write_call_count(call_count)

    print(
        f"save_draft call {call_count}"
        + (f" (will fail: {call_count} <= {flaky_failures})" if flaky_failures > 0 and call_count <= flaky_failures else " (will succeed)"),
        file=sys.stderr,
    )

    if flaky_failures > 0 and call_count <= flaky_failures:
        raise RuntimeError(
            f"save_draft failed: simulated storage outage (call {call_count} of {flaky_failures})"
        )

    DRAFTS_DIR.mkdir(exist_ok=True)
    draft_path = DRAFTS_DIR / f"{story_id}.md"
    draft_path.write_text(story, encoding="utf-8")
    print(f"Draft saved: {draft_path}", file=sys.stderr)
    return str(draft_path)


if __name__ == "__main__":
    mcp.run(transport="stdio")
