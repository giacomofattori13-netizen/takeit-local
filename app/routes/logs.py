import json
from copy import deepcopy
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.db import get_session
from app.models import ConversationLog
from app.security import require_admin_api_key
from app.telemetry import get_latency_snapshot

router = APIRouter(
    prefix="/logs",
    tags=["logs"],
    dependencies=[Depends(require_admin_api_key)],
)

SessionDep = Annotated[Session, Depends(get_session)]


def _safe_json_loads(raw_value: str | None, fallback):
    if not raw_value:
        return deepcopy(fallback)

    try:
        return json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return deepcopy(fallback)


@router.get("/latency")
def latency_metrics():
    return get_latency_snapshot()


@router.get("/")
def list_logs(
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session_id: str | None = Query(default=None),
):
    statement = select(ConversationLog).order_by(ConversationLog.id.desc())
    if session_id:
        statement = statement.where(ConversationLog.session_id == session_id)
    statement = statement.offset(offset).limit(limit)
    logs = session.exec(statement).all()

    result = []
    for log in logs:
        result.append(
            {
                "id": log.id,
                "session_id": log.session_id,
                "user_message": log.user_message,
                "extracted_order": _safe_json_loads(log.extracted_order_json, {}),
                "merged_order": _safe_json_loads(log.merged_order_json, {}),
                "response_message": log.response_message,
                "valid": log.valid,
                "missing_items": _safe_json_loads(log.missing_items_json, []),
                "state": log.state,
            }
        )

    return result
