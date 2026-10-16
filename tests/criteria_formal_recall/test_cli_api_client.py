from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from main import DOCX_MEDIA_TYPE, run_cli


class BrokenDownload(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"partial report"
        raise httpx.ReadError("download connection interrupted")


class CliApiClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.contract = self.root / "contract.docx"
        self.contract.write_bytes(b"uploaded contract")
        self.criteria = self.root / "criteria.docx"
        self.criteria.write_bytes(b"uploaded criteria")
        self.output = self.root / "output.docx"
        self.environment = patch.dict(os.environ, {"API_KEY": "", "REVIEW_API_URL": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def test_upload_poll_and_download_only_after_success(self):
        requests = []
        statuses = iter(["queued", "running", "succeeded"])

        async def handle(request):
            requests.append(request)
            self.assertEqual(request.headers["authorization"], "Bearer platform-key")
            if request.url.path == "/api/review/jobs":
                body = await request.aread()
                for value in (b"uploaded contract", b"uploaded criteria", b"templateCode", b"template-a", b"serialNo", b"serial-a"):
                    self.assertIn(value, body)
                return httpx.Response(202, json={"task_id": "task-a", "status": "queued"})
            if request.url.path.endswith("/result"):
                self.assertEqual(json.loads(request.content), {"output_type": "file"})
                return httpx.Response(200, headers={"content-type": DOCX_MEDIA_TYPE}, content=b"successful report")
            return httpx.Response(200, json={"task_id": "task-a", "status": next(statuses)})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await run_cli(str(self.contract), criteria=str(self.criteria), output=str(self.output),
                                   base_url="http://service", api_key="platform-key", template_code="template-a",
                                   serial_no="serial-a", poll_interval_seconds=0, client=client)
            self.assertFalse(client.is_closed)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["final_report_docx"], str(self.output))
        self.assertEqual(self.output.read_bytes(), b"successful report")
        self.assertEqual([request.method for request in requests], ["POST", "GET", "GET", "GET", "POST"])

    async def test_failed_task_does_not_download_original_contract_as_report(self):
        requests = []
        self.output.write_bytes(b"existing report")

        def handle(request):
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(202, json={"task_id": "task-a", "status": "queued"})
            return httpx.Response(200, json={"status": "failed", "error": {"code": "TASK_TIME_LIMIT_EXCEEDED"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaisesRegex(RuntimeError, "TASK_TIME_LIMIT_EXCEEDED"):
                await run_cli(str(self.contract), output=str(self.output), base_url="http://service",
                              api_key="platform-key", client=client)
        self.assertEqual(self.output.read_bytes(), b"existing report")
        self.assertEqual(len(requests), 2)

    async def test_authentication_error_stops_before_polling(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(401, json={"detail": "Invalid platform key"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await run_cli(str(self.contract), output=str(self.output), base_url="http://service",
                              api_key="bad-key", client=client)
        self.assertEqual(len(requests), 1)
        self.assertFalse(self.output.exists())

    async def test_client_timeout_does_not_cancel_server_task(self):
        requests = []

        async def handle(request):
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(202, json={"task_id": "task-a", "status": "queued"})
            await asyncio.sleep(1)
            return httpx.Response(200, json={"status": "running"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaisesRegex(TimeoutError, "task-a.*continues running"):
                await run_cli(str(self.contract), output=str(self.output), base_url="http://service",
                              api_key="platform-key", wait_timeout_seconds=0.03, client=client)
        self.assertFalse(any(request.url.path.endswith("/cancel") for request in requests))
        self.assertFalse(self.output.exists())

    async def test_interrupted_download_preserves_existing_output(self):
        self.output.write_bytes(b"existing report")

        def handle(request):
            if request.url.path == "/api/review/jobs":
                return httpx.Response(202, json={"task_id": "task-a"})
            if request.url.path.endswith("/result"):
                return httpx.Response(200, headers={"content-type": DOCX_MEDIA_TYPE}, stream=BrokenDownload())
            return httpx.Response(200, json={"status": "succeeded"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(httpx.ReadError):
                await run_cli(str(self.contract), output=str(self.output), base_url="http://service",
                              api_key="platform-key", client=client)
        self.assertEqual(self.output.read_bytes(), b"existing report")
        self.assertEqual(list(self.root.glob(".review-download-*")), [])

    async def test_environment_api_key_takes_precedence_and_base_url_supports_environment(self):
        def handle(request):
            self.assertEqual(request.url.host, "environment-service")
            self.assertEqual(request.headers["authorization"], "Bearer environment-key")
            return httpx.Response(403, json={"detail": "Test completed before submission"})

        with patch.dict(os.environ, {"API_KEY": "environment-key", "REVIEW_API_URL": "http://environment-service"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with self.assertRaises(httpx.HTTPStatusError):
                    await run_cli(str(self.contract), api_key="argument-key", client=client)
