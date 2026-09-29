"""Isolamento dei test dai servizi reali.

app.services.conversation_service chiama load_dotenv() all'import, quindi senza
questo file i test userebbero le credenziali del .env e un percorso non simulato
scriverebbe su Base44 di produzione o invierebbe SMS/WhatsApp veri.
"""
import httpx
import pytest

_SECRET_ENV_VARS = (
    "BASE44_TOKEN",
    "BASE44_API_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_NUMBER",
    "TWILIO_WHATSAPP_FROM",
    "OWNER_PHONE",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ELEVENLABS_API_KEY",
)


class NetworkBlockedError(RuntimeError):
    pass


def _blocked(self, request, *args, **kwargs):
    raise NetworkBlockedError(f"Richiesta di rete reale nei test: {request.method} {request.url}")


@pytest.fixture(autouse=True)
def _isolate_from_real_services(monkeypatch):
    # I test che servono una credenziale la impostano da sé in setUp (dopo questo fixture).
    for name in _SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Qualsiasi richiesta httpx non simulata fallisce invece di uscire in rete.
    # Il TestClient di FastAPI usa un proprio transport e non è toccato.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)
