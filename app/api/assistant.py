"""CricLab assistant endpoints.

Open to signed-out visitors — someone reading the site should be able to ask
"how do I film this?" without an account. Rate limiting is therefore keyed on
the user when there is one and the client address when there is not.

Conversation history comes from the client rather than being stored. There is
nothing worth keeping in it, and not storing it is one less place a person's
question can leak from.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, Client, OptionalUser, enforce_rate_limit
from app.assistant import chat, rag

log = logging.getLogger("criclab.assistant")
router = APIRouter(prefix="/assistant", tags=["assistant"])


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=2000)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=800)
    history: list[Turn] = Field(default_factory=list, max_length=12)


@router.get("/starters")
async def starters():
    """Opening suggestions for an empty chat."""
    return {"starters": chat.STARTERS}


@router.post("/ask")
async def ask(payload: AskIn, user: OptionalUser, client: Client):
    key = f"assistant:{user['_id']}" if user else f"assistant:ip:{client.ip or 'unknown'}"
    await enforce_rate_limit(
        key,
        limit=40 if user else 15,
        window_seconds=600,
        message="That is a lot of questions at once. Give it a minute.",
    )
    result = await chat.answer(
        payload.question,
        history=[t.model_dump() for t in payload.history],
        # First name only — enough to be civil, and it is already their session.
        user_name=(user.get("name", "").split(" ")[0] if user else None),
    )
    return result


@router.post("/ask/stream")
async def ask_stream(payload: AskIn, user: OptionalUser, client: Client):
    """Same answer as `/ask`, delivered as it is generated.

    Newline-delimited JSON rather than SSE — one line, one event, trivial to
    parse on either side, with none of SSE's `event:`/`data:` framing to get
    wrong for a body this simple.
    """
    key = f"assistant:{user['_id']}" if user else f"assistant:ip:{client.ip or 'unknown'}"
    await enforce_rate_limit(
        key,
        limit=40 if user else 15,
        window_seconds=600,
        message="That is a lot of questions at once. Give it a minute.",
    )

    async def events():
        async for event in chat.answer_stream(
            payload.question,
            history=[t.model_dump() for t in payload.history],
            user_name=(user.get("name", "").split(" ")[0] if user else None),
        ):
            yield json.dumps(event) + "\n"

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex(_: AdminUser, force: bool = False):
    """Re-embed the knowledge base after an edit."""
    return await rag.build_index(force=force)
