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

Casual messages ("hi", "thanks", "bye") never reach retrieval or the model
either — see `intent.py`. Running an embedding call and a generation to answer
"hi" is pure latency with no upside, so those are answered directly.
"""

from __future__ import annotations

import logging
import re
from typing import Any, AsyncIterator

from app.agent.ollama_agent import generate_text, generate_text_stream
from app.assistant import intent as intent_router
from app.assistant import rag

log = logging.getLogger("criclab.assistant")

MAX_QUESTION_CHARS = 800
MAX_HISTORY_TURNS = 6
# Someone is watching a typing indicator. Past this it is better to answer
# from the passages directly than to keep them waiting.
GENERATION_TIMEOUT_S = 25.0
STREAM_TIMEOUT_S = 45.0
# How much streamed text to buffer before it is scanned and flushed to the
# client. Smaller means a more immediate-feeling stream; larger means the
# leak-scan (see `_leaks`) sees more context per check. This is a tuning knob,
# not a security boundary — the boundary is that nothing reaches the client
# without passing the scan first.
STREAM_FLUSH_CHARS = 48

# Real, internal navigation the assistant is allowed to point at. Grounding
# these in a fixed table — rather than letting the model invent a path — is
# what makes "Open Coaching" a real link and not a plausible-looking guess.
# The topic hint exists because a label and a path alone were not enough: an
# early version of this prompt got "Open Coaching" appended to answers about
# forgotten passwords and untracked balls, because the model latched onto the
# one example shown in the instructions rather than picking per the question.
PAGE_LINKS: list[tuple[str, str, str]] = [
    ("Video Analysis", "/app", "uploading a clip, starting a new analysis"),
    ("Ball Flight", "/app/ball-flight", "line, length or ball-flight specific analysis"),
    ("Analytics / History", "/app/history", "past sessions, comparing deliveries over time"),
    ("Drill Library", "/app/train", "practice drills"),
    ("Coaching", "/app/coaching", "booking, rescheduling or cancelling a coach session"),
    ("Support", "/app/support", "reporting a problem, a ticket, something not working"),
    ("Account Settings", "/app/settings", "password, profile, email, signed-in devices"),
    ("Filming Guide", "/record", "how to film a clip"),
    ("Help / FAQ", "/faq", "a general question this table does not otherwise cover"),
    ("Pricing", "/pricing", "plans, cost, billing"),
]
_ALLOWED_HREFS = {path for _, path, _ in PAGE_LINKS} | {"/contact", "/features", "/how-it-works"}
_HREF_TO_LABEL = {path: f"Open {label}" for label, path, _ in PAGE_LINKS}
_PAGE_TABLE_TEXT = "\n".join(f"- {label}: {path} — for questions about {when}" for label, path, when in PAGE_LINKS)

SYSTEM_PROMPT = f"""You are the CricLab AI Assistant. You help people use CricLab: \
filming clips, understanding their numbers, their account, support and coaching \
bookings. You are talking directly to a user inside the product, not writing \
documentation, so guide them to the right screen rather than only describing it.

Rules you follow without exception:

- Answer ONLY from the reference passages given to you. They are the only thing \
you know about CricLab. Never guess, and never fill a gap with something that \
sounds plausible.
- If the passages do not answer the question, say plainly that you do not have \
that information and suggest opening a support ticket. Do not pad this out — \
say it in one line and stop.
- Never discuss, describe or speculate about how CricLab is built, hosted or \
implemented. That includes any technology, service, model, algorithm, database \
or internal process. If asked, say it is not something you can go into, and \
offer to help with using CricLab instead.
- Never state prices, plan limits or figures that are not in the passages.
- You cannot see the user's account, footage, bookings or billing. Do not \
pretend to.

Formatting — this renders as Markdown in a chat bubble, use it properly:
- Prefer short paragraphs (1-3 sentences) over one long block.
- Say each thing once. Do not open with a `**Tip:**` that restates the answer \
you are about to give in full below it — a tip adds something extra, it does \
not preview the paragraph that follows.
- Use a numbered list for anything that is genuinely a sequence of steps.
- Use a short bold lead-in (`**Tip:**`, `**Note:**`) for one-line asides rather \
than folding them into the main paragraph.
- Use `**bold**` for the two or three words that matter most in a sentence, not \
whole sentences.
- Use a heading (`## `) only for an answer long enough to actually need one — \
most answers do not.
- Never use a code block or inline backticks unless the content genuinely is \
code, a file path, or an exact field name.

