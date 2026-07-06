"""Vector math and blob serialization (compatible with existing SQLite embeddings)."""

from __future__ import annotations

import math
import struct
from typing import List, Optional


def cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0


def embedding_to_blob(vec: List[float]) -> bytes:
    return struct.pack(f"{len(vec)}d", *vec)


def blob_to_embedding(blob: Optional[bytes]) -> Optional[List[float]]:
    if not blob:
        return None
    n = len(blob) // 8
    if n == 0:
        return None
    return list(struct.unpack(f"{n}d", blob))
