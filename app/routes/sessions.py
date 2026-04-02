import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import ConversationSession
from app.schemas import SessionCreateRequest, SessionCreateResponse, SessionRead

router = APIRouter(prefix="/sessions", tags=["sessions"])

SessionDep = Annotated[Session, Depends(get_session)]


@router.post("/", response_model=SessionCreateResponse)
def create_session(body: SessionCreateRequest, session: SessionDep):
    new_session_id = str(uuid.uuid4())

    conversation = ConversationSession(
        session_id=new_session_id,
        customer_name=None,
        customer_phone=body.test_phone or body.caller_phone or None,
        pickup_time=None,
        items_json="[]",
        state="collecting_items",
        completed=False,
    )

    session.add(conversation)
    session.commit()
    session.refresh(conversation)

    return SessionCreateResponse(
        session_id=conversation.session_id,
        state=conversation.state,
        completed=conversation.completed,
    )


@router.get("/{session_id}", response_model=SessionRead)
def get_session_state(session_id: str, session: SessionDep):
    statement = select(ConversationSession).where(
        ConversationSession.session_id == session_id
    )
    conversation = session.exec(statement).first()

    if not conversation:
        raise HTTPException(status_code=404, detail="Session not found")

    return SessionRead(
        session_id=conversation.session_id,
        customer_name=conversation.customer_name,
        customer_phone=conversation.customer_phone,
        pickup_time=conversation.pickup_time,
        items=json.loads(conversation.items_json),
        completed=conversation.completed,
        state=conversation.state,
    )


@router.delete("/{session_id}")
def delete_session(session_id: str, session: SessionDep):
    statement = select(ConversationSession).where(
        ConversationSession.session_id == session_id
    )
    conversation = session.exec(statement).first()

    if not conversation:
        raise HTTPException(status_code=404, detail="Session not found")

    session.delete(conversation)
    session.commit()

    return {"message": f"Session '{session_id}' deleted successfully"}