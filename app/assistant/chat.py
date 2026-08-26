"""The CricLab assistant's answer layer.

Grounding is enforced in three places, not one:

1. **Retrieval** hands the model a small set of user-facing passages and nothing
   else — see `rag.py`. There is no internal documentation in the corpus.
2. **The prompt** tells the model to answer only from those passages and to say
   so when they do not cover the question.
3. **The output is checked** before it is returned. If an answer mentions
   anything that looks like internal machinery, it is dropped and replaced —
   a model instructed not to say something is not the same as a model that
   cannot.

Questions that are *about* the internals never reach the model at all. They get
a straight, friendly refusal and a pointer to what the assistant can help with.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agent.ollama_agent import generate_text
from app.assistant import rag

log = logging.getLogger("criclab.assistant")

MAX_QUESTION_CHARS = 800
MAX_HISTORY_TURNS = 6
# Someone is watching a typing indicator. Past this it is better to answer
# from the passages directly than to keep them waiting.
GENERATION_TIMEOUT_S = 25.0

SYSTEM_PROMPT = """You are the CricLab assistant. You help people use CricLab: \
filming clips, understanding their numbers, their account, support and coaching \
bookings.

Rules you follow without exception:

- Answer ONLY from the reference passages given to you. They are the only thing \
you know.
- If the passages do not answer the question, say plainly that you do not have \
that information and suggest opening a support ticket. Never guess, and never \
fill a gap with something that sounds plausible.
- Never discuss, describe or speculate about how CricLab is built, hosted or \
implemented. That includes any technology, service, model, algorithm, database \
or internal process. If asked, say it is not something you can go into, and \
offer to help with using CricLab instead.
- Never state prices, plan limits or figures that are not in the passages.
- You cannot see the user's account, footage, bookings or billing. Do not \
pretend to.
- Write plainly, in British English, in short paragraphs. Two or three sentences \
is usually enough. No headings, no bullet lists unless the answer is genuinely \
a list of steps.
"""

# Asking *about* the internals. Deliberately narrow: "how does CricLab measure
# ball speed" is a fair user question and must not be caught here, while "what
# database does CricLab use" must be.
_INTERNALS_PATTERNS = [
    r"\b(what|which|whose)\b[^?]{0,40}\b(tech|technolog|stack|framework|language|database|db|server|host(ing|ed)?|infra(structure)?|cloud|api|backend|back-end|architecture)\b",
    r"\b(built|written|made|coded|developed|running|hosted|deployed)\s+(with|in|on|using)\b",
    r"\b(tell|show|give)\s+me\b[^?]{0,30}\b(source code|codebase|repo|repository|schema|endpoint|api key|secret|env|credential)\b",
    r"\b(system|developer|internal)\s+prompt\b",
    r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions\b",
    r"\bwhat\s+(ai\s+)?model\b|\bwhich\s+(ai\s+)?model\b|\bllm\b|\bneural net(work)?\b",
    r"\b(schema|collection|table)\s+(design|structure|layout)\b",
    r"\bhow\s+(is|are)\s+(it|criclab|the\s+data|passwords?|tokens?)\s+(stored|hashed|encrypted|hosted|deployed)\b",
]
_INTERNALS_RE = [re.compile(p, re.IGNORECASE) for p in _INTERNALS_PATTERNS]

# Words that should never appear in an answer. If one does, something has gone
# wrong upstream and the generated text is discarded rather than trimmed.
_LEAK_TERMS = {
    "mongodb", "mongo", "fastapi", "uvicorn", "postgres", "mysql", "redis",
    "ollama", "openai", "anthropic", "gpt", "llama", "gemma", "mediapipe",
    "opencv", "pytorch", "tensorflow", "kubernetes", "docker", "aws", "azure",
    "gcp", "cloudinary", "nginx", "python", "javascript", "typescript", "react",
    "node.js", "jwt", "bcrypt", "sha-256", "api endpoint", "database schema",
    "system prompt", "vector database", "embedding model", "ransac",
}

DEFLECTION = (
    "That is not something I can go into — how CricLab is built is kept private. "
    "I can help with anything about using it though: filming a clip that reads "
    "well, what a number in your report means, your account, support, or booking "
    "a coaching session."
)

NO_ANSWER = (
    "I do not have anything on that, and I would rather say so than guess. "
    "Open a support ticket and someone will pick it up — that is the quickest "
    "route for anything specific to your account."
)

# Shown as tappable prompts when a conversation starts.
STARTERS: list[str] = [
    "How should I film a delivery?",
    "Why was my ball not tracked?",
    "What does arm speed actually mean?",
    "How do I book a coaching session?",
    "How do I change my password?",
]


def _asks_about_internals(question: str) -> bool:
    return any(pattern.search(question) for pattern in _INTERNALS_RE)


def _leaks(answer: str) -> str | None:
    lowered = answer.lower()
    for term in _LEAK_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", lowered):
            return term
    return None


def _extractive(passages: list[dict[str, Any]]) -> str:
    """A grounded answer without a model: the best passage, lightly tidied.

    Used when no language model is reachable. Less conversational, but every
    word of it came from the knowledge base, which is the property that matters.
    """
    if not passages:
        return NO_ANSWER
    body = passages[0]["body"]
    # Markdown emphasis reads badly in a chat bubble.
    body = re.sub(r"\*\*(.+?)\*\*", r"\1", body)
    body = re.sub(r"^[-*]\s+", "• ", body, flags=re.MULTILINE)
    if len(body) > 900:
        body = body[:900].rsplit(". ", 1)[0] + "."
    return f"Here is what the CricLab guide says about {passages[0]['title'].split(' — ')[-1].lower()}:\n\n{body}"


def _history_text(history: list[dict[str, str]] | None) -> str:
    if not history:
        return ""
    lines = []
    for turn in history[-MAX_HISTORY_TURNS:]:
        role = "User" if turn.get("role") == "user" else "Assistant"
        content = (turn.get("content") or "").strip()[:400]
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def answer(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    user_name: str | None = None,
) -> dict[str, Any]:
    """Answer one question. Always returns a reply; never raises."""
    question = (question or "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        return {"answer": "Ask me anything about using CricLab.", "sources": [], "grounded": False}

    if _asks_about_internals(question):
        return {"answer": DEFLECTION, "sources": [], "grounded": False, "refused": True}

    passages = await rag.retrieve(question)
    if not passages:
        return {"answer": NO_ANSWER, "sources": [], "grounded": False, "escalate": True}

    reference = "\n\n---\n\n".join(
        f"[{i + 1}] {p['title']}\n{p['body']}" for i, p in enumerate(passages)
    )
    conversation = _history_text(history)
    prompt = (
        f"Reference passages:\n\n{reference}\n\n"
        + (f"Conversation so far:\n{conversation}\n\n" if conversation else "")
        + f"Question{f' from {user_name}' if user_name else ''}: {question}\n\n"
        "Answer using only the passages above."
    )

    try:
        generated = (await generate_text(
            prompt, system=SYSTEM_PROMPT, num_predict=320, timeout_s=GENERATION_TIMEOUT_S
        )).strip()
    except Exception as exc:
        log.info("assistant generation unavailable (%s)", type(exc).__name__)
        generated = ""

    if generated:
        leaked = _leaks(generated)
        if leaked:
            # Not repaired — replaced. A partly-scrubbed answer is still an
            # answer that tried to say it.
            log.warning("assistant answer withheld: mentioned %r", leaked)
            generated = ""

    return {
        "answer": generated or _extractive(passages),
        "sources": rag.sources_of(passages),
        "grounded": True,
        "generated": bool(generated),
    }
