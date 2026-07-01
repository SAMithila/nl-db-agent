"""
evaluation/llm_judge.py
-----------------------
Phase 7C — LLM-as-Judge Evaluator

Uses GPT-4o to score agent answers on multiple dimensions:
    - Correctness (1-5): Does the answer match ground truth?
    - Faithfulness (1-5): Is the answer grounded in retrieved context?
    - Hallucination (yes/no): Did the agent state unsupported facts?
    - Source Attribution (1-5): Did it cite SQL vs document correctly?
    - Helpfulness (1-5): Would a business user find this useful?


Usage:
    from evaluation.llm_judge import judge_answer
    score = judge_answer(question, answer, context, route)
"""

import os
import json
import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL  = "gpt-4o"


# ------------------------------------------------------------------
# Judge prompts
# ------------------------------------------------------------------

SQL_JUDGE_PROMPT = """You are an expert evaluator for a natural language to SQL agent.

Question asked: {question}
SQL generated: {sql}
Result rows: {row_count}
Agent answer: {answer}
Expected value: {expected_value}

Evaluate the agent's answer on these dimensions:

1. CORRECTNESS (1-5): Does the answer correctly address the question?
   - 5: Perfect answer with correct numbers
   - 3: Partially correct or missing key details
   - 1: Wrong or completely off-topic

2. HALLUCINATION (yes/no): Did the agent state any facts NOT present in the SQL result?
   - yes: Agent invented numbers or facts not in the data
   - no: All claims are supported by the SQL result

3. HELPFULNESS (1-5): Would a business analyst find this useful?
   - 5: Clear, actionable, well-explained
   - 3: Technically correct but hard to interpret
   - 1: Confusing or not useful

Respond ONLY with valid JSON:
{{
    "correctness": <1-5>,
    "hallucination": "<yes/no>",
    "helpfulness": <1-5>,
    "reasoning": "<one sentence explanation>"
}}"""


RAG_JUDGE_PROMPT = """You are an expert evaluator for a RAG system.
IMPORTANT: Judge ONLY based on what is explicitly stated in the Document Context below.
If a fact appears in the context, it is NOT hallucination even if you don't recognize it.
Only mark hallucination=yes if the answer states facts ABSENT from the context.

Question asked: {question}
Documents retrieved from: {sources}
Document context used: {rag_context}
Agent answer: {answer}
Expected keywords: {expected_keywords}

Evaluate the agent's answer on these dimensions:

1. FAITHFULNESS (1-5): Is every claim in the answer supported by the retrieved documents?
   - 5: All claims directly supported by the context
   - 3: Mostly supported but some unsupported claims
   - 1: Answer contradicts or ignores the context

2. HALLUCINATION (yes/no): Did the agent state facts NOT present in the retrieved documents?
   - yes: Agent invented statistics or facts not in the documents
   - no: All claims are grounded in the retrieved context

3. SOURCE ATTRIBUTION (1-5): Did the answer correctly cite which document the info came from?
   - 5: Clearly cites specific documents with accurate info
   - 3: Mentions sources but vaguely
   - 1: No attribution or wrong attribution

4. RELEVANCE (1-5): Does the answer actually address the question using the retrieved context?
   - 5: Directly answers the question using specific document data
   - 3: Partially relevant
   - 1: Irrelevant to the question

5. HELPFULNESS (1-5): Would a business analyst find this useful?
   - 5: Clear, specific, actionable with real numbers
   - 3: Correct but vague
   - 1: Not useful

Respond ONLY with valid JSON:
{{
    "faithfulness": <1-5>,
    "hallucination": "<yes/no>",
    "source_attribution": <1-5>,
    "relevance": <1-5>,
    "helpfulness": <1-5>,
    "reasoning": "<one sentence explanation>"
}}"""


BOTH_JUDGE_PROMPT = """You are an expert evaluator for an Agentic RAG system that combines SQL data with document retrieval.

Question asked: {question}
SQL result rows: {row_count}
Documents retrieved from: {sources}
Agent answer: {answer}

Evaluate the agent's answer on these dimensions:

1. CORRECTNESS (1-5): Does the SQL data part of the answer correctly address the question?
   - 5: SQL numbers are correct and clearly stated
   - 3: SQL data used but partially correct
   - 1: SQL data wrong or missing

2. FAITHFULNESS (1-5): Is the document context part grounded in the retrieved documents?
   - 5: All document claims directly supported by retrieved context
   - 3: Mostly supported
   - 1: Contradicts documents

3. SYNTHESIS QUALITY (1-5): How well does the answer combine SQL data with document context?
   - 5: Seamlessly integrates both sources with clear comparison
   - 3: Uses both but doesn't connect them meaningfully
   - 1: Uses only one source or ignores the other

4. HALLUCINATION (yes/no): Did the agent invent any facts not supported by SQL or documents?
   - yes: Any invented statistics or unsupported claims
   - no: All claims grounded in data or documents

5. HELPFULNESS (1-5): Would a music industry analyst find this answer useful?
   - 5: Provides actionable insight combining internal data and industry context
   - 3: Informative but not actionable
   - 1: Not useful

Respond ONLY with valid JSON:
{{
    "correctness": <1-5>,
    "faithfulness": <1-5>,
    "synthesis_quality": <1-5>,
    "hallucination": "<yes/no>",
    "helpfulness": <1-5>,
    "reasoning": "<one sentence explanation>"
}}"""


