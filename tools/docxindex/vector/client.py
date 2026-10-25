"""Compatibility imports; embedding transport and settings live in runtime."""
from endpoints.runtime.embedding_client import EMBEDDING_BATCH_SIZE, EmbeddingClient, EmbeddingSettings

__all__ = ["EMBEDDING_BATCH_SIZE", "EmbeddingClient", "EmbeddingSettings"]
