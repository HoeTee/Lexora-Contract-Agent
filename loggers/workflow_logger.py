"""
Task-scoped workflow logging.
"""
import json
import time
from datetime import datetime
from pathlib import Path

from config import LOGGING_ENABLED, LOGGING_DETAILED, MAX_CONTEXT_TOKENS
from loggers.api_event_logger import append_api_event


class WorkflowLogger:
    """Records workflow steps and generates a Markdown log with Mermaid."""

    SUMMARY_ACTIONS = {
        "mcp_connect",
        "ingest_file(criteria)",
        "ingest_file(contract)",
        "contract_build_index",
        "design_tasks",
        "execute_criteria_complete",
        "compile_summary_comment",
        "generate_docx_report",
        "mcp_cleanup",
    }

    def __init__(self, log_dir: str | Path | None = None, *, events_path: str | Path | None = None):
        self.steps = []
        self.start_time = time.time()
        self.log_dir = Path(log_dir) if log_dir else None
        self.events_path = Path(events_path) if events_path else None

    def log(
        self,
        phase: str,
        sender: str,
        receiver: str,
        action: str,
        input_summary: str = "",
        output_summary: str = "",
        tokens: int = 0,
        duration: float = 0.0,
    ) -> None:
        if not LOGGING_ENABLED:
            return
        if self.events_path and action in self.SUMMARY_ACTIONS:
            append_api_event(self.events_path, "phase_completed", phase=phase,
                             action=action, duration_seconds=round(duration, 2))
        if not LOGGING_DETAILED:
            input_summary = output_summary = ""
            tokens = 0

        ctx_pct = ""
        if tokens > 0 and MAX_CONTEXT_TOKENS > 0:
            pct = int((tokens / MAX_CONTEXT_TOKENS) * 100)
            ctx_pct = f" ({pct}%)"

        self.steps.append({
            "step": len(self.steps) + 1,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "phase": phase,
            "sender": sender,
            "receiver": receiver,
            "action": action,
            "input": input_summary,
            "output": output_summary,
            "tokens": f"{tokens}{ctx_pct}" if tokens else "-",
            "raw_tokens": tokens,
            "duration": round(duration, 2),
        })

    def summarize_phase_durations(self) -> list[dict]:
        """Summarize main workflow phase durations from logged steps."""
        phase_totals: dict[str, float] = {}
        for step in self.steps:
            if step.get("action") not in self.SUMMARY_ACTIONS:
                continue
            phase = step.get("phase", "")
            if not phase:
                continue
            phase_totals[phase] = round(
                phase_totals.get(phase, 0.0) + float(step.get("duration", 0.0)),
                2,
            )

        return [
            {"phase": phase, "duration_seconds": duration}
            for phase, duration in phase_totals.items()
        ]

    def total_logged_duration(self) -> float:
        """Return the sum of main workflow phase durations."""
        return round(
            sum(item["duration_seconds"] for item in self.summarize_phase_durations()),
            2,
        )

    def _build_mermaid(self) -> str:
        def sanitize_id(name: str) -> str:
            return name.replace(" ", "_").replace(":", "_").replace("-", "_")

        seen = {}
        for step in self.steps:
            for name in [step["sender"], step["receiver"]]:
                if name not in seen:
                    seen[name] = len(seen)

        lines = ["```mermaid", "sequenceDiagram"]
        for name in seen:
            alias = sanitize_id(name)
            lines.append(f"    participant {alias} as \"{name}\"")

        for step in self.steps:
            src = sanitize_id(step["sender"])
            dst = sanitize_id(step["receiver"])
            label = step["action"]
            if step["duration"]:
                label += f" ({step['duration']}s)"
            lines.append(f"    {src}->>{dst}: {label}")

        lines.append("```")
        return "\n".join(lines)

    def _build_steps_md(self) -> str:
        blocks = []
        for step in self.steps:
            input_text = f"\n```text\n{step['input']}\n```\n" if step["input"] else "-"
            output_text = f"\n```text\n{step['output']}\n```\n" if step["output"] else "-"

            blocks.append(f"""### Step {step['step']} - {step['action']}

- **Time**: {step['timestamp']}
- **Phase**: {step['phase']}
- **Sender**: {step['sender']}
- **Receiver**: {step['receiver']}
- **Action**: {step['action']}
- **Input**: {input_text}
- **Output**: {output_text}
- **Tokens**: {step['tokens']}
- **Duration**: {step['duration']}s
""")
        return "\n".join(blocks)

    def save(self) -> str | None:
        if not LOGGING_ENABLED or not LOGGING_DETAILED:
            return None
        if self.log_dir is None:
            raise ValueError("WorkflowLogger.log_dir is required when file logging is enabled.")

        self.log_dir.mkdir(parents=True, exist_ok=True)

        total_time = round(time.time() - self.start_time, 2)
        total_tokens = sum(step.get("raw_tokens", 0) for step in self.steps)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = self.log_dir / f"workflow_{ts}.md"

        content = f"""# Workflow Log - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

**Total Steps**: {len(self.steps)} | **Total Time**: {total_time}s | **Total Tokens**: {total_tokens}

---

## Flow Diagram

{self._build_mermaid()}

---

## Detailed Steps

{self._build_steps_md()}
"""
        filepath.write_text(content, encoding="utf-8")
        print(f"[Logger] Workflow log saved: {filepath}")
        return str(filepath)


def save_review_outputs_json(results: list[dict], path: str | Path) -> str | None:
    if not LOGGING_ENABLED or not LOGGING_DETAILED:
        return None
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)