# ------------------------------------------------------------------
# Core judge function
# ------------------------------------------------------------------

def judge_answer(
    question:          str,
    answer:            str,
    route:             str,
    sql:               Optional[str]  = None,
    row_count:         int            = 0,
    rag_context:       Optional[str]  = None,
    rag_sources:       Optional[list] = None,
    expected_value:    Optional[dict] = None,
    expected_keywords: Optional[list] = None,
) -> dict:
    """
    Uses GPT-4o to evaluate an agent answer.

    Args:
        question:          The user's question
        answer:            The agent's answer
        route:             "SQL" | "RAG" | "BOTH"
        sql:               Generated SQL (for SQL/BOTH routes)
        row_count:         Number of rows returned
        rag_context:       Retrieved document context
        rag_sources:       List of source document names
        expected_value:    Expected value dict (for SQL routes)
        expected_keywords: Keywords that should appear (for RAG routes)

    Returns:
        dict with scores and overall quality rating
    """
    if not answer:
        return _empty_score(route, "No answer provided")

    try:
        if route == "SQL":
            prompt = SQL_JUDGE_PROMPT.format(
                question       = question,
                sql            = sql or "N/A",
                row_count      = row_count,
                answer         = answer[:1000],
                expected_value = json.dumps(expected_value) if expected_value else "Not specified",
            )
            scores = _call_judge(prompt)
            return _build_sql_result(scores)

        elif route == "RAG":
            source_names = [s.get("title", s.get("source", "Unknown")) for s in (rag_sources or [])]
            
            # Build structured context — each chunk clearly labeled
            structured_context = ""
            if rag_context:
                structured_context = rag_context[:4000]
            
            prompt = RAG_JUDGE_PROMPT.format(
                question          = question,
                sources           = ", ".join(source_names) or "None retrieved",
                rag_context       = structured_context,
                answer            = answer[:1000],
                expected_keywords = json.dumps(expected_keywords) if expected_keywords else "Not specified",
            )

        elif route == "BOTH":
            source_names = [s.get("title", s.get("source", "Unknown")) for s in (rag_sources or [])]
            prompt = BOTH_JUDGE_PROMPT.format(
                question  = question,
                row_count = row_count,
                sources   = ", ".join(source_names) or "None retrieved",
                answer    = answer[:1000],
            )
            scores = _call_judge(prompt)
            return _build_both_result(scores)

        else:
            return _empty_score(route, f"Unknown route: {route}")

    except Exception as e:
        return _empty_score(route, f"Judge error: {str(e)}")


def _call_judge(prompt: str) -> dict:
    """Calls GPT-4o with the judge prompt and parses JSON response."""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content
    return json.loads(raw)


def _build_sql_result(scores: dict) -> dict:
    correctness  = scores.get("correctness", 3)
    helpfulness  = scores.get("helpfulness", 3)
    hallucinated = scores.get("hallucination", "no").lower() == "yes"

    overall = round((correctness + helpfulness) / 2, 1)
    if hallucinated:
        overall = max(1.0, overall - 1.5)

    return {
        "route":        "SQL",
        "correctness":  correctness,
        "helpfulness":  helpfulness,
        "hallucination": hallucinated,
        "overall":      overall,
        "reasoning":    scores.get("reasoning", ""),
        "passed":       overall >= 3.5 and not hallucinated,
    }


def _build_rag_result(scores: dict) -> dict:
    faithfulness      = scores.get("faithfulness", 3)
    source_attribution = scores.get("source_attribution", 3)
    relevance         = scores.get("relevance", 3)
    helpfulness       = scores.get("helpfulness", 3)
    hallucinated      = scores.get("hallucination", "no").lower() == "yes"

    overall = round((faithfulness + source_attribution + relevance + helpfulness) / 4, 1)
    if hallucinated:
        overall = max(1.0, overall - 1.5)

    return {
        "route":              "RAG",
        "faithfulness":       faithfulness,
        "source_attribution": source_attribution,
        "relevance":          relevance,
        "helpfulness":        helpfulness,
        "hallucination":      hallucinated,
        "overall":            overall,
        "reasoning":          scores.get("reasoning", ""),
        "passed":             overall >= 3.5 and not hallucinated,
    }


