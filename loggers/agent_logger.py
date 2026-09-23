"""
Agent conversation logging to task-scoped JSON files.
"""
import json
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from config import LOGGING_ENABLED


_CONVERSATION_LOG_DIR: ContextVar[Path | None] = ContextVar(
    "conversation_log_dir",
    default=None,
)


def set_conversation_log_dir(path: str | Path | None):
    return _CONVERSATION_LOG_DIR.set(Path(path) if path else None)


def reset_conversation_log_dir(token) -> None:
    _CONVERSATION_LOG_DIR.reset(token)


def agent_call_log_dir() -> Path | None:
    directory = _CONVERSATION_LOG_DIR.get()
    return directory.parent / "calls" if directory else None


def _serialize_messages(messages: list) -> list:
    serializable = []
    for msg in messages:
        try:
            role = msg.get("role")
            out = {"role": role}

            if "content" in msg:
                content = msg["content"]
                out["content"] = content if (content is None or isinstance(content, str)) else str(content)

            if role == "assistant" and "tool_calls" in msg:
                tool_calls = msg["tool_calls"] or []
                out_tcs = []
                for tc in tool_calls:
                    if hasattr(tc, "model_dump"):
                        tc = tc.model_dump()
                    elif hasattr(tc, "to_dict"):
                        tc = tc.to_dict()
                    out_tcs.append({
                        "id": tc.get("id"),
                        "type": tc.get("type", "function"),
                        "function": {
                            "name": (tc.get("function") or {}).get("name"),
                            "arguments": (tc.get("function") or {}).get("arguments"),
                        },
                    })
                out["tool_calls"] = out_tcs

            if role == "tool":
                out["tool_call_id"] = msg.get("tool_call_id")

            serializable.append(out)
        except Exception as exc:
            print(f"Error processing message: {exc}")

    return serializable


def log_conversation(agent_name: str, messages: list) -> Path | None:
    if not LOGGING_ENABLED:
        return None

    log_dir = _CONVERSATION_LOG_DIR.get()
    if log_dir is None:
        print("Conversation log directory is not set; skipping agent conversation log.")
        return None
    if log_dir.name == "conversations" and log_dir.parent.name == "logs":
        return None  # Task calls contain the full request and response.

    log_dir.mkdir(parents=True, exist_ok=True)
    serializable = _serialize_messages(messages)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filepath = log_dir / f"{agent_name}_{ts}.json"

    try:
        filepath.write_text(
            json.dumps(serializable, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return filepath
    except Exception as exc:
        print(f"Error writing conversation log: {exc}")
        return None


def log_agent_step(agent_name: str, step: dict) -> None:
    if not LOGGING_ENABLED:
        return

    log_dir = _CONVERSATION_LOG_DIR.get()
    if log_dir is None:
        print("Conversation log directory is not set; skipping agent step log.")
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "agent_name": agent_name,
        **step,
    }
    filepath = log_dir / "agent_steps.jsonl"

    try:
        with filepath.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"Error writing agent step log: {exc}")
