"""Opt-in Linux integration test, using an isolated Redis supplied by the caller.

Run with CONTRACT_CELERY_TEST_BROKER=redis://<isolated-redis>:6379/0.
No model service is contacted. The real task, prefork pool and Request callbacks run.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from endpoints.review import job_worker, task_store
from task_queue import review_tasks
from task_queue.celery_app import celery_app
from task_queue.review_queue import enqueue_review_job


class ProbeWorkflow:
    def __init__(self, **kwargs):
        pass

    async def run(self, *, contract_path, output_path, **kwargs):
        contract = Path(contract_path)
        if contract.name in {"timeout.docx", "lost.docx"}:
            child = subprocess.Popen(
                [sys.executable, "-m", "mcp_service.client.worker_child", str(contract.parent / "tool.py")],
                start_new_session=True,
            )
            (contract.parent / "tool.pid").write_text(str(child.pid))
            while not (contract.parent / "tool.ready").exists():
                await asyncio.sleep(0.01)
            if contract.name == "lost.docx":
                # Verify a killed writer does not leave a stale task.json lock.
                with task_store.task_write_lock(task_store.task_json_path("probe", os.environ["CONTRACT_TASK_ID"])):
                    os.kill(os.getpid(), signal.SIGKILL)
            # The hard-timeout callback must not wait for this lock before killing us.
            with task_store.task_write_lock(task_store.task_json_path("probe", os.environ["CONTRACT_TASK_ID"])):
                await asyncio.sleep(30)
        Path(output_path).write_bytes(b"completed report")
        return {"report_docx": output_path, "criteria": []}


def configure_probe() -> None:
    task_store.DATA_DIR = os.environ["CONTRACT_CELERY_TEST_DATA"]
    task_store.LOGGING_ENABLED = False
    job_worker.LOGGING_ENABLED = False
    job_worker.ContractReviewWorkflow = ProbeWorkflow
    review_tasks.QUEUE_TASK_TIME_LIMIT_SECONDS = 2
    celery_app.conf.update(
        broker_url=os.environ["CONTRACT_CELERY_TEST_BROKER"],
        task_time_limit=2,
        task_default_queue=os.environ["CONTRACT_CELERY_TEST_QUEUE"],
        worker_enable_remote_control=True,
    )


if os.getenv("CONTRACT_CELERY_TEST_DATA"):
    configure_probe()


@unittest.skipUnless(sys.platform == "linux" and os.getenv("CONTRACT_CELERY_TEST_BROKER"),
                     "Requires Linux prefork and an explicitly supplied isolated Redis")
class CeleryHardTimeoutLinuxTests(unittest.TestCase):
    def test_timeout_and_worker_loss_stop_tools_and_replace_execution_child(self):
        with TemporaryDirectory(prefix="contract-celery-") as directory:
            root = Path(directory)
            environment = {
                **os.environ,
                "CONTRACT_CELERY_TEST_DATA": str(root / "data"),
                "CONTRACT_CELERY_TEST_QUEUE": "probe-" + uuid4().hex,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            with patch.dict(os.environ, environment):
                configure_probe()
                with (root / "worker.log").open("w+") as log:
                    worker = subprocess.Popen(
                        [sys.executable, "-m", "celery", "-A", __name__ + ":celery_app", "worker",
                         "--concurrency=1", "--pool=prefork", "--loglevel=INFO", "--without-gossip",
                         "--without-mingle", "--without-heartbeat"],
                        env=environment, stdout=log, stderr=subprocess.STDOUT,
                    )
                    try:
                        ready_by = time.monotonic() + 25
                        while time.monotonic() < ready_by:
                            if worker.poll() is not None:
                                self.fail("Worker exited before ready: " + (root / "worker.log").read_text())
                            log.seek(0)
                            if " ready." in log.read():
                                break
                            time.sleep(0.1)
                        else:
                            self.fail("Worker did not become ready: " + (root / "worker.log").read_text())

                        submitted_at = time.monotonic()
                        first = self._submit(root, "20261016-120000-abcd", "timeout.docx")
                        failed = self._wait_terminal(first)
                        hard_timeout_elapsed = time.monotonic() - submitted_at
                        self.assertLess(hard_timeout_elapsed, 8, "Hard stop was delayed by the child-held task lock")
                        self.assertEqual(failed["error"]["code"], "TASK_TIME_LIMIT_EXCEEDED")
                        tool_pid = int((root / "tool.pid").read_text())
                        dead_by = time.monotonic() + 5
                        while self._alive(tool_pid) and time.monotonic() < dead_by:
                            time.sleep(0.05)
                        self.assertFalse(self._alive(tool_pid), "Tool survived its review worker")

                        second = self._submit(root, "20261016-120001-abcd", "success.docx")
                        succeeded = self._wait_terminal(second)
                        self.assertEqual(succeeded["status"], "succeeded")
                        self.assertNotEqual(failed["worker"]["worker_id"].split(":")[-2],
                                            succeeded["worker"]["worker_id"].split(":")[-2])
                        lost_id = self._submit(root, "20261016-120002-abcd", "lost.docx")
                        lost = self._wait_terminal(lost_id)
                        self.assertEqual(lost["error"]["code"], "WORKER_INTERRUPTED")
                        tool_pid = int((root / "tool.pid").read_text())
                        self.assertFalse(self._alive(tool_pid), "Tool survived worker SIGKILL")
                        replacement_id = self._submit(root, "20261016-120003-abcd", "replacement.docx")
                        replacement = self._wait_terminal(replacement_id)
                        self.assertEqual(replacement["status"], "succeeded")
                        self.assertNotEqual(lost["worker"]["worker_id"].split(":")[-2],
                                            replacement["worker"]["worker_id"].split(":")[-2])
                        print("LINUX_CELERY_PROBE " + json.dumps({
                            "timeout_code": failed["error"]["code"],
                            "worker_loss_code": lost["error"]["code"],
                            "execution_pids": [entry["worker"]["worker_id"].split(":")[-2]
                                               for entry in (failed, succeeded, lost, replacement)],
                            "replacement_statuses": [succeeded["status"], replacement["status"]],
                            "tools_stopped": True,
                            "hard_timeout_with_lock_elapsed_seconds": round(hard_timeout_elapsed, 3),
                        }))
                    finally:
                        worker.terminate()
                        try:
                            worker.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            worker.kill()
                            worker.wait(timeout=5)
                        if self._outcome and not self._outcome.success:
                            print((root / "worker.log").read_text())

    def _submit(self, root: Path, task_id: str, filename: str) -> str:
        contract = root / filename
        contract.write_bytes(b"original contract")
        (root / "tool.ready").unlink(missing_ok=True)
        (root / "tool.py").write_text(
            "from pathlib import Path\nimport time\nPath(__file__).with_name('tool.ready').touch()\ntime.sleep(60)\n"
        )
        task_store.create_task(client_id="probe", client_dir="probe", task_id=task_id,
                               contract_filename=filename, contract_path=contract,
                               criteria_source="mandatory", criteria_filename=None, criteria_path=None,
                               result_filename="result.docx", result_path=root / (task_id + ".docx"))
        enqueue_review_job("probe", task_id)
        return task_id

    def _wait_terminal(self, task_id: str) -> dict:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            task = task_store.read_task("probe", task_id)
            if task and task["status"] not in task_store.ACTIVE_STATUSES:
                return task
            time.sleep(0.05)
        self.fail("Task did not become terminal: " + str(task_store.read_task("probe", task_id)))

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False
