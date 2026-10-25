"""The MCP import phase must not write application logs onto the wire."""

import contextlib
import io
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcp_service.server import stdio_entry


class MCPStdioEntryTests(unittest.TestCase):
    def test_import_logs_go_to_stderr_but_run_keeps_stdout(self):
        stdout, stderr = io.StringIO(), io.StringIO()

        def load(name):
            self.assertEqual(name, "mcp_service.server.server")
            print("[config] loaded")
            return SimpleNamespace(mcp=SimpleNamespace(run=lambda: print('{"jsonrpc":"2.0"}')))

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch.object(stdio_entry.importlib, "import_module", side_effect=load):
            stdio_entry.main()

        self.assertEqual(stderr.getvalue(), "[config] loaded\n")
        self.assertEqual(stdout.getvalue(), '{"jsonrpc":"2.0"}\n')

    def test_import_failure_restores_stdout_and_preserves_exception(self):
        before = sys.stdout
        with patch.object(stdio_entry.importlib, "import_module", side_effect=RuntimeError("broken import")):
            with self.assertRaisesRegex(RuntimeError, "broken import"):
                stdio_entry.main()
        self.assertIs(sys.stdout, before)


if __name__ == "__main__":
    unittest.main()
