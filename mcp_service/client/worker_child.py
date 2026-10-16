"""Trusted local MCP launcher: do not leave tools alive after a worker dies."""
import ctypes
import os
import runpy
import signal
import sys


def main():
    target = sys.argv[1]
    if sys.platform == "linux":
        parent = os.getppid()
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL) != 0:
            raise OSError(ctypes.get_errno(), "Cannot configure MCP parent-death signal")
        if parent == 1 or os.getppid() != parent:
            raise SystemExit("Review worker exited before MCP launch")
    if target.endswith(".js"):
        os.execlp("node", "node", target)
    elif target.startswith("module:"):
        sys.argv = [target.removeprefix("module:")]
        runpy.run_module(sys.argv[0], run_name="__main__")
    else:
        sys.argv = [target]
        runpy.run_path(target, run_name="__main__")


if __name__ == "__main__":
    main()
