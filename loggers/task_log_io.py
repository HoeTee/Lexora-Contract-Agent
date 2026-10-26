"""Small, task-local file operations shared by logging processes."""

from __future__ import annotations

import errno
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.01
_NUMBERED_CALL = re.compile(r"^([1-9][0-9]*)__.*\.json$")
_COUNTER_NAME = ".logical_call_counter"


@contextmanager
def task_log_lock(path: Path) -> Iterator[None]:
    """Lock a stable sidecar inode; the OS releases it if a writer exits."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    try:
        while True:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except InterruptedError:
                continue
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for the task log write lock") from None
                time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        os.close(fd)
        # Do not unlink: that would let simultaneous writers lock different inodes.


def allocate_logical_no(logs_dir: Path) -> int:
    """Reserve a unique natural number in this task, including after restart."""
    logs_dir = Path(logs_dir)
    counter = logs_dir / _COUNTER_NAME
    with task_log_lock(counter):
        try:
            text = counter.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            previous = 0
        else:
            if not re.fullmatch(r"0|[1-9][0-9]*", text):
                raise ValueError("Invalid task logical-call counter; refusing to reuse a number")
            previous = int(text)
        for candidate in (logs_dir / "logical_calls").glob("*.json"):
            match = _NUMBERED_CALL.fullmatch(candidate.name)
            if match:
                previous = max(previous, int(match.group(1)))
        number = previous + 1
        temporary = counter.with_name(counter.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="ascii", newline="\n") as handle:
                handle.write(str(number) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, counter)
        finally:
            temporary.unlink(missing_ok=True)
        return number


def append_task_log_line(path: Path, line: str) -> None:
    """Append one complete UTF-8 line while other task processes also write."""
    path = Path(path)
    if "\n" in line or "\r" in line:
        raise ValueError("A task event must contain exactly one line")
    with task_log_lock(path):
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
