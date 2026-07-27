"""
ai_mapper.py
============
Stage 3: AI CONFIDENCE ENGINE.

For any sheet schema NOT already in memory.py's cache, we ask Gemini to
propose a mapping from the sheet's raw column names to the Universal
General Ledger's target columns (Date, Description, Payee, Debit, Credit,
Balance), along with a self-reported confidence score and a one-sentence
justification. pipeline.py then decides whether that confidence is high
enough to auto-apply or whether a human needs to confirm it.

Structured Outputs design note
-------------------------------
The obvious Pydantic shape for "a mapping" is `Dict[str, str]` (raw column
-> target column). We deliberately do NOT use that as the Gemini
response_schema. Gemini's structured-output schema (and JSON Schema
generally) is built around fixed, named properties -- an arbitrary-keyed
dict maps to "additionalProperties", which is a much less reliably
constrained pattern for an LLM to fill in correctly than an explicit list
of objects. So the LLM is asked to return a LIST of
`{raw_column, target_column}` pairs (a shape structured-output engines
handle very reliably), which this module then converts into the
Dict[str, str] the rest of the pipeline expects. The public function
below hands back exactly the `{"mapping": ..., "confidence_score": ...,
"reasoning": ...}` contract regardless of this internal representation.
"""

from __future__ import annotations

import os
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()  # picks up GEMINI_API_KEY from a .env file in the project root

TARGET_COLUMNS = ["Date", "Description", "Payee", "Debit", "Credit", "Balance"]
DEFAULT_MODEL = "gemini-2.5-flash"


class ColumnAssignment(BaseModel):
    raw_column: str = Field(description="The exact raw column header text as it appears in the source sheet.")
    target_column: Literal["Date", "Description", "Payee", "Debit", "Credit", "Balance", "Unmapped"] = Field(
        description=(
            "Which Universal General Ledger column this raw column represents. "
            "Use 'Unmapped' if the raw column doesn't correspond to any of the target columns "
            "(e.g. an internal ID, a currency code, a blank spacer column)."
        )
    )


class SchemaMappingResponse(BaseModel):
    """The exact structured shape Gemini is constrained to return."""

    assignments: list[ColumnAssignment] = Field(
        description="One assignment per raw column provided in the prompt -- every raw column must appear exactly once."
    )
    confidence_score: int = Field(ge=0, le=100, description="Overall confidence (0-100) that this mapping is correct.")
    reasoning: str = Field(description="One-sentence justification for the mapping and confidence score.")


class AIMapperError(RuntimeError):
    """Raised for any failure calling or parsing the Gemini mapping response."""


def _get_client():
    """Lazily construct the Gemini client. Imported here (not at module
    top-level import time in some designs) so this module can still be
    imported -- and its Pydantic schema introspected/tested -- even in
    environments without the google-genai package installed; here we do
    import at top-of-function to give a clear, actionable error the
    moment a real AI call is attempted, rather than at import time."""
    try:
        from google import genai
    except ImportError as exc:
        raise AIMapperError(
            "The 'google-genai' package is required to call the AI Confidence Engine. "
            "Install it with: pip install google-genai"
        ) from exc

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise AIMapperError(
            "GEMINI_API_KEY is not set. Create a .env file in the ai_ingestion/ project "
            "root (see .env.example) with a valid Gemini API key before ingesting a "
            "sheet schema that isn't already in schema_memory.json."
        )
    return genai.Client(api_key=api_key)


def build_prompt(raw_columns: list[str], sample_rows: list[dict]) -> str:
    """Compose the natural-language instructions + the sheet's raw columns
    and a few sample data rows, so the model has real values to reason
    over (e.g. distinguishing a 'Balance' column of running totals from an
    'Amount' column of individual transaction values)."""
    columns_block = "\n".join(f"  - {col!r}" for col in raw_columns)
    samples_block = "\n".join(f"  {i + 1}. {row}" for i, row in enumerate(sample_rows))

    return f"""You are a financial data mapping specialist. You are given the raw column
headers of ONE sheet from a company's accounting/bank export, plus a few sample
data rows, and must map every raw column to exactly one Universal General Ledger
target column, or mark it 'Unmapped'.

Target columns (a raw column maps to AT MOST one of these):
  - Date: the transaction date
  - Description: a free-text narrative/memo/particulars of the transaction
  - Payee: the counterparty name (vendor, customer, payee, beneficiary)
  - Debit: money leaving the account / a debit-side amount
  - Credit: money entering the account / a credit-side amount
  - Balance: a running or closing balance after the transaction

Raw columns to map:
{columns_block}

Sample data rows (for context on what each column actually contains):
{samples_block}

Provide an assignment for EVERY raw column listed above (using 'Unmapped' for
any that don't fit), plus an overall confidence_score (0-100) for the mapping
as a whole, and a one-sentence reasoning."""


def map_columns_with_ai(
    raw_columns: list[str],
    sample_rows: list[dict],
    model: str = DEFAULT_MODEL,
) -> dict:
    """Call Gemini with a Structured Output schema to propose a column
    mapping for a never-before-seen sheet schema.

    Returns exactly:
        {
            "mapping": {raw_column: target_column, ...},   # 'Unmapped' columns omitted
            "confidence_score": int (0-100),
            "reasoning": str,
        }

    Raises AIMapperError if the package/API key is missing or the model
    fails to return a parseable structured response.
    """
    from google.genai import types  # deferred import, mirrors _get_client()

    client = _get_client()
    prompt = build_prompt(raw_columns, sample_rows)

    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SchemaMappingResponse,
            ),
        )
    except Exception as exc:  # network/auth/model errors from the SDK
        raise AIMapperError(f"Gemini API call failed: {exc}") from exc

    parsed: SchemaMappingResponse | None = getattr(response, "parsed", None)
    if parsed is None:
        raise AIMapperError(
            f"Gemini did not return a parseable structured response. Raw text: {getattr(response, 'text', '')!r}"
        )

    mapping = {
        assignment.raw_column: assignment.target_column
        for assignment in parsed.assignments
        if assignment.target_column != "Unmapped"
    }

    return {
        "mapping": mapping,
        "confidence_score": parsed.confidence_score,
        "reasoning": parsed.reasoning,
    }
