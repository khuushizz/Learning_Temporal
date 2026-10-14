"""Temporal worker — registers StoryWorkflow and its activities."""
import asyncio
import os

from dotenv import load_dotenv
from temporalio.client import Client
from temporalio.worker import Worker

from app.activities import (
    call_flaky_tool_activity,
    publish_story_activity,
    write_story_activity,
)
from app.workflows import StoryWorkflow

load_dotenv(override=True)


async def main() -> None:
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
    )
    worker = Worker(
        client,
        task_queue=os.getenv("TASK_QUEUE", "story-queue"),
        workflows=[StoryWorkflow],
        activities=[
            write_story_activity,
            call_flaky_tool_activity,
            publish_story_activity,
        ],
    )
    print(
        f"Worker started — task queue: {os.getenv('TASK_QUEUE', 'story-queue')}, "
        f"namespace: {os.getenv('TEMPORAL_NAMESPACE', 'default')}"
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