Linking to CricLab itself — this is what makes an answer actionable instead of \
descriptive:
- When, and only when, the answer's next step is one specific screen in \
CricLab, end with ONE Markdown link to that screen, formatted as \
`[Open <Page Name>](<path>)` using the exact label and path from the table \
below — never a bare URL, never "click here", and never a path not in the table.
- The page you link to MUST match what THIS answer is actually about. A \
question about a forgotten password links to Account Settings; a question \
about the ball not tracking links to nothing at all, because there is no \
single screen that fixes it. Getting this wrong — linking to a page unrelated \
to the question, for example ending every answer with the same Coaching link \
out of habit — is a worse mistake than adding no link, because it sends the \
user to the wrong place with false confidence.
- Most answers do not end with a link at all. Only add one when a specific \
page is genuinely the next thing to click — not as a sign-off.

Path table — the ONLY paths you may ever use, each with the question-topic it \
actually belongs to:
{_PAGE_TABLE_TEXT}
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
_LEAK_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in _LEAK_TERMS) + r")\b", re.IGNORECASE)

# A generated link to anything not in PAGE_LINKS is stripped rather than shown
# — the same "grounded, not guessed" rule that applies to prose applies to links.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

DEFLECTION = (
    "That is not something I can go into — how CricLab is built is kept private. "
    "I can help with anything about using it though: filming a clip that reads "
    "well, what a number in your report means, your account, support, or booking "
    "a coaching session."
)

NO_ANSWER = (
    "I do not have anything on that, and I would rather say so than guess. "
    "Open a [support ticket](/app/support) and someone will pick it up — that is "
    "the quickest route for anything specific to your account."
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
    m = _LEAK_RE.search(answer)
    return m.group(1).lower() if m else None


def _sanitize_links(answer: str) -> str:
    """Drop any Markdown link whose href the model was not given.

    Rebuilds the link from the trimmed href rather than returning the match
    untouched — the model sometimes pads the parenthetical with a stray space
    (`( /app )`), which is harmless in prose but not a path a router should
    receive verbatim.
    """

    def repl(m: re.Match) -> str:
        text, href = m.group(1), m.group(2).strip()
        if href in _ALLOWED_HREFS:
            return f"[{text}]({href})"
        log.warning("assistant link stripped: %r -> %r not in allow-list", text, href)
        return text

    return _MD_LINK_RE.sub(repl, answer)


def _humanize_link_text(text: str) -> str:
    """Replace `[/app/settings](/app/settings)` with `[Open Account Settings](/app/settings)`.

    Asked for a Markdown link, a small model sometimes uses the path itself as
    the link text — technically valid Markdown, but exactly the raw-URL-as-
    link-text the assistant is meant to avoid. Only fires when the text is
    literally the href (not a false-positive on ordinary prose).
    """

    def repl(m: re.Match) -> str:
        label, href = m.group(1).strip(), m.group(2).strip()
        # Catches both "[/app/support]" and "[Open /app/support]" — the path
        # showing up inside the label at all is the tell, not just an exact
        # match, since the model sometimes keeps a word like "Open" in front.
        if href in _HREF_TO_LABEL and (href in label or href.lstrip("/") in label):
            return f"[{_HREF_TO_LABEL[href]}]({href})"
        return m.group(0)

    return _MD_LINK_RE.sub(repl, text)


# A small model occasionally writes a complete, valid link and then — a
# token-repetition quirk, not a formatting choice — immediately writes the
# same href again in a bare trailing "(...)" right after it: "[Open X](/app)
# (/app)". Collapsed here rather than prevented, since instructing a 4B model
# not to repeat itself has limited effect.
_DUPLICATE_HREF_RE = re.compile(r"(\]\(([^)]+)\))\(\s*([^)]*)\s*\)")


def _collapse_duplicate_href(text: str) -> str:
    def repl(m: re.Match) -> str:
        whole, href, maybe_dup = m.group(1), m.group(2).strip(), m.group(3).strip()
        return whole if maybe_dup == href else m.group(0)

    return _DUPLICATE_HREF_RE.sub(repl, text)


def _last_link_href(text: str) -> str | None:
    """The href of the last Markdown link in `text`, if any."""
    last = None
    for m in _MD_LINK_RE.finditer(text):
        last = m.group(2).strip()
    return last


def _strip_leading_duplicate(text: str, last_href: str | None) -> str:
    """Cross-flush counterpart to `_collapse_duplicate_href`.

    A duplicate `(href)` following a just-completed link can land in the
    *next* streamed chunk instead of the same one — `_collapse_duplicate_href`
    only sees one chunk at a time, so it never catches that split. This
    catches it using the href carried over from the previous flush.
    """
    if not last_href:
        return text
    return re.sub(r"^\s*\(\s*" + re.escape(last_href) + r"\s*\)", "", text, count=1)


# A 4B local model asked to write "[Open Coaching](/app/coaching)" will,
# often enough, write "[Open Coaching]" and stop — the intent is right, the
# syntax just didn't make it. Rather than police the model harder, complete
# what it clearly meant: match the label against the same table it was given
# and fill in the href deterministically. Order matters — more specific labels
# must be checked before the generic ones they'd otherwise be swallowed by.
_LABEL_HREF_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bvideo analysis\b|\bdashboard\b|\bupload\b", re.IGNORECASE), "/app"),
    (re.compile(r"\bball flight\b", re.IGNORECASE), "/app/ball-flight"),
    (re.compile(r"\banalytics\b|\bhistory\b|\bpast sessions?\b", re.IGNORECASE), "/app/history"),
    (re.compile(r"\bdrill(s|\s+library)?\b|\btrain(ing)?\b", re.IGNORECASE), "/app/train"),
    (re.compile(r"\bcoach(ing)?\b|\bbook(ings?|\s+a\s+session)?\b", re.IGNORECASE), "/app/coaching"),
    (re.compile(r"\bsupport\b|\bticket\b", re.IGNORECASE), "/app/support"),
    (re.compile(r"\baccount\b|\bsettings\b|\bpassword\b|\bprofile\b", re.IGNORECASE), "/app/settings"),
    (re.compile(r"\bfilming guide\b|\brecord(ing)?\s+a?\s*video\b", re.IGNORECASE), "/record"),
    (re.compile(r"\bhelp\b|\bfaq\b", re.IGNORECASE), "/faq"),
    (re.compile(r"\bpricing\b|\bplans?\b", re.IGNORECASE), "/pricing"),
]
_BARE_BRACKET_RE = re.compile(r"\[([^\[\]]{2,48})\](?!\()")


