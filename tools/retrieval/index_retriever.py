from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime

from tools.retrieval.llamaindex import build_llamaindex_env_error, load_llamaindex_rag
from tools.document.file_parser import FileParser


class IndexRetriever:
    """Facade over temporary contract retrieval and optional institutional indexes."""

    def __init__(
        self,
        *,
        project_root: str,
        llm_api_key: str | None,
        llm_base_url: str | None,
        llm_name: str,
        llm_enable_thinking: bool | None,
        embed_api_key: str | None,
        embed_base_url: str | None,
        embed_name: str,
        rerank_api_key: str | None,
        rerank_base_url: str | None,
        rerank_name: str | None,
        rerank_provider: str = "higress_qwen",
        chunk_size: int = 512,
        chunk_overlap: int = 200,
        similarity_top_k: int = 5,
        rerank_top_n: int = 3,
    ) -> None:
        self.project_root = project_root
        self.llm_api_key = llm_api_key
        self.llm_base_url = llm_base_url
        self.llm_name = llm_name
        self.llm_enable_thinking = llm_enable_thinking
        self.embed_api_key = embed_api_key
        self.embed_base_url = embed_base_url
        self.embed_name = embed_name
        self.rerank_api_key = rerank_api_key
        self.rerank_base_url = rerank_base_url
        self.rerank_name = rerank_name
        self.rerank_provider = rerank_provider
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.similarity_top_k = similarity_top_k
        self.rerank_top_n = rerank_top_n
        self._contract_llamaindex_engine = None
        self._institutional_engine = None
        institutional_docs_dir = os.getenv("INSTITUTIONAL_DOCS_DIR")
        if institutional_docs_dir is None:
            institutional_username = os.getenv("INSTITUTIONAL_DOCS_USERNAME", "default")
            institutional_docs_dir = os.path.join(
                "data",
                institutional_username,
                "institutional_docs",
            )
        self.institutional_docs_dir = os.path.join(
            self.project_root,
            institutional_docs_dir,
        )
        self.institutional_index_dir = os.path.join(
            self.project_root,
            "RAG_persist",
            "institutional_index",
        )
        self.institutional_manifest_path = os.path.join(
            self.project_root,
            "RAG_persist",
            "institutional_manifest.json",
        )

    async def build_contract_llamaindex_index(self, docx_path: str) -> str:
        """Build a temporary in-memory LlamaIndex contract index from DOCX anchors."""
        try:
            if os.path.splitext(docx_path)[1].lower() != ".docx":
                raise ValueError("Only .docx contract files are supported for review indexing.")
            engine = self._get_contract_llamaindex_engine()
            node_count = await asyncio.to_thread(
                engine.build_temporary_index_from_docx,
                docx_path,
                "current_contract",
            )
            return json.dumps(
                {"status": "built_temporary_index", "num_nodes": node_count},
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    async def search_contract_llamaindex(self, query: str) -> str:
        """Search the temporary in-memory contract LlamaIndex."""
        try:
            engine = self._get_contract_llamaindex_engine()
            return await asyncio.to_thread(engine.search, query, "合同检索结果")
        except Exception as e:
            return f"Error in contract LlamaIndex search: {str(e)}"

    async def build_institutional_index(self) -> str:
        """Incrementally update the persistent institutional-document index."""
        print("[institutional-index] Loading index state...")
        engine = self._get_institutional_engine()
        engine.load_or_create_index()

        print("[institutional-index] Scanning institutional documents...")
        manifest = self._read_institutional_manifest()
        current_files = self._scan_institutional_files()
        previous_files = manifest.get("files", {})

        added = [path for path in current_files if path not in previous_files]
        modified = [
            path for path, meta in current_files.items()
            if path in previous_files and meta["sha256"] != previous_files[path]["sha256"]
        ]
        deleted = [path for path in previous_files if path not in current_files]
        unchanged_count = len(current_files) - len(added) - len(modified)
        print(
            "[institutional-index] "
            f"files={len(current_files)} added={len(added)} "
            f"modified={len(modified)} deleted={len(deleted)} unchanged={unchanged_count}"
        )

        for relative_path in deleted:
            print(f"[institutional-index] deleting: {relative_path}")
            engine.delete_ref_doc(previous_files[relative_path]["doc_id"])

        parser_name = "local"
        print(f"[institutional-index] parser={parser_name}")
        node_counts: dict[str, int] = {}
        for relative_path in modified:
            print(f"[institutional-index] updating: {relative_path}")
            engine.delete_ref_doc(previous_files[relative_path]["doc_id"])
            node_counts[relative_path] = await self._insert_institutional_file(
                engine,
                relative_path,
                current_files[relative_path]["sha256"],
                parser_name,
            )
            print(f"[institutional-index] nodes={node_counts[relative_path]} file={relative_path}")

        for relative_path in added:
            print(f"[institutional-index] adding: {relative_path}")
            node_counts[relative_path] = await self._insert_institutional_file(
                engine,
                relative_path,
                current_files[relative_path]["sha256"],
                parser_name,
            )
            print(f"[institutional-index] nodes={node_counts[relative_path]} file={relative_path}")

        print("[institutional-index] Persisting index...")
        engine.persist()
        next_files = {
            path: previous_files[path]
            for path in previous_files
            if path in current_files and path not in modified
        }
        for relative_path, meta in current_files.items():
            if relative_path in added or relative_path in modified:
                next_files[relative_path] = {
                    "sha256": meta["sha256"],
                    "doc_id": self._institutional_doc_id(relative_path),
                    "parser": parser_name,
                    "node_count": node_counts[relative_path],
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }

        next_manifest = {
            "source_dir": os.path.relpath(self.institutional_docs_dir, self.project_root),
            "embedding_model": self.embed_name,
            "chunk_size": 512,
            "chunk_overlap": 100,
            "files": dict(sorted(next_files.items())),
        }
        os.makedirs(os.path.dirname(self.institutional_manifest_path), exist_ok=True)
        with open(self.institutional_manifest_path, "w", encoding="utf-8") as f:
            json.dump(next_manifest, f, ensure_ascii=False, indent=2)
        print("[institutional-index] Manifest updated.")

        return json.dumps(
            {
                "status": "updated",
                "added": added,
                "modified": modified,
                "deleted": deleted,
                "unchanged_count": unchanged_count,
                "total_files": len(current_files),
            },
            ensure_ascii=False,
        )

    async def search_institutional_index(self, query: str) -> str:
        """Search the persistent institutional-document index."""
        try:
            engine = self._get_institutional_engine()
            return await asyncio.to_thread(engine.search, query)
        except Exception as e:
            return f"Error in institutional search: {str(e)}"

    def _get_institutional_engine(self):
        """Lazily initialize the institutional LlamaIndex engine."""
        if self._institutional_engine is None:
            try:
                llamaindex_rag = load_llamaindex_rag()
            except Exception as e:
                raise build_llamaindex_env_error(e) from e

            self._institutional_engine = llamaindex_rag(
                persist_dir=self.institutional_index_dir,
                llm_api_key=self.llm_api_key,
                llm_base_url=self.llm_base_url,
                llm_name=self.llm_name,
                llm_enable_thinking=self.llm_enable_thinking,
                embed_api_key=self.embed_api_key,
                embed_base_url=self.embed_base_url,
                embed_name=self.embed_name,
                rerank_api_key=self.rerank_api_key,
                rerank_base_url=self.rerank_base_url,
                rerank_name=self.rerank_name,
                rerank_provider=self.rerank_provider,
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
                similarity_top_k=self.similarity_top_k,
                rerank_top_n=self.rerank_top_n,
            )
        return self._institutional_engine

    def _get_contract_llamaindex_engine(self):
        """Lazily initialize the temporary contract LlamaIndex engine."""
        if self._contract_llamaindex_engine is None:
            try:
                llamaindex_rag = load_llamaindex_rag()
            except Exception as e:
                raise build_llamaindex_env_error(e) from e

            self._contract_llamaindex_engine = llamaindex_rag(
                persist_dir="",
                llm_api_key=self.llm_api_key,
                llm_base_url=self.llm_base_url,
                llm_name=self.llm_name,
                llm_enable_thinking=self.llm_enable_thinking,
                embed_api_key=self.embed_api_key,
                embed_base_url=self.embed_base_url,
                embed_name=self.embed_name,
                rerank_api_key=self.rerank_api_key,
                rerank_base_url=self.rerank_base_url,
                rerank_name=self.rerank_name,
                rerank_provider=self.rerank_provider,
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
                similarity_top_k=self.similarity_top_k,
                rerank_top_n=self.rerank_top_n,
            )
        return self._contract_llamaindex_engine

    def _read_institutional_manifest(self) -> dict:
        if not os.path.exists(self.institutional_manifest_path):
            return {"files": {}}
        with open(self.institutional_manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _scan_institutional_files(self) -> dict[str, dict[str, str]]:
        supported_suffixes = {".docx"}
        files: dict[str, dict[str, str]] = {}
        for root, _, filenames in os.walk(self.institutional_docs_dir):
            for filename in filenames:
                path = os.path.join(root, filename)
                if os.path.splitext(filename)[1].lower() not in supported_suffixes:
                    continue
                relative_path = os.path.relpath(path, self.institutional_docs_dir).replace(os.sep, "/")
                files[relative_path] = {"sha256": self._file_sha256(path)}
        return dict(sorted(files.items()))

    async def _insert_institutional_file(
        self,
        engine,
        relative_path: str,
        source_hash: str,
        parser_name: str,
    ) -> int:
        absolute_path = os.path.join(self.institutional_docs_dir, relative_path)
        content = FileParser.parse_file(absolute_path)
        return await asyncio.to_thread(
            engine.insert_text,
            content=content,
            relative_path=relative_path,
            doc_id=self._institutional_doc_id(relative_path),
            source_hash=source_hash,
            parser=parser_name,
        )

    @staticmethod
    def _file_sha256(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _institutional_doc_id(relative_path: str) -> str:
        return f"institutional:{relative_path}"
