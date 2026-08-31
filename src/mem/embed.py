"""Embeddings via the local ollama daemon's HTTP API.

MODEL_CODE is the version-pinned identifier the memoryfield spec requires in
index filenames; OLLAMA_MODEL is ollama's unversioned tag for the same model.
"""

import json
import os
import urllib.error
import urllib.request

MODEL_CODE = "nomic-embed-text-v1.5"
OLLAMA_MODEL = "nomic-embed-text"


def _base_url() -> str:
    return os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts, raising RuntimeError with a fix-it hint on failure."""
    body = json.dumps(
        {"model": OLLAMA_MODEL, "input": texts, "truncate": True}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{_base_url()}/api/embed",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.load(resp)
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"embedding failed ({e}). Is ollama running? "
            "Try: brew services start ollama && ollama pull nomic-embed-text"
        ) from e
    embeddings = payload.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise RuntimeError(f"unexpected ollama response: {payload}")
    return embeddings
