#!/usr/bin/env python3
"""Embedding wrapper for Cortex OS memory search."""

from fastembed import TextEmbedding

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

_model = None


def get_model() -> TextEmbedding:
    """Lazy-load the embedding model."""
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts. Returns list of 384-dim float vectors."""
    model = get_model()
    return [vec.tolist() for vec in model.embed(texts, batch_size=256)]


def embed_query(text: str) -> list[float]:
    """Embed a single query string. Returns 384-dim float vector."""
    return embed_texts([text])[0]
