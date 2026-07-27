"""
pipeline.py
===========
Stage 4: ROUTING & EXCEPTION GATE.

This is the orchestrator that ties memory.py and ai_mapper.py together and
decides, per sheet, one of three paths:

    1. FAST LANE   -- schema fingerprint already in schema_memory.json:
                      apply the saved mapping immediately, no AI call, no
                      human involved.
    2. AUTO-APPLY  -- new schema, but the AI's confidence_score >= 90:
                      trust it, apply it, and save it to memory so the
                      NEXT sheet with this schema hits the Fast Lane.
    3. HUMAN GATE  -- new schema, AI confidence_score < 90: show the
                      proposed mapping in the CLI and require the user to
                      confirm or correct it before it's applied and saved.

Every sheet that reaches AUTO-APPLY or HUMAN GATE gets written into
schema_memory.json (via memory.remember_mapping / save_memory) so this
exact schema is never re-prompted for again.
"""

from __future__ import annotations

import pandas as pd

import ai_mapper
import memory as memory_module

CONFIDENCE_THRESHOLD = 90
SAMPLE_ROW_COUNT = 3


def _get_sample_rows(df: pd.DataFrame, n: int = SAMPLE_ROW_COUNT) -> list[dict]:
    """First N data rows, as plain JSON-serializable dicts, to give the AI
    real values to reason over. NaN is converted to None so it doesn't
    break JSON/prompt serialization."""
    sample = df.head(n).where(pd.notna(df.head(n)), None)
    return sample.to_dict(orient="records")


def _normalize_mapping_keys(mapping: dict[str, str], actual_columns: list[str]) -> dict[str, str]:
    """Defensive re-keying: the AI is instructed to echo raw column names
    back VERBATIM, but LLM output isn't guaranteed byte-for-byte -- a
    trailing space or a re-cased letter would otherwise cause
    transformer.apply_mapping() to silently miss that column (it does an
    exact `raw in df.columns` check). We re-key the AI's mapping onto the
    ACTUAL DataFrame column strings using a case/whitespace-insensitive
    match, so small formatting drift from the model never silently drops
    a column from the ledger. Anything the AI mentions that still doesn't
    match any real column is dropped with a warning (likely a minor
    hallucination) rather than silently miskeyed.
    """
    lookup = {str(col).strip().lower(): col for col in actual_columns}
    normalized: dict[str, str] = {}
    for raw_col, target in mapping.items():
        actual = lookup.get(str(raw_col).strip().lower())
        if actual is None:
            print(f"    [WARN] AI referenced column {raw_col!r} which doesn't match any actual column; ignoring.")
            continue
        normalized[actual] = target
    return normalized


def _prompt_user_for_confirmation(
    sheet_name: str,
    raw_columns: list[str],
    ai_mapping: dict[str, str],
    confidence_score: int,
    reasoning: str,
) -> tuple[dict[str, str], str]:
    """The Human Gate: show the AI's proposed mapping for a low-confidence
    sheet and let the user accept it as-is or correct individual columns.

    Returns (final_mapping, source) where source is 'user_confirmed' if
    accepted unchanged, or 'user_adjusted' if the user corrected anything.
    """
    print(f"\n{'=' * 70}")
    print(f"HUMAN REVIEW REQUIRED - sheet '{sheet_name}'")
    print(f"AI confidence: {confidence_score}/100 (threshold is {CONFIDENCE_THRESHOLD})")
    print(f"AI reasoning: {reasoning}")
    print(f"{'-' * 70}")
    print("Proposed mapping (raw column -> UGL column):")
    for raw_col in raw_columns:
        target = ai_mapping.get(raw_col, "Unmapped")
        print(f"  {raw_col!r:40s} -> {target}")
    print(f"{'=' * 70}")

    answer = input("Accept this mapping as-is? [y/n]: ").strip().lower()
    if answer in ("y", "yes"):
        return ai_mapping, "user_confirmed"

    print(
        f"\nEnter the correct target for each raw column. Valid targets: "
        f"{', '.join(ai_mapper.TARGET_COLUMNS)}, or 'skip' to leave it unmapped."
    )
    corrected: dict[str, str] = {}
    valid_targets = {t.lower(): t for t in ai_mapper.TARGET_COLUMNS}
    for raw_col in raw_columns:
        default = ai_mapping.get(raw_col, "skip")
        while True:
            entry = input(f"  {raw_col!r} [{default}]: ").strip().lower() or default.lower()
            if entry == "skip":
                break
            if entry in valid_targets:
                corrected[raw_col] = valid_targets[entry]
                break
            print(f"    '{entry}' is not valid. Choose from: {', '.join(ai_mapper.TARGET_COLUMNS)}, or 'skip'.")

    return corrected, "user_adjusted"


def process_sheet(
    sheet_name: str,
    df: pd.DataFrame,
    memory: dict,
) -> dict:
    """Run one sheet through the Fast Lane / AI / Human Gate routing logic.

    Mutates `memory` in place (via memory.remember_mapping) whenever a NEW
    mapping is approved, so the caller can persist it. Returns:
        {
            "mapping": {...},
            "confidence_score": int,
            "source": "memory" | "ai_auto" | "user_confirmed" | "user_adjusted",
            "fingerprint": str,
        }
    """
    raw_columns = list(df.columns)
    fingerprint = memory_module.compute_fingerprint(raw_columns)

    cached = memory_module.get_mapping_for_fingerprint(fingerprint, memory)
    if cached is not None:
        print(f"[FAST LANE] '{sheet_name}': schema seen before (fingerprint {fingerprint[:12]}...), reusing saved mapping.")
        return {
            "mapping": cached["mapping"],
            "confidence_score": cached["confidence_score"],
            "source": "memory",
            "fingerprint": fingerprint,
        }

    print(f"[AI ENGINE] '{sheet_name}': new schema (fingerprint {fingerprint[:12]}...), calling Gemini...")
    sample_rows = _get_sample_rows(df)
    result = ai_mapper.map_columns_with_ai(raw_columns, sample_rows)
    ai_mapping = _normalize_mapping_keys(result["mapping"], raw_columns)
    confidence_score, reasoning = result["confidence_score"], result["reasoning"]

    if confidence_score >= CONFIDENCE_THRESHOLD:
        print(f"[AUTO-APPLY] '{sheet_name}': confidence {confidence_score} >= {CONFIDENCE_THRESHOLD}, applying automatically.")
        final_mapping, source = ai_mapping, "ai_auto"
    else:
        final_mapping, source = _prompt_user_for_confirmation(sheet_name, raw_columns, ai_mapping, confidence_score, reasoning)

    memory_module.remember_mapping(
        memory, fingerprint, raw_columns, final_mapping, confidence_score, reasoning, source
    )

    return {
        "mapping": final_mapping,
        "confidence_score": confidence_score,
        "source": source,
        "fingerprint": fingerprint,
    }


def process_workbook(
    financial_sheets: dict[str, pd.DataFrame],
    memory_path=memory_module.DEFAULT_MEMORY_PATH,
) -> dict[str, dict]:
    """Run every discovered financial sheet through process_sheet(),
    persisting schema_memory.json incrementally after each newly-approved
    mapping (so progress survives an interrupted CLI session instead of
    only saving at the very end).

    Returns {sheet_name: result_dict} as produced by process_sheet().
    """
    memory = memory_module.load_memory(memory_path)
    results: dict[str, dict] = {}

    for sheet_name, df in financial_sheets.items():
        result = process_sheet(sheet_name, df, memory)
        results[sheet_name] = result
        if result["source"] != "memory":
            memory_module.save_memory(memory, memory_path)

    return results
