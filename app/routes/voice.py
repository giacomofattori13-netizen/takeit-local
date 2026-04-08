import uuid
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import Response
from sqlmodel import Session

from app.db import get_session
from app.models import ConversationSession
from app.schemas import ChatRequest
from app.services.conversation_service import get_agent_greeting

router = APIRouter(prefix="/voice", tags=["voice"])

_POLLY = "Polly.Bianca"
_GATHER_ATTRS = 'input="speech" language="it-IT" speechTimeout="auto"'
_NO_INPUT_MSG = "Non ho sentito nulla. Riprovi a chiamare, grazie."


def _twiml_gather(session_id: str, say_text: str) -> str:
    """TwiML che dice qualcosa e rimane in ascolto per il turno successivo."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"  <Gather {_GATHER_ATTRS} "
        f'action="/voice/gather?session_id={session_id}" method="POST">\n'
        f'    <Say voice="{_POLLY}">{escape(say_text)}</Say>\n'
        "  </Gather>\n"
        f'  <Say voice="{_POLLY}">{escape(_NO_INPUT_MSG)}</Say>\n'
        "</Response>"
    )


def _twiml_end(say_text: str) -> str:
    """TwiML che dice qualcosa e chiude la chiamata."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'  <Say voice="{_POLLY}">{escape(say_text)}</Say>\n'
        "  <Hangup/>\n"
        "</Response>"
    )


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

    return Response(content=_twiml_gather(session_id, greeting), media_type="application/xml")


@router.post("/gather")
def voice_gather(
    session_id: str = Query(...),
    SpeechResult: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Riceve il testo trascritto da Twilio, lo passa al motore di chat
    e risponde con TwiML per far sentire la risposta al cliente."""
    # Import locale per evitare import circolare (chat importa da conversation_service)
    from app.routes.chat import chat  # noqa: PLC0415

    speech = SpeechResult.strip()
    print(f"[Voice] Gather session={session_id!r} speech={speech!r}")

    if not speech:
        twiml = _twiml_gather(session_id, "Non ho capito. Può ripetere?")
        return Response(content=twiml, media_type="application/xml")

    chat_request = ChatRequest(session_id=session_id, message=speech)
    result = chat(chat_request, session)

    reply = result.response_message
    print(f"[Voice] Risposta agente: {reply!r} stato={result.state!r}")

    if result.state == "completed":
        twiml = _twiml_end(reply)
    else:
        twiml = _twiml_gather(session_id, reply)

    return Response(content=twiml, media_type="application/xml")
