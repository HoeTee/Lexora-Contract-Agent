"""Build the learning page from allowlisted source snapshots, without loading the app."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "code-learning"


def source_path(relative: str) -> Path:
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT) or path.name in {".env", "config.yaml", "VMProtectLicense.ini"}:
        raise ValueError(f"Source is outside the permitted documentation scope: {relative}")
    if path.suffix not in {".py", ".js", ".yaml", ".ps1", ".cpp", ".c", ".h"} and path.name != "Dockerfile":
        raise ValueError(f"Unsupported source type: {relative}")
    return path


def resolve_reference(reference: dict, text: str) -> dict:
    lines = text.splitlines()
    if "symbol" in reference:
        nodes = ast.parse(text).body
        node = None
        for part in reference["symbol"].split("."):
            node = next((item for item in nodes if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == part), None)
            if node is None:
                raise ValueError(f"Missing symbol: {reference}")
            nodes = node.body
        start = min([node.lineno] + [item.lineno for item in getattr(node, "decorator_list", [])])
        end = node.end_lineno
    else:
        start, end = reference.get("lines", [1, len(lines)])
    if not 1 <= start <= end <= len(lines):
        raise ValueError(f"Invalid source range: {reference}")
    return {**reference, "start": start, "end": end, "code": "\n".join(lines[start - 1:end])}


def build_snapshot() -> dict:
    catalog = json.loads((GUIDE / "catalog.json").read_text(encoding="utf-8"))
    files = {}
    identifiers = {item["id"] for item in catalog["components"]}
    if len(identifiers) != len(catalog["components"]):
        raise ValueError("Component IDs must be unique.")
    for component in catalog["components"]:
        if not set(component.get("related", [])) <= identifiers:
            raise ValueError(f"Unknown related component: {component['id']}")
        references = []
        for reference in component["refs"]:
            relative = reference["file"]
            if relative not in files:
                path = source_path(relative)
                content = path.read_text(encoding="utf-8-sig")
                files[relative] = {"content": content, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "absolute": path.as_posix()}
            references.append(resolve_reference(reference, files[relative]["content"]))
        component["refs"] = references
    for step in catalog["journey"]:
        if step["component"] not in identifiers:
            raise ValueError(f"Unknown journey component: {step}")
    head = subprocess.check_output(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, text=True).strip()
    return {**catalog, "files": files, "root": ROOT.as_posix(), "commit": head}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--renderer", type=Path, default=Path.home() / ".agents/skills/answer-me-with-html/scripts/am.mjs")
    parser.add_argument("--check", action="store_true", help="Validate source references without writing the page.")
    args = parser.parse_args()
    snapshot = build_snapshot()
    counts = {"components": len(snapshot["components"]), "source_files": len(snapshot["files"]), "snippets": sum(len(item["refs"]) for item in snapshot["components"])}
    if args.check:
        print(json.dumps({"verified": True, **counts}))
        return
    if not args.renderer.is_file():
        raise SystemExit("Pass --renderer with the path to answer-me-with-html/scripts/am.mjs.")
    data = json.dumps(snapshot, ensure_ascii=False).replace("<", "\\u003c")
    explorer = (GUIDE / "explorer.html").read_text(encoding="utf-8").replace("__LEARNING_DATA__", data)
    draft = f"""---
template: doc
theme: shadcn
mode: light
lang: zh
style: off
title: Contract Agent 核心代码实验室
subtitle: 从一次合同上传，读懂 Agent、检索、模型请求与并发控制。
source: 源码快照 {snapshot['commit']} · {counts['components']} 个组件 · {counts['snippets']} 段真实代码
---
这是一份可离线打开的学习页面。代码、路径与行号来自当前仓库快照。
页面中的演示不会执行审查，也不会向模型服务发送请求。

## A 先看一次任务经过哪里
```flow LR
Web或CLI -> HTTP服务: 提交合同
HTTP服务 -> Celery队列: 投递任务ID
Celery队列 -> worker: 领取任务
worker -> Workflow: 执行各阶段
Workflow -> Agent与MCP: 检索和审查
Agent与MCP -> 模型控制器: 统一模型请求
Workflow -> 批注DOCX: 汇总和交付
```

## B 点击组件，直接阅读源码
```html
{explorer}
```

## C 如何使用这份快照
代码区可切换核心片段与完整文件。行号对应生成时的仓库文件。
复制路径后，可在编辑器中定位对应文件和行号。
源码发生变化后，需要重新生成页面。页面不会自动读取本机文件。
配置值来自示例配置和代码默认值，不代表内网运行时配置。
本页依据源码静态追踪，不代表已经执行真实模型审查。
"""
    with tempfile.TemporaryDirectory(prefix="contract-code-learning-") as directory:
        draft_path = Path(directory) / "guide.md"
        draft_path.write_text(draft, encoding="utf-8")
        subprocess.run(["node", str(args.renderer), "render", str(draft_path), "-o", str(GUIDE / "index.html"), "--no-open", "--style", "off"], cwd=ROOT, check=True)
    print(json.dumps({"generated": str(GUIDE / "index.html"), **counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
