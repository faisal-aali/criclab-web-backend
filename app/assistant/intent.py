"""Fast-path intent routing: greetings and small talk skip RAG entirely.

Retrieval and generation exist to answer questions about CricLab. "Hi" is not
one. Running an embedding call, a Mongo scan and an LLM generation to answer
"hello" wastes the two things a chat UI is judged on hardest — latency and
looking sensible — for zero benefit: nothing in the knowledge base makes
"hey" a better-answered question.

This runs as a cheap, regex-only classifier *before* anything else in the
pipeline is touched, matching the intent-detection step CricLab's own product
brief calls for:

    User Message → Intent Detection → Casual/Greeting → Direct Response
                                     → CricLab Query   → RAG → AI Response

Deliberately conservative: a short, greeting-shaped message is fast-pathed;
anything with real content past the pleasantry ("hi, why wasn't my ball
tracked?") falls through to RAG like any other question, because pattern-
matching to a canned reply there would answer the greeting and ignore the
actual question.
"""

from __future__ import annotations

import random
import re
from typing import Literal

Intent = Literal["greeting", "thanks", "farewell", "query"]

# Under this length, a message is a plausible standalone pleasantry. Above it,
# assume there's a real question riding along even if it opens with "hi".
_MAX_CASUAL_CHARS = 24

_GREETING_RE = re.compile(
    r"^(hi+|hello+|hey+|yo|sup|howdy|good\s?(morning|afternoon|evening)|greetings)[!.\s]*$",
    re.IGNORECASE,
)
_THANKS_RE = re.compile(
    r"^(thanks|thank\s?you|thx|ta|cheers|appreciate\s?it|nice\s?one|great|awesome|perfect|cool)[!.\s]*$",
    re.IGNORECASE,
)
_FAREWELL_RE = re.compile(
    r"^(bye|goodbye|see\s?ya|see\s?you|later|farewell|night|good\s?night)[!.\s]*$",
    re.IGNORECASE,
)
# Standalone, content-free check-ins — routed like a greeting, not answered
# as if they were a real question about CricLab.
_CHECKIN_RE = re.compile(
    r"^(how\s?(are\s?you|('|’)s\s?it\s?going)|what('|’)s\s?up|you\s?(ok|okay)\??)[!.\s]*$",
    re.IGNORECASE,
)

GREETINGS = [
    "Hey! I'm the CricLab assistant. Ask me anything about filming a clip, what a number in your report means, booking a coach, or your account.\n\n## Go here\n- [Open Video Analysis](/app/action)\n- [Open Filming Guide](/record)\n- [Open Coaching](/app/coaching)",
    "Hi — I can walk you through filming, your results, drills, or a coaching booking.\n\n## Go here\n- [Open Video Analysis](/app/action)\n- [Open Ball Flight](/app/ball-flight)\n- [Open Filming Guide](/record)",
    "Hello! Tell me what you want to do in CricLab and I will take it step by step.\n\n## Go here\n- [Open Video Analysis](/app/action)\n- [Open Help / FAQ](/faq)",
]
THANKS = [
    "You're welcome! Anything else I can help with?",
    "Anytime! Let me know if anything else comes up.",
    "Glad that helped — happy to look at anything else.",
]
FAREWELLS = [
    "Bye for now! Come back any time you have a CricLab question.",
    "See you — good luck with the next net session.",
]
CHECKINS = [
    "Doing well, thanks for asking! I'm here to help with anything CricLab — what's on your mind?",
]


def classify(message: str) -> Intent:
    text = (message or "").strip()
    if not text or len(text) > _MAX_CASUAL_CHARS:
        return "query"
    if _GREETING_RE.match(text):
        return "greeting"
    if _THANKS_RE.match(text):
        return "thanks"
    if _FAREWELL_RE.match(text):
        return "farewell"
    if _CHECKIN_RE.match(text):
        return "greeting"
    return "query"


def direct_reply(intent: Intent) -> str:
    bank = {"greeting": GREETINGS, "thanks": THANKS, "farewell": FAREWELLS}.get(intent, CHECKINS)
    return random.choice(bank)
