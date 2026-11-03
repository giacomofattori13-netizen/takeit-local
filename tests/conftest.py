"""Isolamento dei test dai servizi reali.

app.services.conversation_service chiama load_dotenv() all'import, quindi senza
questo file i test userebbero le credenziali del .env e un percorso non simulato
scriverebbe su Base44 di produzione o invierebbe SMS/WhatsApp veri.
"""
import httpx
import pytest

# Unico host raggiungibile dai test marcati llm_live: Base44 e Twilio restano bloccati.
_LIVE_LLM_HOST = "api.openai.com"

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


_real_handle_request = httpx.HTTPTransport.handle_request


def _blocked(self, request, *args, **kwargs):
    raise NetworkBlockedError(f"Richiesta di rete reale nei test: {request.method} {request.url}")


def _blocked_except_llm(self, request, *args, **kwargs):
    if request.url.host == _LIVE_LLM_HOST:
        return _real_handle_request(self, request, *args, **kwargs)
    return _blocked(self, request)


def pytest_addoption(parser):
    parser.addoption(
        "--run-llm", action="store_true", default=False,
        help="Esegue anche i test marcati llm_live, che chiamano l'LLM vero (costano e usano la rete).",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "llm_live: chiama l'LLM vero; gira solo con --run-llm")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-llm"):
        return
    skip = pytest.mark.skip(reason="LLM vero: serve --run-llm")
    for item in items:
        if item.get_closest_marker("llm_live"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolate_from_real_services(monkeypatch, request):
    # I test che servono una credenziale la impostano da sé in setUp (dopo questo fixture).
    live_llm = request.node.get_closest_marker("llm_live") is not None
    for name in _SECRET_ENV_VARS:
        if not (live_llm and name == "OPENAI_API_KEY"):
            monkeypatch.delenv(name, raising=False)
    # Qualsiasi richiesta httpx non simulata fallisce invece di uscire in rete.
    # Il TestClient di FastAPI usa un proprio transport e non è toccato.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked_except_llm if live_llm else _blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)
