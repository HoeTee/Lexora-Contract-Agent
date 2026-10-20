from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from loggers.call_log import call_stage, map_with_context
from pathlib import Path
from typing import Any

from .client import EMBEDDING_BATCH_SIZE, EmbeddingClient
from .schema import VectorIndex, VectorItem, VectorMetadata
from tools.docxindex.model_input import embedding_chunks, model_tokenizer


def build_vector_index(
    index: dict[str, Any],
    client: EmbeddingClient,
    source_index: str = "document_index.json",
    concurrency: int = 10,
) -> VectorIndex:
    max_input_tokens = int(getattr(client.settings, "max_input_tokens", 8000))
    overlap = int(getattr(client.settings, "chunk_overlap_tokens", 32))
    tokenizer = model_tokenizer(client.settings.model)
    entries = []
    for entry in _select_vector_entries(index):
        chunks = embedding_chunks(entry["vector_text"], client.settings.model, max_input_tokens, overlap)
        for number, chunk in enumerate(chunks):
            entries.append({**entry, "vector_text": chunk.text, "chunk": chunk,
                            "chunk_index": number, "chunk_count": len(chunks)})
    texts = [entry["vector_text"] for entry in entries]
    batches = [texts[start : start + EMBEDDING_BATCH_SIZE] for start in range(0, len(texts), EMBEDDING_BATCH_SIZE)]
    def embed(entry):
        number, batch = entry
        with call_stage(f"index-embedding-batch{number}"):
            return client.embed_batch(batch)

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        embedded_batches = map_with_context(executor, embed, list(enumerate(batches, 1)))
    embeddings = [embedding for batch in embedded_batches for embedding in batch]
    items = []
    for entry, embedding in zip(entries, embeddings, strict=True):
        node = entry["node"]
        items.append(
            VectorItem(
                vector_id=(f"{node['node_id']}::chunk{entry['chunk_index'] + 1:04d}"
                           if entry["chunk_count"] > 1 else node["node_id"]),
                node_id=node["node_id"],
                text_hash=_text_hash(entry["vector_text"]),
                text=entry["vector_text"],
                embedding=embedding,
                chunk_index=entry["chunk_index"],
                source_start=entry["chunk"].start,
                source_end=entry["chunk"].end,
                metadata=VectorMetadata(
                    title=node.get("title") or "",
                    node_type=node.get("node_type") or "",
                    start_index=node.get("start_index"),
                    end_index=node.get("end_index"),
                    start_anchor=node.get("start_anchor"),
                    end_anchor=node.get("end_anchor"),
                    token_estimate=int(node.get("token_estimate") or 0),
                ),
            )
        )
    return VectorIndex(source_index=source_index, embedding_model=client.settings.model, items=items,
                       format_version=2, configured_input_tokens=max_input_tokens,
                       effective_input_tokens=tokenizer.effective_limit(max_input_tokens),
                       chunk_overlap_tokens=overlap, tokenizer=tokenizer.identity)


def save_vector_index(path: Path, index: VectorIndex) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")


def load_vector_index(path: Path) -> VectorIndex:
    return VectorIndex.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _select_vector_entries(index: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for node in index.get("nodes") or []:
        if not _should_index_node(node):
            continue
        vector_text = _vector_text(node)
        if vector_text:
            result.append({"node": node, "vector_text": vector_text})
    return result


def _should_index_node(node: dict[str, Any]) -> bool:
    node_type = node.get("node_type")
    text = (node.get("text") or "").strip()
    children = node.get("nodes") or node.get("children") or []
    if node_type in {"body", "attachments"}:
        return False
    if node_type == "table":
        return bool(text)
    if children and not text:
        return False
    return bool(text)


def _vector_text(node: dict[str, Any]) -> str:
    parts = [
        f"标题：{node.get('title') or ''}",
        f"摘要：{node.get('summary') or ''}",
        f"正文：{node.get('text') or ''}",
    ]
    return "\n".join(part for part in parts if part.strip()).strip()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
