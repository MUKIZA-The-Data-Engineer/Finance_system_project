"""
memory.py
=========
Stage 2: SCHEMA FINGERPRINTING & MEMORY.

Calling an LLM for every single sheet, every single run, is slow and
costs money -- and it's unnecessary once a given company's export format
has been seen and mapped once. This module computes a stable fingerprint
of a sheet's raw column set and persists {fingerprint: approved mapping}
to a local JSON file, so the "Fast Lane" in pipeline.py can skip the AI
(and any CLI prompt) entirely for a schema we've already solved.

The fingerprint is intentionally based ONLY on the column names (cleaned
and sorted), not the data or the sheet name -- two sheets named "Jan_2026"
and "Feb_2026" from the same bank, with identical columns, should hit the
exact same cache entry.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MEMORY_PATH = Path(__file__).parent / "schema_memory.json"


def compute_fingerprint(columns: list[str]) -> str:
    """SHA256 hash of the sheet's clean, sorted raw column names.

    - Cleaning (strip + lowercase) means 'Debit Amount' and ' DEBIT AMOUNT '
      fingerprint identically.
    - Sorting means column ORDER doesn't matter -- ['Date','Amount'] and
      ['Amount','Date'] are the same schema for mapping purposes.
    """
    normalized = sorted(str(c).strip().lower() for c in columns)
    joined = "|".join(normalized)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def load_memory(path: str | Path = DEFAULT_MEMORY_PATH) -> dict:
    """Load schema_memory.json, or return an empty dict if it doesn't
    exist yet (first run)."""
    path = Path(path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_memory(memory: dict, path: str | Path = DEFAULT_MEMORY_PATH) -> None:
    """Persist the full memory dict back to disk, pretty-printed so it's
    human-readable/auditable (an accountant can open this file and see
    exactly what mapping was approved for each known schema)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2, sort_keys=True)


def get_mapping_for_fingerprint(fingerprint: str, memory: dict) -> dict | None:
    """Fast Lane lookup: return the previously-approved entry for this
    exact schema fingerprint, or None if it's never been seen before."""
    return memory.get(fingerprint)


def remember_mapping(
    memory: dict,
    fingerprint: str,
    raw_columns: list[str],
    mapping: dict[str, str],
    confidence_score: int,
    reasoning: str,
    source: str,
) -> dict:
    """Record an approved mapping (AI auto-approved OR human-confirmed/
    adjusted) into the in-memory dict. Caller is responsible for calling
    save_memory() afterwards to persist it -- kept separate so a caller
    processing many sheets can batch multiple remember_mapping() calls
    into one disk write, or persist incrementally, as it prefers.

    `source` should be one of: 'ai_auto', 'user_confirmed', 'user_adjusted'
    -- kept for audit purposes so it's always clear whether a human ever
    had to intervene on this schema.
    """
    memory[fingerprint] = {
        "raw_columns": list(raw_columns),
        "mapping": mapping,
        "confidence_score": confidence_score,
        "reasoning": reasoning,
        "source": source,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    return memory