def _split_before_open_bracket(buffer: str) -> tuple[str, str]:
    """Split `buffer` so nothing flushed ends mid-way through Markdown link syntax.

    Returns `(flushable, carry)`; `carry` is held back for the next chunk to
    complete. Three cases, checked in order:

    1. An unclosed `[` — the label itself is still arriving.
    2. A `]` sitting at the very end of the buffer — ambiguous, because the
       next token might be `(`, turning this into `[label](href)`. Held back
       rather than repairing a "bare" link that turns out not to be one.
    3. An unclosed `(` — the model wrote a real href and it got split.
    """
    last_open = buffer.rfind("[")
    if last_open != -1 and "]" not in buffer[last_open:]:
        return buffer[:last_open], buffer[last_open:]

    if buffer.endswith("]"):
        start = buffer.rfind("[", 0, len(buffer) - 1)
        if start != -1:
            return buffer[:start], buffer[start:]

    last_paren = buffer.rfind("(")
    if last_paren != -1 and ")" not in buffer[last_paren:]:
        return buffer[:last_paren], buffer[last_paren:]

    return buffer, ""


def _repair_bare_links(text: str) -> str:
    """Turn a bracketed page reference the model left unlinked into a real link."""

    def repl(m: re.Match) -> str:
        label = m.group(1)
        for pattern, href in _LABEL_HREF_HINTS:
            if pattern.search(label):
                return f"[{label}]({href})"
        return m.group(0)  # not a recognisable page reference — leave as plain text

    return _BARE_BRACKET_RE.sub(repl, text)


def _extractive(passages: list[dict[str, Any]]) -> str:
    """A grounded answer without a model: the best passage, lightly tidied.

    Used when no language model is reachable, or when a generated answer had
    to be withheld. Less conversational, but every word of it came from the
    knowledge base, which is the property that matters.
    """
    if not passages:
        return NO_ANSWER
    body = passages[0]["body"]
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


def _build_prompt(question: str, passages: list[dict[str, Any]], history, user_name: str | None) -> str:
    reference = "\n\n---\n\n".join(
        f"[{i + 1}] {p['title']}\n{p['body']}" for i, p in enumerate(passages)
    )
    conversation = _history_text(history)
    return (
        f"Reference passages:\n\n{reference}\n\n"
        + (f"Conversation so far:\n{conversation}\n\n" if conversation else "")
        + f"Question{f' from {user_name}' if user_name else ''}: {question}\n\n"
        "Answer using only the passages above."
    )


