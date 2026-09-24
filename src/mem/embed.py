"""Embeddings via the local ollama daemon's HTTP API.

MODEL_CODE is the version-pinned identifier used in the index filename (so
indexes from different model versions are never conflated); OLLAMA_MODEL is
ollama's unversioned tag for the same model.
"""

import json
import os
import urllib.error
import urllib.request

MODEL_CODE = "nomic-embed-text-v1.5"
OLLAMA_MODEL = "nomic-embed-text"


def base_url() -> str:
    return os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")


def status() -> tuple[bool, str]:
    """(ready, one-line status) for the embedding backend: is ollama
    reachable, and is the model pulled. Used by `mem init` so setup problems
    surface before the first embed."""
    try:
        with urllib.request.urlopen(f"{base_url()}/api/tags", timeout=3) as resp:
            models = [m.get("name", "") for m in json.load(resp).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return False, (
            f"ollama: not reachable at {base_url()}. Start it (`ollama serve`, or "
            "`brew services start ollama` on macOS), then: ollama pull nomic-embed-text"
        )
    if any(name.startswith(OLLAMA_MODEL) for name in models):
        return True, f"ollama: serving at {base_url()}, {OLLAMA_MODEL} present"
    return False, f"ollama: serving, but {OLLAMA_MODEL} is missing. Run: ollama pull {OLLAMA_MODEL}"


class InputTooLong(RuntimeError):
    """The text exceeds the model's context window (2048 tokens by default);
    raised only when embedding with truncate=False."""


def embed_texts(texts: list[str], truncate: bool = True) -> list[list[float]]:
    """Embed texts, raising RuntimeError with a fix-it hint on failure. With
    truncate=False, over-long input raises InputTooLong instead of being
    silently cut, which lets writers reject pages the index could not fully
    represent."""
    body = json.dumps(
        {"model": OLLAMA_MODEL, "input": texts, "truncate": truncate}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url()}/api/embed",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        if e.code == 400 and "context length" in detail:
            raise InputTooLong(detail) from e
        raise RuntimeError(f"embedding failed: HTTP {e.code} {detail[:200]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"embedding failed ({e}). Is ollama serving (`ollama serve`, or "
            "OLLAMA_URL for a remote one) and is the model pulled "
            "(`ollama pull nomic-embed-text`)?"
        ) from e
    embeddings = payload.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise RuntimeError(f"unexpected ollama response: {payload}")
    return embeddings
