import json
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app.db import get_session
from app.models import ConversationLog
from app.security import require_admin_api_key

router = APIRouter(
    prefix="/logs",
    tags=["logs"],
    dependencies=[Depends(require_admin_api_key)],
)

SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/")
def list_logs(session: SessionDep):
    statement = select(ConversationLog)
    logs = session.exec(statement).all()

    result = []
    for log in logs:
        result.append(
            {
                "id": log.id,
                "session_id": log.session_id,
                "user_message": log.user_message,
                "extracted_order": json.loads(log.extracted_order_json),
                "merged_order": json.loads(log.merged_order_json),
                "response_message": log.response_message,
                "valid": log.valid,
                "missing_items": json.loads(log.missing_items_json),
                "state": log.state,
            }
        )

    return result