async def answer(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    user_name: str | None = None,
) -> dict[str, Any]:
    """Answer one question in a single response. Always returns; never raises."""
    question = (question or "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        return {"answer": "Ask me anything about using CricLab.", "sources": [], "grounded": False}

    detected = intent_router.classify(question)
    if detected != "query":
        return {
            "answer": intent_router.direct_reply(detected),
            "sources": [],
            "grounded": False,
            "intent": detected,
        }

    if _asks_about_internals(question):
        return {"answer": DEFLECTION, "sources": [], "grounded": False, "refused": True}

    passages = await rag.retrieve(question)
    if not passages:
        return {"answer": NO_ANSWER, "sources": [], "grounded": False, "escalate": True}

    prompt = _build_prompt(question, passages, history, user_name)
    try:
        generated = (await generate_text(
            prompt, system=SYSTEM_PROMPT, num_predict=380, timeout_s=GENERATION_TIMEOUT_S
        )).strip()
    except Exception as exc:
        log.info("assistant generation unavailable (%s)", type(exc).__name__)
        generated = ""

    if generated:
        leaked = _leaks(generated)
        if leaked:
            log.warning("assistant answer withheld: mentioned %r", leaked)
            generated = ""
        else:
            generated = _collapse_duplicate_href(_repair_bare_links(_humanize_link_text(_sanitize_links(generated))))

    return {
        "answer": generated or _extractive(passages),
        "sources": rag.sources_of(passages),
        "grounded": True,
        "generated": bool(generated),
    }


async def answer_stream(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    user_name: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Answer as a sequence of events: one `meta`, zero or more `delta`, one `done`.

    A `redacted` event instead of the final `delta`/`done` pair means a leak
    check failed partway through — the client discards whatever partial text
    it had buffered from this turn and shows the replacement `answer` instead.
    """
    question = (question or "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        yield {"type": "meta", "sources": [], "grounded": False}
        yield {"type": "delta", "text": "Ask me anything about using CricLab."}
        yield {"type": "done"}
        return

    detected = intent_router.classify(question)
    if detected != "query":
        yield {"type": "meta", "sources": [], "grounded": False, "intent": detected}
        yield {"type": "delta", "text": intent_router.direct_reply(detected)}
        yield {"type": "done"}
        return

    if _asks_about_internals(question):
        yield {"type": "meta", "sources": [], "grounded": False, "refused": True}
        yield {"type": "delta", "text": DEFLECTION}
        yield {"type": "done"}
        return

    passages = await rag.retrieve(question)
    if not passages:
        yield {"type": "meta", "sources": [], "grounded": False, "escalate": True}
        yield {"type": "delta", "text": NO_ANSWER}
        yield {"type": "done"}
        return

    yield {"type": "meta", "sources": rag.sources_of(passages), "grounded": True}

    prompt = _build_prompt(question, passages, history, user_name)
    buffer = ""
    sent_any = False
    leaked_term: str | None = None
    # Tracks the href of the last link flushed, so a duplicate parenthetical
    # that lands in the *next* flush — the boundary case `_collapse_duplicate_href`
    # cannot see on its own — is still caught. See `_strip_leading_duplicate`.
    last_href: str | None = None

    def clean(raw: str) -> str:
        return _collapse_duplicate_href(
            _repair_bare_links(_humanize_link_text(_sanitize_links(raw)))
        )

    try:
        async for piece in generate_text_stream(
            prompt, system=SYSTEM_PROMPT, num_predict=380, timeout_s=STREAM_TIMEOUT_S
        ):
            buffer += piece
            if len(buffer) < STREAM_FLUSH_CHARS:
                continue
            # A `[Label]` can straddle two flushes — hold back an unclosed
            # trailing bracket rather than sending half a link two chunks in a
            # row, which `_repair_bare_links` cannot reassemble after the fact.
            flushable, buffer = _split_before_open_bracket(buffer)
            if not flushable:
                continue
            leaked_term = _leaks(flushable)
            if leaked_term:
                break
            cleaned = _strip_leading_duplicate(clean(flushable), last_href)
            # Only an *immediately* adjacent duplicate is the model's repetition
            # quirk; carrying `last_href` past a flush with no link of its own
            # would risk stripping an unrelated parenthetical later by coincidence.
            last_href = _last_link_href(cleaned)
            if cleaned:
                yield {"type": "delta", "text": cleaned}
                sent_any = True
        if not leaked_term and buffer:
            leaked_term = _leaks(buffer)
            if not leaked_term:
                cleaned = _strip_leading_duplicate(clean(buffer), last_href)
                if cleaned:
                    yield {"type": "delta", "text": cleaned}
                    sent_any = True
                buffer = ""
    except Exception as exc:
        log.info("assistant stream unavailable (%s)", type(exc).__name__)

    if leaked_term:
        log.warning("assistant stream withheld: mentioned %r", leaked_term)
        yield {"type": "redacted", "answer": _extractive(passages)}
        yield {"type": "done"}
        return

    if not sent_any:
        # The model never produced anything usable — fall back exactly like
        # the non-streaming path does.
        yield {"type": "delta", "text": _extractive(passages)}

    yield {"type": "done"}
