"""Report real workflow wall time, index substages and source provenance."""

import csv
import hashlib
import json
import re
import statistics
import subprocess

from tests.review_timing.run import OUTPUT, ROOT


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def report():
    rows = []
    provenance = {}
    for variant in ("baseline", "latest"):
        source = ROOT / "tests/review_timing/sources" / variant
        provenance[variant] = {
            "commit": subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip(),
            "config_sha256": sha(source / "config.yaml"),
            "env_sha256": sha(source / ".env"),
            "source": str(source),
        }
        for number in range(1, 6):
            cid = f"D{number:02}"
            record_path = OUTPUT / variant / f"{cid}.json"
            if not record_path.exists():
                continue
            record = json.loads(record_path.read_text(encoding="utf-8"))
            row = {"contract": cid, "variant": variant, "task_id": record["task_id"],
                   "status": record["status"]}
            directory = ROOT / "data/api/review_timing" / variant / "api" / record["task_id"]
            trace_file = directory / "logs/trace.json"
            if trace_file.exists():
                trace = json.loads(trace_file.read_text(encoding="utf-8"))
                for name, field in (("workflow.run", "workflow_seconds"), ("phase.build_index", "index_seconds")):
                    matches = [r for r in trace["runs"] if r["name"] == name]
                    row[field] = matches[-1]["duration_seconds"] if matches else None
                text = (directory / "logs/events.log").read_text(encoding="utf-8")
                for operation in ("build_document_index", "build_vector_index"):
                    values = re.findall(rf"version_timing variant={variant} operation={operation} seconds=([\d.]+)", text)
                    if len(values) != 1:
                        raise ValueError(f"Expected one verified substage marker: {trace_file} {operation}")
                    row[operation + "_seconds"] = float(values[0])
                row["trace"] = str(trace_file)
                row["output_docx"] = record.get("output_docx", "")
                original = ROOT / f"tests/criteria_formal_recall/cases/{cid}.docx"
                if sha(directory / "input" / f"{cid}.docx") != sha(original):
                    raise ValueError(f"Input mismatch {cid} {variant}")
            rows.append(row)
    provenance["contracts"] = {f"D{i:02}": sha(ROOT / f"tests/criteria_formal_recall/cases/D{i:02}.docx") for i in range(1, 6)}
    provenance["criteria_sha256"] = sha(ROOT / "resources/criteria/criteria-formal.docx")
    provenance["infrastructure"] = "Real app/workflow/MCP; isolated ports 5111/5112; in-process API dispatch; unchanged model limits, same Redis DB 2 via host port 6380"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (OUTPUT / "timing.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    for variant in ("baseline", "latest"):
        group = [row for row in rows if row["variant"] == variant and row["status"] == "succeeded"]
        print(variant, "completed", len(group))
        if len(group) == 5:
            print({field: round(statistics.mean(row[field] for row in group), 3) for field in (
                "workflow_seconds", "index_seconds", "build_document_index_seconds", "build_vector_index_seconds")})


if __name__ == "__main__":
    report()
