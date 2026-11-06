"""Embedding adapter with Ollama local fallback and deterministic normalized hash-vector fallback
so trading NEVER blocks when an external LLM or Ollama service is down.

When the primary provider fails:
1. If `outbox` is supplied, the text is queued in `embedding_outbox` for backfill when the service recovers.
2. A deterministic local normalized projection is returned immediately so lessons are still searchable.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.storage.outbox import OutboxStore


def deterministic_embed(text: str, dim: int = 768) -> list[float]:
    """Bag-of-tokens signed feature hashing into `dim` dimensions, L2-normalized.
    Preserves token-overlap cosine similarity for setup/regime/tag strings offline."""
    vec = [0.0] * dim
    tokens = [t.strip(".,:;()[]{}\"'").lower() for t in text.split() if t.strip()]
    for idx, tok in enumerate(tokens):
        for gram in (tok, f"{tokens[idx-1]}_{tok}" if idx > 0 else tok):
            h = hashlib.sha256(gram.encode("utf-8")).digest()
            bucket = int.from_bytes(h[:4], "little") % dim
            sign = 1.0 if (h[4] & 1) == 0 else -1.0
            vec[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 6) for v in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class EmbeddingService:
    def __init__(self, settings: Settings | None = None, outbox: OutboxStore | None = None):
        self.s = settings or get_settings()
        self.outbox = outbox or OutboxStore(self.s.outbox_path)
        self.simulated_outage: bool = False

    def embed(
        self,
        text: str,
        *,
        journal_id_for_backfill: str | None = None,
        metadata_for_backfill: dict[str, Any] | None = None,
    ) -> tuple[list[float], str]:
        """Returns (vector, model_version). Never raises; queues for backfill if primary provider fails."""
        if not self.simulated_outage and self.s.embed_provider == "ollama":
            try:
                with httpx.Client(timeout=2.5) as client:
                    resp = client.post(
                        f"{self.s.ollama_url}/api/embeddings",
                        json={"model": self.s.ollama_embed_model, "prompt": text},
                    )
                    if resp.status_code == 200:
                        emb = resp.json().get("embedding")
                        if isinstance(emb, list) and len(emb) > 0:
                            return [float(x) for x in emb], f"ollama:{self.s.ollama_embed_model}"
            except Exception:
                pass

        # Primary unavailable -> queue for backfill if associated with a closed trade journal
        if journal_id_for_backfill:
            self.outbox.enqueue_embedding(
                journal_id_for_backfill,
                text,
                metadata_for_backfill or {},
            )
        return deterministic_embed(text, self.s.embed_dim), f"fallback-hash-{self.s.embed_dim}"

    def backfill_pending(self, db_session: Any) -> int:
        """Re-embeds queued lessons when the embedding service comes back online."""
        from app.storage.models import LessonRecord

        if self.simulated_outage:
            return 0
        pending = self.outbox.pending_embeddings()
        completed = 0
        for item in pending:
            vec, model_ver = self.embed(item["text"])
            # If still on fallback during an real ollama outage and provider=='ollama', keep queued
            if self.s.embed_provider == "ollama" and model_ver.startswith("fallback-") and not item["metadata"].get("allow_hash_backfill"):
                continue
            lesson = db_session.query(LessonRecord).filter_by(journal_id=item["journal_id"]).one_or_none()
            if lesson:
                lesson.embedding_json = vec
                lesson.model_version = model_ver
                db_session.flush()
            self.outbox.mark_embedding_done(item["id"])
            completed += 1
        return completed
