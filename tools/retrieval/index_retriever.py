from __future__ import annotations

import asyncio
import json
import os

from tools.retrieval.llamaindex import build_llamaindex_env_error, load_llamaindex_rag


class IndexRetriever:
    """Facade over temporary in-memory LlamaIndex contract retrieval."""

    def __init__(
        self,
        *,
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
        similarity_top_k: int | None = None,
        rerank_top_n: int | None = None,
    ) -> None:
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

    def _get_contract_llamaindex_engine(self):
        """Lazily initialize the temporary contract LlamaIndex engine."""
        if self._contract_llamaindex_engine is None:
            try:
                llamaindex_rag = load_llamaindex_rag()
            except Exception as e:
                raise build_llamaindex_env_error(e) from e

            self._contract_llamaindex_engine = llamaindex_rag(
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
