import uuid
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Form
from fastapi.responses import Response
from sqlmodel import Session

from app.db import get_session
from app.models import ConversationSession
from app.services.conversation_service import get_agent_greeting

router = APIRouter(prefix="/voice", tags=["voice"])


@router.post("/incoming")
def voice_incoming(
    From: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Webhook Twilio Voice: crea sessione, risponde con saluto + Gather speech."""
    caller_phone = From.strip() or None
    print(f"[Voice] Chiamata in arrivo da: {caller_phone!r}")

    session_id = str(uuid.uuid4())
    conversation = ConversationSession(
        session_id=session_id,
        customer_phone=caller_phone,
        items_json="[]",
        state="collecting_items",
        completed=False,
    )
    session.add(conversation)
    session.commit()
    print(f"[Voice] Sessione creata: {session_id}")

    greeting = get_agent_greeting()
    print(f"[Voice] Saluto: {greeting!r}")

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'  <Gather input="speech" language="it-IT" speechTimeout="auto" '
        f'action="/voice/gather?session_id={session_id}" method="POST">\n'
        f"    <Say voice=\"Polly.Bianca\">{escape(greeting)}</Say>\n"
        "  </Gather>\n"
        '  <Say voice="Polly.Bianca">Non ho sentito nulla. Riprovi a chiamare, grazie.</Say>\n'
        "</Response>"
    )

    return Response(content=twiml, media_type="application/xml")
