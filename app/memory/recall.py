"""Pre-trade similarity recall and optional caution rule (Phase 4.5, step 4).

Safety guarantee:
- Recall is ADVISORY ONLY on exits (`close_long` / `close_short` never blocked or delayed).
- Optional caution rule (`caution_rule_enabled`, OFF by default): only fires on NEW entries when
  `similar_count >= caution_min_samples` (default 20) and `avg_r < caution_avg_r_threshold` (-0.3).
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.memory.embeddings import EmbeddingService, cosine_similarity
from app.memory.journal import regime_bucket_from_indicators, setup_descriptor
from app.storage.models import LessonRecord


def recall_similar_setups(
    db: Session,
    *,
    symbol: str,
    timeframe: str,
    strategy: str,
    action: str,
    indicators: dict[str, Any],
    settings: Settings | None = None,
    embedder: EmbeddingService | None = None,
) -> dict[str, Any]:
    s = settings or get_settings()
    if action in ("close_long", "close_short"):
        return {
            "count": 0,
            "win_rate": 0.0,
            "avg_r": 0.0,
            "top_mistake_tag": None,
            "caution_triggered": False,
            "caution_action": None,
            "note": "exit_signal_bypass",
        }

    direction = "long" if action == "buy" else "short"
    regime_bucket = regime_bucket_from_indicators(indicators)
    emb_svc = embedder or EmbeddingService(s)
    query_txt = setup_descriptor(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        direction=direction,
        regime_bucket=regime_bucket,
        indicators=indicators,
    )
    q_vec, _ = emb_svc.embed(query_txt)

    candidates = (
        db.query(LessonRecord)
        .filter_by(strategy=strategy, direction=direction, regime_bucket=regime_bucket)
        .all()
    )
    if not candidates:
        return {
            "count": 0,
            "win_rate": 0.0,
            "avg_r": 0.0,
            "top_mistake_tag": None,
            "caution_triggered": False,
            "caution_action": None,
            "regime_bucket": regime_bucket,
        }

    scored = sorted(
        ((cosine_similarity(q_vec, c.embedding_json or []), c) for c in candidates),
        key=lambda x: x[0],
        reverse=True,
    )[: s.recall_top_k]

    top_lessons = [c for _, c in scored]
    n = len(top_lessons)
    wins = sum(1 for c in top_lessons if float(c.r_multiple) > 0)
    avg_r = sum(float(c.r_multiple) for c in top_lessons) / n
    tag_counts: Counter[str] = Counter()
    for c in top_lessons:
        for t in (c.tags or []):
            if t != "clean_win":
                tag_counts[t] += 1
    top_tag = tag_counts.most_common(1)[0][0] if tag_counts else None

    caution_triggered = bool(
        s.caution_rule_enabled
        and n >= s.caution_min_samples
        and Decimal(str(round(avg_r, 4))) < s.caution_avg_r_threshold
    )
    return {
        "count": n,
        "win_rate": round(wins * 100.0 / n, 1),
        "avg_r": round(avg_r, 3),
        "top_mistake_tag": top_tag,
        "regime_bucket": regime_bucket,
        "caution_triggered": caution_triggered,
        "caution_action": s.caution_action if caution_triggered else None,
    }
