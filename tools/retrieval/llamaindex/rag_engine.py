"""LlamaIndex retrieval over temporary in-memory contract indexes."""

from llama_index.core import (
    Document,
    Settings,
    VectorStoreIndex,
)
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.schema import TextNode
from llama_index.llms.openai_like import OpenAILike

from tools.retrieval.llamaindex.limited_embedding import SemaphoreOpenAILikeEmbedding
from tools.retrieval.llamaindex.qwen_reranker import QwenRerankPostprocessor
from config import (
    MODEL_CALL_MAX_RETRIES,
    MODEL_CALL_TIMEOUT_SECONDS,
    RERANK_INSTRUCT,
    RERANK_PROVIDER,
)
from tools.document.docx_anchor_index import build_docx_anchor_nodes


class LlamaIndexRAG:
    """Vector index engine for temporary contract retrieval."""

    def __init__(
        self,
        *,
        llm_api_key: str,
        llm_base_url: str,
        llm_name: str,
        llm_enable_thinking: bool | None = None,
        embed_api_key: str,
        embed_base_url: str,
        embed_name: str,
        rerank_api_key: str = None,
        rerank_base_url: str = None,
        rerank_name: str = None,
        rerank_provider: str = RERANK_PROVIDER,
        similarity_top_k: int = 5,
        rerank_top_n: int = 3,
        chunk_size: int = 512,
        chunk_overlap: int = 100,
    ) -> None:
        self.embed_name = embed_name
        self.similarity_top_k = similarity_top_k
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

        llm_additional_kwargs = {}
        if llm_enable_thinking is not None:
            llm_additional_kwargs["extra_body"] = {
                "enable_thinking": llm_enable_thinking,
            }

        Settings.llm = OpenAILike(
            model=llm_name,
            api_key=llm_api_key,
            api_base=llm_base_url,
            is_chat_model=True,
            is_function_calling_model=True,
            timeout=MODEL_CALL_TIMEOUT_SECONDS,
            max_retries=MODEL_CALL_MAX_RETRIES,
            additional_kwargs=llm_additional_kwargs,
        )
        Settings.embed_model = SemaphoreOpenAILikeEmbedding(
            model_name=embed_name,
            api_key=embed_api_key,
            api_base=embed_base_url,
            timeout=MODEL_CALL_TIMEOUT_SECONDS,
            max_retries=MODEL_CALL_MAX_RETRIES,
        )

        self.reranker = None
        if rerank_base_url:
            self.reranker = QwenRerankPostprocessor(
                api_key=rerank_api_key,
                base_url=rerank_base_url or embed_base_url,
                model=rerank_name,
                provider=rerank_provider,
                top_n=rerank_top_n,
                timeout=MODEL_CALL_TIMEOUT_SECONDS,
                max_retries=MODEL_CALL_MAX_RETRIES,
                instruct=RERANK_INSTRUCT,
            )

        self._index = None

    def build_temporary_index_from_text(self, content: str, source_name: str = "current_contract") -> int:
        document = Document(
            text=content,
            id_=source_name,
            metadata={
                "source_file": source_name,
                "doc_id": source_name,
                "index_scope": "temporary_contract",
            },
        )
        splitter = SentenceSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            paragraph_separator="\n\n",
        )
        nodes = splitter.get_nodes_from_documents([document])
        self._index = VectorStoreIndex(nodes)
        return len(nodes)

    def build_temporary_index_from_docx(self, docx_path: str, source_name: str = "current_contract") -> int:
        anchor_nodes = build_docx_anchor_nodes(docx_path)
        nodes = [
            TextNode(
                text=anchor.text,
                id_=f"{source_name}:{anchor.anchor_type}:{anchor.anchor_id}",
                metadata={
                    "source_file": source_name,
                    "doc_id": source_name,
                    "index_scope": "temporary_contract",
                    "xml_anchor_type": anchor.anchor_type,
                    "xml_anchor_id": anchor.anchor_id,
                    "xml_anchor_path": anchor.path,
                },
            )
            for anchor in anchor_nodes
        ]
        if not nodes:
            raise ValueError("No non-empty DOCX paragraph/table nodes found for indexing.")
        self._index = VectorStoreIndex(nodes)
        return len(nodes)

    def search(self, query: str, result_title: str = "检索结果") -> str:
        """Search the loaded index. Returns text with source metadata."""
        if self._index is None:
            raise RuntimeError("Build a temporary contract index before searching.")

        retriever = self._index.as_retriever(similarity_top_k=self.similarity_top_k)
        postprocessors = [self.reranker] if self.reranker else []
        query_engine = RetrieverQueryEngine.from_args(
            retriever=retriever,
            node_postprocessors=postprocessors,
        )
        response = query_engine.query(query)

        parts = []
        for source_node in response.source_nodes:
            score = getattr(source_node, "score", None)
            text = source_node.get_content()
            metadata = getattr(source_node, "metadata", None) or {}
            # Keep the LLM-facing header free of ordinals so it cannot be
            # mistaken for the stable xml_anchor_id below.
            header = f"## {result_title}"
            source_file = metadata.get("source_file")
            xml_anchor_type = metadata.get("xml_anchor_type")
            xml_anchor_id = metadata.get("xml_anchor_id")
            if source_file:
                header += f"\n来源文件：{source_file}"
            if xml_anchor_type and xml_anchor_id:
                header += f"\nxml_anchor_type: {xml_anchor_type}\nxml_anchor_id: {xml_anchor_id}"
            if score is not None:
                header += f"\n相关度：{score:.3f}"
            parts.append(f"{header}\n内容：\n{text}")

        return "\n\n---\n\n".join(parts) if parts else "No relevant contract sections found."
