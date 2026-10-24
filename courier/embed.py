"""Optional embeddings for semantic search. Off unless config `embeddings.provider` is set.

ollama      -> local, nothing leaves the PC (e.g. model nomic-embed-text)
nvidia      -> NVIDIA NIM embeddings API (your text is sent to NVIDIA)
openrouter  -> OpenRouter embeddings endpoint (your text is sent to the provider)
"""
from __future__ import annotations

import math
import os
from array import array

from .providers import http_post


class EmbedError(Exception):
    pass


class Embedder:
    def __init__(self, kind, base_url, model, key_env="", transport=http_post, batch=32, input_type=False, local=False):
        self.kind, self.base, self.model, self.key_env = kind, base_url.rstrip("/"), model, key_env
        self.transport, self.batch, self.input_type, self.local = transport, batch, input_type, local

    def embed(self, texts, role="passage"):
        out = []
        for i in range(0, len(texts), self.batch):
            out += self._call(texts[i:i + self.batch], role)
        return out

    def _call(self, texts, role):
        headers = {"Content-Type": "application/json"}
        if self.key_env:
            key = (os.environ.get(self.key_env) or "").split(",")[0].strip()
            if not key:
                raise EmbedError(f"{self.key_env} is not set")
            headers["Authorization"] = f"Bearer {key}"
        if self.kind == "ollama":
            url, body = f"{self.base}/api/embed", {"model": self.model, "input": texts}
        else:
            url, body = f"{self.base}/embeddings", {"model": self.model, "input": texts, "encoding_format": "float"}
            if self.input_type:
                body["input_type"] = "query" if role == "query" else "passage"
        status, data, _ = self.transport(url, headers, body, 60)
        if status != 200:
            raise EmbedError(f"embeddings HTTP {status}: {str(data)[:150]}")
        vecs = data.get("embeddings") if self.kind == "ollama" else [d["embedding"] for d in sorted(data.get("data", []), key=lambda d: d["index"])]
        if not vecs or len(vecs) != len(texts):
            raise EmbedError("embeddings response did not match the request")
        return vecs


def from_config(cfg, transport=http_post):
    e = cfg.get("embeddings") or {}
    p = e.get("provider")
    if not p:
        return None
    if p == "ollama":
        base = cfg.get("ollama", {}).get("base_url", "http://localhost:11434/v1").replace("/v1", "")
        return Embedder("ollama", base, e.get("model", "nomic-embed-text"), transport=transport, local=True)
    if p == "nvidia":
        return Embedder("openai", cfg["nvidia"]["base_url"], e.get("model", "nvidia/nv-embedqa-e5-v5"), "NVIDIA_API_KEY",
                        transport, input_type=True)
    if p == "openrouter":
        if not e.get("model"):
            raise EmbedError("set embeddings.model for openrouter")
        return Embedder("openai", cfg["openrouter"]["base_url"], e["model"], "OPENROUTER_API_KEY", transport)
    raise EmbedError("embeddings.provider: ollama|nvidia|openrouter")


def pack(v):
    return array("f", v).tobytes()


def unpack(b):
    a = array("f")
    a.frombytes(b)
    return a


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
