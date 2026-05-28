"""Cross-session memory for the SQL generator.

We store every successful (question -> SQL) pair in the ``qa_examples`` table and feed
the few most similar ones back to the LLM as few-shot examples on the next request.
Embeddings are computed locally with a token + bigram feature hash (no external
dependency, no embeddings endpoint required); cosine similarity is done in Python.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from fintextsql.db.models import QAExample

EMBEDDING_DIM = 256
SIMILARITY_THRESHOLD = 0.18  # below this we don't bother retrieving as a few-shot
MAX_EXAMPLES_RETURNED = 3
MAX_STORED_EXAMPLES = 2000  # soft cap; oldest pruned beyond this


@dataclass(slots=True)
class FewShotExample:
    question: str
    sql: str
    score: float


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    stripped = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return stripped.replace("đ", "d").replace("Đ", "d")


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _normalize(text))


def embed_text(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Feature-hash embedding: token + bigram counts hashed into ``dim`` buckets."""
    tokens = _tokenize(text)
    if not tokens:
        return [0.0] * dim
    bigrams = [f"{tokens[i]}_{tokens[i + 1]}" for i in range(len(tokens) - 1)]
    vec = [0.0] * dim
    for term in tokens + bigrams:
        bucket = int(hashlib.md5(term.encode("utf-8")).hexdigest(), 16) % dim
        vec[bucket] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [value / norm for value in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _question_key(question: str) -> str:
    """Canonical form used to dedupe near-identical questions."""
    return " ".join(_tokenize(question))[:480]


def find_similar(db: Session, question: str, *, limit: int = MAX_EXAMPLES_RETURNED) -> list[FewShotExample]:
    """Return up to ``limit`` past Q/SQL pairs most similar to ``question``."""
    target_emb = embed_text(question)
    if not any(target_emb):
        return []
    rows = db.execute(
        select(QAExample.question, QAExample.sql, QAExample.embedding).where(QAExample.embedding.isnot(None))
    ).all()
    scored: list[tuple[float, str, str]] = []
    for past_question, past_sql, past_emb in rows:
        if not isinstance(past_emb, list):
            continue
        if _normalize(past_question) == _normalize(question):
            # Don't feed the question back as its own example.
            continue
        score = _cosine(target_emb, past_emb)
        if score >= SIMILARITY_THRESHOLD:
            scored.append((score, past_question, past_sql))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        FewShotExample(question=q, sql=sql, score=round(score, 4))
        for score, q, sql in scored[:limit]
    ]


def render_few_shot_block(examples: Iterable[FewShotExample]) -> str:
    """Render examples into a compact text block that goes into the LLM prompt."""
    chunks: list[str] = []
    for index, example in enumerate(examples, start=1):
        chunks.append(
            f"Example {index} (similar past question):\n"
            f"  Q: {example.question}\n"
            f"  SQL:\n{example.sql}"
        )
    return "\n\n".join(chunks)


def record_example(
    db: Session,
    *,
    question: str,
    sql: str,
    intent: str | None,
) -> None:
    """Upsert a Q -> SQL pair into the cross-session memory.

    Silently ignored when the question or SQL is empty. Existing rows are kept (we
    increment use_count instead of overwriting) so we accumulate co-occurrences.
    """
    question = (question or "").strip()
    sql = (sql or "").strip()
    if not question or not sql:
        return
    key = _question_key(question)
    if not key:
        return
    embedding = embed_text(question)
    stmt = insert(QAExample).values(
        question_key=key,
        question=question[:4000],
        sql=sql[:8000],
        intent=intent,
        embedding=embedding,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[QAExample.question_key],
        set_={"use_count": QAExample.use_count + 1, "sql": stmt.excluded.sql},
    )
    db.execute(stmt)
    db.commit()
    _prune_oldest(db)


def _prune_oldest(db: Session, cap: int = MAX_STORED_EXAMPLES) -> None:
    """Keep the table under ``cap`` rows by deleting the oldest, least-used examples."""
    count = db.execute(select(QAExample.id).limit(cap + 1)).fetchall()
    if len(count) <= cap:
        return
    overflow = len(count) - cap
    victims = db.execute(
        select(QAExample.id).order_by(QAExample.use_count.asc(), QAExample.created_at.asc()).limit(overflow)
    ).scalars().all()
    if not victims:
        return
    db.execute(QAExample.__table__.delete().where(QAExample.id.in_(victims)))
    db.commit()
