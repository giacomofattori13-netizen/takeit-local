import os
import secrets

from fastapi import Header, HTTPException, status


def require_admin_api_key(x_admin_api_key: str | None = Header(default=None)) -> None:
    """Protegge gli endpoint amministrativi con una API key configurata via env."""
    expected = os.getenv("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_API_KEY non configurata",
        )
    if not x_admin_api_key or not secrets.compare_digest(x_admin_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key amministrativa non valida",
        )
