import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from docx import Document
from llama_index.core.schema import NodeWithScore
from starlette.requests import Request

from endpoints.runtime.auth import hash_password, save_users, verify_login
from endpoints.web.user_routes import sync_context_user
from loggers.review_history import load_history_records
from mcp_service.server import server
from tools.retrieval.index_retriever import IndexRetriever
from tools.retrieval.llamaindex import rag_engine


class RetainedReviewPathsTests(unittest.TestCase):
    def test_llamaindex_contract_tools_reuse_index_and_preserve_xml_anchors(self) -> None:
        facade = IndexRetriever(
            llm_api_key="unused",
            llm_base_url="http://unused.invalid/v1",
            llm_name="test-llm",
            llm_enable_thinking=False,
            embed_api_key="unused",
            embed_base_url="http://unused.invalid/v1",
            embed_name="test-embedding",
            rerank_api_key=None,
            rerank_base_url=None,
            rerank_name=None,
        )
        response = SimpleNamespace(source_nodes=[])
        query_engine = SimpleNamespace(query=Mock(return_value=response))
        index = Mock()

        def build_index(nodes):
            response.source_nodes = [NodeWithScore(node=node, score=0.9) for node in nodes]
            index.index_struct = SimpleNamespace(nodes_dict={node.node_id: node.node_id for node in nodes})
            return index

        with TemporaryDirectory() as directory:
            contract = Path(directory) / "contract.docx"
            document = Document()
            document.add_paragraph("付款期限为验收后十个工作日。")
            document.save(str(contract))

            with (
                patch.object(server, "index_retriever", facade),
                patch.object(server, "RETRIEVAL_BACKEND", "llamaindex"),
                patch.object(rag_engine, "Settings", SimpleNamespace()),
                patch.object(rag_engine, "OpenAILike"),
                patch.object(rag_engine, "SemaphoreOpenAILikeEmbedding"),
                patch.object(rag_engine, "VectorStoreIndex", side_effect=build_index) as vector_index,
                patch.object(rag_engine.RetrieverQueryEngine, "from_args", return_value=query_engine),
            ):
                built = json.loads(asyncio.run(server.contract_build_index(str(contract))))
                result = asyncio.run(server.contract_search("付款期限"))

        self.assertEqual(built, {"status": "built_temporary_index", "num_nodes": 1})
        self.assertEqual(vector_index.call_count, 1)
        index.as_retriever.assert_called_once_with(similarity_top_k=1)
        query_engine.query.assert_called_once_with("付款期限")
        self.assertIn("xml_anchor_type: paragraph", result)
        self.assertIn("xml_anchor_id: path:body/p1", result)
        self.assertIn("付款期限为验收后十个工作日。", result)

    def test_llamaindex_search_requires_a_temporary_index(self) -> None:
        engine = rag_engine.LlamaIndexRAG.__new__(rag_engine.LlamaIndexRAG)
        engine._index = None

        with self.assertRaisesRegex(RuntimeError, "Build a temporary contract index"):
            engine.search("付款期限")

    def test_history_reader_preserves_tenant_isolation_and_legacy_reads(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            for tenant, task, username in [
                ("tenant-a", "own-task", "alice"),
                ("tenant-a", "other-user-task", "bob"),
                ("tenant-b", "other-tenant-task", "alice"),
            ]:
                task_json = data / "web" / tenant / task / "task.json"
                task_json.parent.mkdir(parents=True)
                task_json.write_text(
                    json.dumps({"task_id": task, "username": username, "status": "succeeded"}),
                    encoding="utf-8",
                )
            legacy_json = data / "alice" / "records" / "review_history.json"
            legacy_json.parent.mkdir(parents=True)
            legacy_json.write_text(json.dumps([{"task_id": "legacy-task"}]), encoding="utf-8")

            current = load_history_records(data, "alice", tenant_id="tenant-a")
            legacy = load_history_records(data, "alice")

        self.assertEqual([record["task_id"] for record in current], ["own-task"])
        self.assertEqual(legacy, [{"task_id": "legacy-task"}])

    def test_disabled_user_loses_only_their_current_auth_context(self) -> None:
        with TemporaryDirectory() as directory:
            profiles = Path(directory) / "profiles.json"
            users = [{
                "username": "alice",
                "display_name": "Alice",
                "role": "admin",
                "enabled": True,
                "password_hash": hash_password("test-password"),
            }]
            save_users(profiles, users)
            self.assertIsNotNone(verify_login(profiles, "alice", "test-password"))
            self.assertIsNone(verify_login(profiles, "alice", "wrong-password"))

            request = Request({
                "type": "http",
                "query_string": b"ctx=current",
                "headers": [],
                "session": {"auth_contexts": {
                    "current": {"tenant_id": "tenant-a", "username": "alice"},
                    "other": {"tenant_id": "tenant-b", "username": "bob"},
                }},
            })
            with (
                patch("endpoints.web.user_routes.require_tenant", return_value={"name": "Tenant A"}),
                patch("endpoints.web.user_routes.tenant_profiles_file", return_value=profiles),
            ):
                current = sync_context_user(request)
                self.assertEqual(current["role"], "admin")
                users[0]["enabled"] = False
                save_users(profiles, users)
                self.assertIsNone(verify_login(profiles, "alice", "test-password"))
                self.assertIsNone(sync_context_user(request))

        self.assertEqual(set(request.session["auth_contexts"]), {"other"})


if __name__ == "__main__":
    unittest.main()
