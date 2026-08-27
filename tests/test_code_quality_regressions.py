from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from endpoints.review import job_worker
from tools.docxindex.retrieval.content import build_content_context


class ContentChunkRegressionTests(unittest.TestCase):
    def test_oversized_node_chunks_keep_explicit_anchor_boundaries(self) -> None:
        anchor_map = {
            f"paragraph:{index}": {
                "body_child_index": index,
                "text": "测试内容" * 12,
            }
            for index in range(3)
        }
        index = {
            "nodes": [
                {
                    "node_id": "body/sec_001",
                    "title": "测试章节",
                    "node_type": "section",
                    "text": "测试内容" * 36,
                    "start_index": 0,
                    "end_index": 2,
                    "start_anchor": "paragraph:0",
                    "end_anchor": "paragraph:2",
                }
            ],
            "anchor_map": anchor_map,
        }

        pages = [
            build_content_context(
                index,
                [{"node_id": "body/sec_001", "source": "title"}],
                input_tokens=10,
                part=part,
            )
            for part in range(1, 4)
        ]

        chunks = [page["content_context"][0] for page in pages]
        self.assertEqual(
            [(chunk["start_anchor"], chunk["end_anchor"]) for chunk in chunks],
            [("paragraph:0", "paragraph:0"), ("paragraph:1", "paragraph:1"), ("paragraph:2", "paragraph:2")],
        )
        self.assertTrue(pages[0]["pagination"]["has_more"])
        self.assertFalse(pages[-1]["pagination"]["has_more"])


class HeartbeatLoggingRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_failure_is_recorded_before_next_cycle(self) -> None:
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])
        with (
            patch.object(job_worker.asyncio, "sleep", sleep),
            patch.object(job_worker, "mark_worker_heartbeat", side_effect=OSError("disk unavailable")),
            patch.object(job_worker, "write_task_log_event") as write_event,
            patch.object(job_worker.logger, "exception"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await job_worker.heartbeat_worker("client-a", "task-a", "worker-a")

        write_event.assert_called_once_with(
            "client-a",
            "task-a",
            "worker_heartbeat_failed",
            worker_id="worker-a",
            error="OSError('disk unavailable')",
        )


class CompatibilityImportTests(unittest.TestCase):
    def test_legacy_modules_reference_canonical_implementations(self) -> None:
        from agents import agent_logger as legacy_logger
        from loggers import agent_logger as canonical_logger
        from tools.document import table_markdown_map as legacy_mapping
        from tools.docxindex.table_mapping import markdown_map as canonical_mapping

        self.assertIs(legacy_logger.log_conversation, canonical_logger.log_conversation)
        self.assertIs(legacy_mapping.render_table_markdown, canonical_mapping.render_table_markdown)


if __name__ == "__main__":
    unittest.main()