def _build_both_result(scores: dict) -> dict:
    correctness      = scores.get("correctness", 3)
    faithfulness     = scores.get("faithfulness", 3)
    synthesis        = scores.get("synthesis_quality", 3)
    helpfulness      = scores.get("helpfulness", 3)
    hallucinated     = scores.get("hallucination", "no").lower() == "yes"

    overall = round((correctness + faithfulness + synthesis + helpfulness) / 4, 1)
    if hallucinated:
        overall = max(1.0, overall - 1.5)

    return {
        "route":            "BOTH",
        "correctness":      correctness,
        "faithfulness":     faithfulness,
        "synthesis_quality": synthesis,
        "helpfulness":      helpfulness,
        "hallucination":    hallucinated,
        "overall":          overall,
        "reasoning":        scores.get("reasoning", ""),
        "passed":           overall >= 3.5 and not hallucinated,
    }


def _empty_score(route: str, reason: str) -> dict:
    return {
        "route":       route,
        "overall":     0,
        "hallucination": False,
        "passed":      False,
        "reasoning":   reason,
        "error":       reason,
    }


# ------------------------------------------------------------------
# Batch judge utility
# ------------------------------------------------------------------

def judge_batch(results: list) -> list:
    """
    Runs LLM judge on a list of evaluation results.
    Adds judge_scores to each result dict.
    """
    judged = []
    for r in results:
        if r.get("tier") in ("rag", "both") or r.get("route") in ("RAG", "BOTH"):
            scores = judge_answer(
                question    = r.get("question", ""),
                answer      = r.get("answer", ""),
                route       = r.get("route", "RAG"),
                rag_context = r.get("rag_context"),
                rag_sources = r.get("rag_sources", []),
                expected_keywords = r.get("expected_content_keywords"),
            )
        elif r.get("passed") is not None:
            scores = judge_answer(
                question       = r.get("question", ""),
                answer         = r.get("answer", ""),
                route          = "SQL",
                sql            = r.get("sql"),
                row_count      = r.get("row_count", 0),
                expected_value = r.get("expected_value"),
            )
        else:
            scores = None

        r["judge_scores"] = scores
        judged.append(r)

    return judged


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("LLM JUDGE TEST")
    print("=" * 60)

    # Test SQL judge
    print("\n── SQL Route ──")
    result = judge_answer(
        question    = "What is the total revenue by genre?",
        answer      = "Rock leads with $826.65, followed by Latin at $382.14 and Metal at $261.36.",
        route       = "SQL",
        sql         = "SELECT g.Name, SUM(il.UnitPrice * il.Quantity) FROM Genre g JOIN Track t ON g.GenreId = t.GenreId JOIN InvoiceLine il ON t.TrackId = il.TrackId GROUP BY g.Name",
        row_count   = 25,
        expected_value = None,
    )
    print(f"Overall: {result['overall']}/5")
    print(f"Hallucination: {result['hallucination']}")
    print(f"Passed: {result['passed']}")
    print(f"Reasoning: {result['reasoning']}")

    # Test RAG judge
    print("\n── RAG Route ──")
    result = judge_answer(
        question    = "What is the global recorded music revenue growth rate?",
        answer      = "According to the IFPI Global Music Report 2026, global recorded music revenues grew 6.4% to reach $31.7 billion in 2025.",
        route       = "RAG",
        rag_context = "Global recorded music revenues grew 6.4% and reached US$31.7 billion in 2025.",
        rag_sources = [{"title": "IFPI Global Music Report 2026", "source": "GMR2026_SOTI.pdf"}],
        expected_keywords = ["6.4%", "31.7 billion"],
    )
    print(f"Overall: {result['overall']}/5")
    print(f"Faithfulness: {result.get('faithfulness')}/5")
    print(f"Hallucination: {result['hallucination']}")
    print(f"Passed: {result['passed']}")
    print(f"Reasoning: {result['reasoning']}")

    # Test BOTH judge
    print("\n── BOTH Route ──")
    result = judge_answer(
        question    = "How does our Rock revenue compare to global industry trends?",
        answer      = "Our Chinook database shows Rock generating $826.65, our highest genre. The IFPI 2026 report confirms Rock maintains ~34% global market share, suggesting our catalog aligns with industry demand.",
        route       = "BOTH",
        row_count   = 1,
        rag_sources = [{"title": "IFPI Global Music Report 2026"}],
    )
    print(f"Overall: {result['overall']}/5")
    print(f"Synthesis quality: {result.get('synthesis_quality')}/5")
    print(f"Hallucination: {result['hallucination']}")
    print(f"Passed: {result['passed']}")
    print(f"Reasoning: {result['reasoning']}")

    print("\n" + "=" * 60)
    print("✅ LLM Judge tests complete")
    print("=" * 60)
