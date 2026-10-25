"""Null LlamaIndex candidate caps must mean all candidates, never SDK defaults."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode

from tools.retrieval.llamaindex.rag_engine import LlamaIndexRAG
from tools.retrieval.llamaindex.qwen_reranker import QwenRerankPostprocessor


class NullableLlamaLimitsTests(unittest.TestCase):
    def test_null_similarity_top_k_requests_all_indexed_nodes(self):
        engine = LlamaIndexRAG.__new__(LlamaIndexRAG)
        engine.similarity_top_k = None
        engine.reranker = None
        engine._index = SimpleNamespace(
            index_struct=SimpleNamespace(nodes_dict={str(i): str(i) for i in range(17)}),
            as_retriever=Mock(return_value=object()),
        )
        query_engine = SimpleNamespace(query=Mock(return_value=SimpleNamespace(source_nodes=[])))
        with patch("tools.retrieval.llamaindex.rag_engine.RetrieverQueryEngine.from_args", return_value=query_engine):
            engine.search("query")
        engine._index.as_retriever.assert_called_once_with(similarity_top_k=17)

    def test_explicit_similarity_top_k_is_retained(self):
        engine = LlamaIndexRAG.__new__(LlamaIndexRAG)
        engine.similarity_top_k = 4
        engine.reranker = None
        engine._index = SimpleNamespace(as_retriever=Mock(return_value=object()))
        query_engine = SimpleNamespace(query=Mock(return_value=SimpleNamespace(source_nodes=[])))
        with patch("tools.retrieval.llamaindex.rag_engine.RetrieverQueryEngine.from_args", return_value=query_engine):
            engine.search("query")
        engine._index.as_retriever.assert_called_once_with(similarity_top_k=4)

    def test_null_rerank_top_n_is_forwarded_as_unlimited(self):
        reranker = QwenRerankPostprocessor(api_key="EMPTY", base_url="http://unused/v1/rerank", model="test")
        nodes = [NodeWithScore(node=TextNode(text=f"node {i}")) for i in range(7)]
        client = SimpleNamespace(rank=Mock(return_value=[(i, 0.9) for i in range(7)]))
        with patch("tools.retrieval.llamaindex.qwen_reranker.RerankerClient", return_value=client):
            result = reranker._postprocess_nodes(nodes, QueryBundle(query_str="query"))
        self.assertIsNone(client.rank.call_args.args[2])
        self.assertEqual(len(result), 7)


if __name__ == "__main__":
    unittest.main()
