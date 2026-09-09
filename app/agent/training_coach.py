"""Training plan generator — narrates a user's trend stats and picks catalog drills.

This module calls the same local model (`gemma3:4b`) as the chat assistant.
It is NOT a measurement engine: all numbers come from `coaching/trends.py` and
are only repeated or prioritized by the LLM.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent.ollama_agent import generate_text
from app.coaching.recommend import allowed_drill_summaries, hydrate_recommendations


_ALLOWED_KEYS = ["SUMMARY", "FOCUS_AREAS", "STRENGTHS", "IMPROVEMENTS", "SUGGESTIONS", "RECOMMENDATIONS"]

# Matches a JSON array of objects, e.g. `[{"drill_id": "...", ...}, ...]`. Used both to
# find a recommendations blob that landed in the wrong section, and to strip one out of
# prose text it should never have appeared in — a small local model is not reliable about
# keeping its JSON under the right label (see assistant/chat.py's own link-repair
# functions for the same class of issue).
_JSON_ARRAY_RE = re.compile(r"\[\s*\{.*?\}\s*\]", re.DOTALL)
# Matches things like (drill_id: "front_foot_block"), "drill_id": "front_foot_block",
# or drill_id: front_foot_block that the small model sometimes leaks into prose.
_DRILL_ID_RE = re.compile(
    r"[\(\[]?\s*['\"]?drill_id['\"]?\s*[:=]\s*['\"]?[a-z0-9_]+['\"]?\s*[\)\]]?",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", (text or "").strip())


def _strip_drill_ids(text: str) -> str:
    """Remove any 'drill_id: ...' snippets the model leaks into prose."""
    if not text:
        return text
    # First remove parentheses/brackets containing the drill_id reference.
    text = _DRILL_ID_RE.sub("", text)
    # Then clean up stray surrounding punctuation/whitespace left behind, e.g. "( )" or " -  -".
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\[\s*\]", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return _clean(text)


def _strip_embedded_json(text: str) -> str:
    """Remove any JSON-array-shaped substring and drill_id leaks from prose so it never renders as raw text."""
    if not text:
        return text
    return _strip_drill_ids(_clean(_JSON_ARRAY_RE.sub("", text)))


def _parse_sections(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        matched: str | None = None
        for k in _ALLOWED_KEYS:
            if stripped.upper().startswith(k + ":") or stripped.upper() == k:
                matched = k
                break
        if matched:
            if current:
                out[current] = "\n".join(buf).strip()
            current = matched
            rest = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            buf = [rest] if rest else []
        else:
            buf.append(line)
    if current:
        out[current] = "\n".join(buf).strip()
    return out


def _parse_recommendation_list(blob: str) -> list[dict[str, Any]]:
    text = (blob or "").strip()
    if not text:
        return []
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _extract_recommendations_anywhere(sections: dict[str, str], raw_text: str) -> list[dict[str, Any]]:
    """Recover a recommendations JSON array even if the model mislabeled or merged
    sections, by falling back to a scan of the whole raw response.
    """
    picks = _parse_recommendation_list(sections.get("RECOMMENDATIONS", ""))
    if picks:
        return picks
    for value in sections.values():
        match = _JSON_ARRAY_RE.search(value or "")
        if match:
            picks = _parse_recommendation_list(match.group(0))
            if picks:
                return picks
    match = _JSON_ARRAY_RE.search(raw_text or "")
    if match:
        return _parse_recommendation_list(match.group(0))
    return []


async def generate_training_plan(
    stats: dict[str, Any],
    allowed_drills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a coaching plan with verified drill recommendations.

    `stats` is the output of `coaching.trends.build_training_profile`.
    `allowed_drills` is the catalog view filtered to the user's focus tags
    (and, when known, the player's bowling style).
    """
    profile = stats.get("player_profile") or {}
    focus_areas = stats.get("focus_areas", [])
    focus_tags = [f["tag"] for f in focus_areas]
    bowling_style = profile.get("bowling_style")

    if allowed_drills is None:
        allowed_drills = allowed_drill_summaries(focus_tags, bowling_style=bowling_style)

    tool_payload = {
        "player": {
            "name": profile.get("player_name", "Bowler"),
            "age_years": profile.get("age_years"),
            "height_m": profile.get("height_m"),
            "weight_lbs": profile.get("weight_lbs"),
            "bowling_arm": profile.get("bowling_arm"),
            "bowling_style": bowling_style,
        },
        "sample_size": stats.get("sample_size"),
        "date_range": stats.get("date_range"),
        "overall_score": stats.get("overall_score"),
        "metrics": {k: _summarize_metric(v) for k, v in (stats.get("metrics") or {}).items()},
        "scores": {k: _summarize_metric(v) for k, v in (stats.get("scores") or {}).items()},
        "focus_areas": focus_areas,
        "allowed_drills": allowed_drills or [],
    }

    system = (
        "You are Cric-Lab, an AI cricket bowling coach writing a personal training review. "
        "The player has analyzed several Action-mode deliveries. "
        "You are given a JSON summary of their trends, scores, recurring focus areas, and a closed catalog of drills. "
        "Use ONLY the provided JSON. Do not invent or fill in missing values. "
        "Do not measure anything — you are only interpreting numbers computed by the app. "
        "Never claim radar-grade ball speed or 3D rotation. "
        "Coach to the player's age, height, bowling arm, and style (pace/spin/medium); "
        "only recommend drills from allowed_drills that suit their bowling_style — never recommend "
        "a spin-technique drill to a pace bowler or a pace drill to a spin bowler. "
        "\n\n"
        "Write a full, specific review, not a generic tip sheet:\n"
        "SUMMARY: 3-5 sentences. State the sample size, the date range, and at least one concrete "
        "trend number (a metric value, a percent change, or a score) from the JSON.\n"
        "FOCUS_AREAS: 3-5 sentences interpreting what the recurring focus_areas tags mean for this "
        "bowler's mechanics and how often each one showed up (cite the count) — do not just restate "
        "the tag names, explain the mechanical cause and consequence.\n"
        "STRENGTHS: 3-5 sentences on what is measurably improving or consistently good, citing the "
        "specific metric/score name and its value or trend.\n"
        "IMPROVEMENTS: 3-5 sentences on what is holding this bowler back, citing specific metric "
        "numbers and focus-area counts — not vague advice.\n"
        "SUGGESTIONS: a numbered list of 3-5 concrete actions written as plain prose. "
        "Each item must reference a specific point made in IMPROVEMENTS and, where relevant, "
        "name the drill by its plain-English title or purpose from allowed_drills. "
        "NEVER include a drill_id, JSON, or machine identifiers in SUGGESTIONS — only human-readable text.\n"
        "\n"
        "Rules for every prose section above: cite at least one real number from the JSON; never write "
        "unearned filler like 'keep practicing' or 'great job' with no stat attached; if a metric's "
        "status is insufficient_data, say so plainly instead of writing around it.\n"
        "\n"
        "After SUGGESTIONS, add RECOMMENDATIONS: a JSON array of objects "
        '{"drill_id","reason","priority"} using ONLY drill_id values from allowed_drills. '
        "Pick 2-3 drills that match the focus_areas and the player's bowling_style. Do not invent ids or URLs. "
        "Put the RECOMMENDATIONS JSON array immediately after the literal 'RECOMMENDATIONS:' label and "
        "nowhere else — never place any JSON or drill_id values inside SUMMARY, FOCUS_AREAS, STRENGTHS, IMPROVEMENTS, or SUGGESTIONS.\n"
        "\n"
        "Respond in plain text with exactly these labeled sections, in this order:\n"
        "SUMMARY:\nFOCUS_AREAS:\nSTRENGTHS:\nIMPROVEMENTS:\nSUGGESTIONS:\nRECOMMENDATIONS:"
    )

    prompt = (
        f"Player trend summary:\n{json.dumps(tool_payload, indent=2, default=str)}\n\n"
        "Write the training review now."
    )

    try:
        # num_predict=1400 asks for a full written review; on a slow local Ollama
        # setup that can take longer than the client's default 300s read timeout,
        # so this call gets a longer budget explicitly. Generation already runs in
        # a background task (see api/training.py), so a longer wait here no longer
        # blocks the page.
        raw_text = _clean(await generate_text(prompt, system=system, num_predict=1400, timeout_s=900.0))
        status = "ok"
    except Exception as e:
        # Some exceptions (notably httpx.ReadTimeout) carry no message, so str(e)
        # can be "" — fall back to the exception's type name so this is never blank.
        error_text = str(e) or f"{type(e).__name__} (no further detail from the exception)"
        return {
            "status": "llm_unavailable",
            "summary": "AI coaching suggestions are unavailable right now — the stats below are still up to date.",
            "focus_areas": "",
            "strengths": "",
            "improvements": "",
            "suggestions": "",
            "recommendations": hydrate_recommendations(None, tags=focus_tags, bowling_style=bowling_style),
            "error": error_text,
        }

    sections = _parse_sections(raw_text)
    raw_recs = _extract_recommendations_anywhere(sections, raw_text)
    recs = hydrate_recommendations(raw_recs, tags=focus_tags, bowling_style=bowling_style)

    return {
        "status": status,
        "summary": _strip_embedded_json(sections.get("SUMMARY", "")),
        "focus_areas": _strip_embedded_json(sections.get("FOCUS_AREAS", "")),
        "strengths": _strip_embedded_json(sections.get("STRENGTHS", "")),
        "improvements": _strip_embedded_json(sections.get("IMPROVEMENTS", "")),
        "suggestions": _strip_embedded_json(sections.get("SUGGESTIONS", "")),
        "recommendations": recs,
    }


def _summarize_metric(m: dict[str, Any]) -> dict[str, Any]:
    """Strip noisy series before sending to the LLM; keep only summary fields."""
    return {
        "status": m.get("status"),
        "count": m.get("count"),
        "latest": m.get("latest"),
        "best": m.get("best"),
        "mean": m.get("mean"),
        "trend": m.get("trend"),
    }
