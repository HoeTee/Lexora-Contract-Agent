from typing import Optional

from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.schema import MetadataMode, NodeWithScore, QueryBundle
from tools.docxindex.reranker import RerankerClient


class QwenRerankPostprocessor(BaseNodePostprocessor):
    api_key: str
    api_base: str
    model: str
    provider: str = "higress_qwen"
    top_n: int | None = None
    instruct: str = ""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        provider: str = "higress_qwen",
        top_n: int | None = None,
        instruct: str = "",
        **kwargs,
    ):
        super().__init__(
            api_key=api_key,
            api_base=base_url.rstrip("/"),
            model=model,
            provider=provider,
            top_n=top_n,
            instruct=instruct,
            **kwargs,
        )

    def _postprocess_nodes(self, nodes: list[NodeWithScore], query_bundle: Optional[QueryBundle] = None):
        if not nodes or query_bundle is None:
            return nodes
        client = RerankerClient(
            model=self.model,
            base_url=self.api_base,
            api_key=self.api_key,
            instruct=self.instruct,
            provider=self.provider,
        )
        scores = client.rank(
            query_bundle.query_str, [node.get_content(metadata_mode=MetadataMode.NONE) for node in nodes], self.top_n
        )
        return [NodeWithScore(node=nodes[index].node, score=score) for index, score in scores]
