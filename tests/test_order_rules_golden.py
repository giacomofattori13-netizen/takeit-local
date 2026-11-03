"""Regressione delle regole d'ordine: frasi reali → risultato atteso.

Ogni caso indica prodotti, quantità, aggiunte, rimozioni, impasto, giorno, orario e nome
che devono trovarsi nella sessione dopo la frase, con il menu reale di Corte del Sole
(tests/fixtures/cds_menu.json).

Due modi di eseguire gli stessi casi:

    pytest tests/test_order_rules_golden.py             # estrazione registrata, senza rete
    pytest tests/test_order_rules_golden.py --run-llm   # anche LLM vero: 5 run per caso

Con l'LLM vero un caso fallisce se anche una sola run sbaglia.
"""
import ast
import datetime
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

import app.routes.chat as chat_module
from app.models import ConversationSession, MenuItem
from app.schemas import ChatRequest
from app.services import conversation_service as service
from tests.test_opening_hours_flow import _frozen_datetime_module

LIVE_RUNS = 5
ROME = ZoneInfo("Europe/Rome")
RID = "rest-golden"
MENU = json.loads((Path(__file__).parent / "fixtures" / "cds_menu.json").read_text(encoding="utf-8"))
DOUGHS = json.loads((Path(__file__).parents[1] / "app" / "dough_data.json").read_text(encoding="utf-8"))
ALL_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
CDS = {"id": RID, "name": "Corte Del Sole", "min_prep_minutes": 30, "opening_hours": {d: "19:00-22:00" for d in ALL_DAYS}}
MONDAY_CLOSED = {**CDS, "opening_hours": {**CDS["opening_hours"], "monday": ""}}

THURSDAY_16 = datetime.datetime(2026, 10, 1, 16, 0, tzinfo=ROME)  # domani venerdì 2
SUNDAY_16 = datetime.datetime(2026, 10, 4, 16, 0, tzinfo=ROME)  # domani lunedì 5
THURSDAY_23 = datetime.datetime(2026, 10, 1, 23, 0, tzinfo=ROME)  # cucina chiusa
TONIGHT = None  # pickup_date vuoto: ritiro oggi
TOMORROW = "2026-10-02"
PERSONAL = "Personalizzata"  # Margherita base + nome detto come aggiunta


def item(name, quantity=1, add=(), remove=(), dough="classica", size="normale"):
    return {"name": name, "quantity": quantity, "add": list(add), "remove": list(remove), "dough": dough, "size": size}


def recorded(intent="add_items", items=(), name=None, time=None):
    """Estrazione registrata dall'LLM, per la versione senza rete."""
    return {
        "intent": intent, "customer_name": name, "pickup_time": time,
        "items": [
            {
                "pizza_name": i["name"], "quantity": i["quantity"], "add_ingredients": i["add"],
                "remove_ingredients": i["remove"], "dough_type": i["dough"], "pizza_type": "Normale",
                "size": i["size"], "temperature": "", "order_unit": "",
            }
            for i in items
        ],
    }


@dataclass
class Case:
    id: str
    message: str
    items: list = field(default_factory=list)
    day: "str | None" = TONIGHT
    time: "str | None" = None
    name: "str | None" = None
    now: datetime.datetime = THURSDAY_16
    restaurant: dict = field(default_factory=lambda: CDS)
    state: str = "collecting_items"  # stato della sessione prima della frase
    cart: list = field(default_factory=list)  # carrello prima della frase
    llm: "dict | None" = None  # estrazione registrata; None = la frase non passa dall'LLM
    reply_contains: "str | None" = None
    final_state: "str | None" = None
    replaces_cart: bool = False  # `items` è il carrello finale, non un'aggiunta

    def __post_init__(self):
        self.cart = self.cart or []


_MARGHERITA = item("Margherita")
CASES = [
    # ── Pizze fuori menu: Margherita base + nome detto come aggiunta ─────────
    Case("salamino_domani_alle_8", "2 salamino da asporto per domani alle 8",
         [item(PERSONAL, 2, add=["salamino"])], day=TOMORROW, time="20:00",
         llm=recorded(items=[item(PERSONAL, 2, add=["salamino"])], time="20:00")),
    Case("melanzane_salamino_gorgonzola", "una melanzane salamino e gorgonzola",
         [item(PERSONAL, add=["melanzane", "salamino", "gorgonzola"])],
         llm=recorded(items=[item(PERSONAL, add=["melanzane", "salamino", "gorgonzola"])])),
    Case("melanzane_gorgonzola", "una melanzane e gorgonzola",
         [item(PERSONAL, add=["melanzane", "gorgonzola"])],
         llm=recorded(items=[item(PERSONAL, add=["melanzane", "gorgonzola"])])),
    Case("wurstel_senza_pomodoro", "una wurstel senza pomodoro",
         [item(PERSONAL, add=["würstel"], remove=["pomodoro"])],
         llm=recorded(items=[item(PERSONAL, add=["würstel"], remove=["pomodoro"])])),
    # Stessa forma con parole diverse: la regola non deve valere solo per la frase del prompt
    Case("salamino_senza_mozzarella", "una salamino senza mozzarella",
         [item(PERSONAL, add=["salamino"], remove=["mozzarella"])],
         llm=recorded(items=[item(PERSONAL, add=["salamino"], remove=["mozzarella"])])),
    Case("due_kebab_senza_cipolla", "due kebab senza cipolla",
         [item(PERSONAL, 2, add=["kebab"], remove=["cipolla"])],
         llm=recorded(items=[item(PERSONAL, 2, add=["kebab"], remove=["cipolla"])])),
    Case("salamino_e_rucola_senza_pomodoro", "una salamino e rucola senza pomodoro",
         [item(PERSONAL, add=["salamino", "rucola"], remove=["pomodoro"])],
         llm=recorded(items=[item(PERSONAL, add=["salamino", "rucola"], remove=["pomodoro"])])),
    Case("porchetta_senza_rucola", "tre porchetta senza rucola",
         [item(PERSONAL, 3, add=["porchetta"], remove=["rucola"])],
         llm=recorded(items=[item(PERSONAL, 3, add=["porchetta"], remove=["rucola"])])),
    Case("speck_senza_origano", "una speck senza origano",
         [item(PERSONAL, add=["speck"], remove=["origano"])],
         llm=recorded(items=[item(PERSONAL, add=["speck"], remove=["origano"])])),
    # ── Pizze del menu: aggiunte, rimozioni, impasti, misure ─────────────────
    Case("capricciosa_con_rucola", "una capricciosa con rucola",
         [item("Capricciosa", add=["rucola"])], llm=recorded(items=[item("Capricciosa", add=["rucola"])])),
    Case("margherita_integrale", "una margherita integrale",
         [item("Margherita", dough="integrale")], llm=recorded(items=[item("Margherita", dough="integrale")])),
    Case("impasto_solo_alla_sua_pizza", "una margherita e una capricciosa integrale",
         [item("Margherita"), item("Capricciosa", dough="integrale")],
         llm=recorded(items=[item("Margherita"), item("Capricciosa", dough="integrale")])),
    Case("impasto_per_tutte", "due margherite e una capricciosa tutte napoletane",
         [item("Margherita", 2, dough="napoletana"), item("Capricciosa", dough="napoletana")],
         llm=recorded(items=[item("Margherita", 2, dough="napoletana"), item("Capricciosa", dough="napoletana")])),
    Case("capricciosa_senza_olive", "una capricciosa senza olive",
         [item("Capricciosa", remove=["olive"])], llm=recorded(items=[item("Capricciosa", remove=["olive"])])),
    Case("margherita_mini", "una margherita mini",
         [item("Margherita", size="mini")], llm=recorded(items=[item("Margherita", size="mini")])),
    Case("capricciosa_doppio_impasto", "una capricciosa doppio impasto",
         [item("Capricciosa", size="doppio")], llm=recorded(items=[item("Capricciosa", size="doppio")])),
    Case("bianca_con_prosciutto", "una bianca con prosciutto",
         [item(PERSONAL, add=["prosciutto"], remove=["pomodoro"])],
         llm=recorded(items=[item(PERSONAL, add=["prosciutto"], remove=["pomodoro"])])),
    Case("aggiunta_e_rimozione", "una capricciosa senza olive con würstel",
         [item("Capricciosa", add=["würstel"], remove=["olive"])],
         llm=recorded(items=[item("Capricciosa", add=["würstel"], remove=["olive"])])),
    Case("doppia_pasta_e_impasto", "una margherita doppia pasta integrale",
         [item("Margherita", dough="integrale", size="doppio")],
         llm=recorded(items=[item("Margherita", dough="integrale", size="doppio")])),
    Case("alias_verde_gorgonzola", "una capricciosa con verde",
         [item("Capricciosa", add=["gorgonzola"])], llm=recorded(items=[item("Capricciosa", add=["gorgonzola"])])),
    # ── Carrello: togli, sostituisci, annulla ────────────────────────────────
    Case("togli_una_pizza", "togli la margherita", [item("Capricciosa")], cart=[_MARGHERITA, item("Capricciosa")],
         replaces_cart=True, llm=recorded("remove_items", items=[_MARGHERITA])),
    Case("togli_l_ultima", "rimuovi l'ultima pizza", [_MARGHERITA], cart=[_MARGHERITA, item("Capricciosa")],
         replaces_cart=True, llm=recorded("remove_items", items=[item("__last__")])),
    Case("sostituisci", "fai due capricciose invece", [item("Capricciosa", 2)], cart=[_MARGHERITA],
         replaces_cart=True, llm=recorded("replace_items", items=[item("Capricciosa", 2)])),
    Case("cancella_tutto", "cancella tutto e ricominciamo", [], cart=[_MARGHERITA], replaces_cart=True,
         llm=recorded("clear_cart")),
    Case("annulla_ordine", "annulla l'ordine", [], cart=[_MARGHERITA], replaces_cart=True, llm=recorded("cancel_order")),
    # ── Orario ───────────────────────────────────────────────────────────────
    Case("alle_9", "alle 9", cart=[_MARGHERITA], time="21:00", llm=recorded("set_pickup_time", time="21:00")),
    Case("orario_con_minuti", "per le 20:30", cart=[_MARGHERITA], time="20:30", llm=recorded("set_pickup_time", time="20:30")),
    Case("prima_possibile", "il prima possibile", cart=[_MARGHERITA], time="19:00",
         llm=recorded("set_pickup_time", time="prima_possibile")),
    Case("alle_7", "alle 7", cart=[_MARGHERITA], time="19:00",
         llm=recorded("set_pickup_time", time="19:00")),
    Case("per_le_8", "per le 8", cart=[_MARGHERITA], time="20:00",
         llm=recorded("set_pickup_time", time="20:00")),
    Case("alle_7_alla_domanda_sull_orario", "alle 7", cart=[_MARGHERITA], time="19:00", state="collecting_pickup_time"),
    Case("per_le_8_alla_domanda_sull_orario", "per le 8", cart=[_MARGHERITA], time="20:00", state="collecting_pickup_time"),
    # ── Nome ─────────────────────────────────────────────────────────────────
    Case("solo_il_nome", "Giacomo", cart=[_MARGHERITA], name="Giacomo", llm=recorded("set_customer_name", name="Giacomo")),
    Case("nome_nella_frase", "una margherita per le 8, sono Giacomo",
         [_MARGHERITA], time="20:00", name="Giacomo",
         llm=recorded(items=[_MARGHERITA], name="Giacomo", time="20:00")),
    # ── Giorno a cucina aperta ───────────────────────────────────────────────
    Case("per_domani_a_cucina_aperta", "per domani", day=TOMORROW, llm=recorded("unknown")),
    Case("margherita_domani_alle_8", "una margherita per domani alle 8", [_MARGHERITA], day=TOMORROW, time="20:00",
         llm=recorded(items=[_MARGHERITA], time="20:00")),
    Case("stasera_alle_8", "una margherita per stasera alle 8", [_MARGHERITA], time="20:00",
         llm=recorded(items=[_MARGHERITA], time="20:00")),
    Case("giorno_della_settimana", "una margherita per sabato alle 8", [_MARGHERITA], day="2026-10-03", time="20:00",
         llm=recorded(items=[_MARGHERITA], time="20:00")),
    Case("data", "una margherita per il 21 ottobre alle 8", [_MARGHERITA], day="2026-10-21", time="20:00",
         llm=recorded(items=[_MARGHERITA], time="20:00"), reply_contains="A che nome?"),
    Case("domani_a_cucina_chiusa", "una margherita per domani alle 8", [_MARGHERITA], day=TOMORROW, time="20:00",
         now=THURSDAY_23, llm=recorded(items=[_MARGHERITA], time="20:00"),
         reply_contains="La cucina stasera è chiusa, segno per domani venerdì."),
    # ── Giorno chiuso: proposta del primo giorno aperto ──────────────────────
    Case("domani_chiuso_propone_martedi", "una margherita per domani", [_MARGHERITA], now=SUNDAY_16,
         restaurant=MONDAY_CLOSED, llm=recorded(items=[_MARGHERITA]), final_state="confirming_next_day",
         reply_contains="Domani siamo chiusi, il primo giorno disponibile è martedì, va bene?"),
]


class Call:
    """Chat reale con menu di CdS, orologio fisso, DB in memoria; niente Base44 né invii."""

    def __init__(self, now, restaurant, extract_side_effect=None):
        self.now, self.restaurant, self.extract = now, restaurant, extract_side_effect
        self.saved = []

    @contextmanager
    def open(self, state="collecting_items", cart=()):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(engine)
        self.session = Session(engine)
        for entry in MENU:
            self.session.add(MenuItem(
                name=entry["name"], category=entry["category"], pizza_type="Normale",
                price=entry["price"], restaurant_id=RID, sale_unit=entry.get("sale_unit") or "piece",
            ))
        self.session.add(ConversationSession(
            session_id="golden", customer_phone="+393331234567", state=state, completed=False, restaurant_id=RID,
            items_json=json.dumps(recorded(items=cart)["items"], ensure_ascii=False),
        ))
        self.session.commit()
        frozen = _frozen_datetime_module(self.now)
        load = lambda restaurant_id="": dict(self.restaurant)  # noqa: E731

        def fake_save(**kwargs):
            self.saved.append(kwargs)
            return {"id": "b44", "order_number": 1}

        patches = [
            patch.object(service, "datetime", frozen),
            patch.object(chat_module, "datetime", frozen),
            patch.object(service, "load_restaurant", side_effect=load),
            patch.object(chat_module, "load_restaurant", side_effect=load),
            patch.object(chat_module, "ensure_restaurant_config", return_value=True),
            patch.object(chat_module, "is_agent_active", return_value=True),
            patch.object(chat_module, "get_proposable_menu", return_value=MENU),
            patch.object(chat_module, "get_sold_out_item_names", return_value=set()),
            patch.object(chat_module, "load_doughs", return_value=DOUGHS),
            patch.object(chat_module, "is_dough_available", return_value=True),
            patch.object(chat_module, "save_order_to_base44", side_effect=fake_save),
            patch.object(chat_module, "_schedule_order_side_effect_job", return_value=None),
            patch.object(chat_module, "_db_engine", engine),
        ]
        if self.extract is not None:
            patches.append(patch.object(chat_module, "extract_order_from_text", side_effect=self.extract))
        for active in patches:
            active.start()
        try:
            yield self
        finally:
            for active in patches:
                active.stop()
            self.session.close()

    def say(self, message):
        return chat_module.chat(ChatRequest(session_id="golden", message=message), self.session)

    def conversation(self):
        self.session.expire_all()
        return self.session.exec(select(ConversationSession)).one()


def _normalized_items(items_json):
    return sorted(
        (
            {
                "name": i["pizza_name"], "quantity": i["quantity"],
                "add": sorted(a.lower() for a in i.get("add_ingredients") or []),
                "remove": sorted(r.lower() for r in i.get("remove_ingredients") or []),
                "dough": i.get("dough_type") or "classica", "size": i.get("size") or "normale",
            }
            for i in json.loads(items_json)
        ),
        key=lambda i: i["name"],
    )


def _expected_items(case):
    items = case.items if case.replaces_cart else case.cart + case.items
    return sorted(
        ({**i, "add": sorted(i["add"]), "remove": sorted(i["remove"])} for i in items),
        key=lambda i: i["name"],
    )


def _mismatches(case, call, response):
    """Elenco delle differenze tra sessione e risultato atteso; vuoto se è tutto giusto."""
    conversation = call.conversation()
    errors = []
    actual = {
        "items": _normalized_items(conversation.items_json),
        "day": conversation.pickup_date,
        "time": conversation.pickup_time,
        "name": conversation.customer_name,
    }
    expected = {"items": _expected_items(case), "day": case.day, "time": case.time, "name": case.name}
    for key, value in expected.items():
        if actual[key] != value:
            errors.append(f"{key}: atteso {value!r}, ottenuto {actual[key]!r}")
    if case.reply_contains and case.reply_contains not in response.response_message:
        errors.append(f"risposta senza {case.reply_contains!r}: {response.response_message!r}")
    if case.final_state and response.state != case.final_state:
        errors.append(f"stato: atteso {case.final_state!r}, ottenuto {response.state!r}")
    return errors


# ── Versione senza rete ──────────────────────────────────────────────────────


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_golden_with_recorded_extraction(case):
    def fake_extract(*args, **kwargs):
        assert case.llm is not None, "la frase non dovrebbe passare dall'LLM"
        return json.loads(json.dumps(case.llm))

    with Call(case.now, case.restaurant, fake_extract).open(case.state, case.cart) as call:
        response = call.say(case.message)
        assert _mismatches(case, call, response) == []


# ── LLM vero: 5 run per caso, una sola run sbagliata fa fallire il caso ──────


@pytest.mark.llm_live
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_golden_with_real_llm(case):
    failures = []
    for run in range(1, LIVE_RUNS + 1):
        with Call(case.now, case.restaurant).open(case.state, case.cart) as call:
            response = call.say(case.message)
            errors = _mismatches(case, call, response)
        if errors:
            failures.append(f"run {run}: {errors}")
    assert failures == [], f"{len(failures)}/{LIVE_RUNS} run sbagliate per {case.message!r}"


# ── Giorno detto: conversazioni complete ─────────────────────────────────────


def _script(now=THURSDAY_16, restaurant=CDS):
    extractions = []

    def fake_extract(*args, **kwargs):
        return extractions.pop(0) if extractions else recorded("unknown")

    call = Call(now, restaurant, fake_extract)
    call.extractions = extractions
    return call


def test_tomorrow_with_open_kitchen_is_saved_for_tomorrow_and_the_recap_says_so():
    call = _script()
    call.extractions.append(recorded(items=[_MARGHERITA], time="20:00"))
    with call.open():
        response = call.say("una margherita per domani alle 8")
        assert response.response_message == "A che nome?"
        response = call.say("Giacomo")
        assert response.response_message == "Allora: una margherita, per domani venerdì alle 20, a nome Giacomo. Confermo?"
        call.say("sì")
    assert (call.saved[0]["pickup_date"], call.saved[0]["pickup_time"]) == (TOMORROW, "20:00")


def test_tonight_recap_says_tonight_and_nothing_else():
    call = _script()
    call.extractions.append(recorded(items=[_MARGHERITA], name="Giacomo", time="20:00"))
    with call.open():
        response = call.say("una margherita per stasera alle 8, sono Giacomo")
        assert "per stasera alle 20" in response.response_message
        call.say("sì")
    assert call.saved[0].get("pickup_date") is None


def test_closed_tomorrow_on_sunday_proposes_tuesday_and_waits():
    call = _script(SUNDAY_16, MONDAY_CLOSED)
    call.extractions.append(recorded(items=[_MARGHERITA], time="20:00"))
    with call.open():
        response = call.say("una margherita per domani alle 8")
        assert response.state == "confirming_next_day"
        assert response.response_message == "Domani siamo chiusi, il primo giorno disponibile è martedì, va bene?"
        assert call.conversation().pickup_date is None  # niente spostamenti senza un sì

        response = call.say("boh")
        assert response.state == "confirming_next_day"
        assert "il primo giorno disponibile è martedì" in response.response_message

        response = call.say("sì")
        assert response.response_message == "Perfetto, il ritiro è per martedì. A che nome?"
        response = call.say("Giacomo")
        assert response.response_message == "Allora: una margherita, per martedì alle 20, a nome Giacomo. Confermo?"
        call.say("sì")
    assert (call.saved[0]["pickup_date"], call.saved[0]["pickup_time"]) == ("2026-10-06", "20:00")


def test_closed_day_refused_closes_the_call_without_saving():
    call = _script(SUNDAY_16, MONDAY_CLOSED)
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        call.say("una margherita per domani")
        response = call.say("no grazie")
        assert response.state == "completed"
    assert call.saved == []


def test_closed_named_weekday_proposes_the_next_open_day():
    call = _script(THURSDAY_16, MONDAY_CLOSED)
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        response = call.say("una margherita per lunedì")
        assert response.response_message == "Lunedì siamo chiusi, il primo giorno disponibile è martedì, va bene?"


def test_answer_with_another_day_wins_over_the_proposal():
    call = _script(SUNDAY_16, MONDAY_CLOSED)
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        call.say("una margherita per domani")
        call.say("no, mercoledì")
        assert call.conversation().pickup_date == "2026-10-07"


def test_answer_with_another_closed_day_gets_a_new_proposal():
    closed = {**MONDAY_CLOSED, "opening_hours": {**MONDAY_CLOSED["opening_hours"], "wednesday": ""}}
    call = _script(SUNDAY_16, closed)
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        call.say("una margherita per domani")
        response = call.say("mercoledì")
        assert response.response_message == "Mercoledì siamo chiusi, il primo giorno disponibile è giovedì, va bene?"
        call.say("sì")
        assert call.conversation().pickup_date == "2026-10-08"


def test_tonight_as_answer_to_the_proposal_keeps_today():
    call = _script(SUNDAY_16, MONDAY_CLOSED)
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        call.say("una margherita per domani")
        call.say("allora stasera")
        assert call.conversation().pickup_date is None
        assert call.conversation().state != "confirming_next_day"


def test_day_change_at_the_recap_updates_without_confirming_or_cancelling():
    call = _script()
    call.extractions.append(recorded(items=[_MARGHERITA], name="Giacomo", time="20:00"))
    with call.open():
        response = call.say("una margherita per le 8, sono Giacomo")
        assert "per stasera alle 20" in response.response_message
        response = call.say("no, per domani")
        assert response.state == "awaiting_confirmation"
        assert "per domani venerdì alle 20" in response.response_message
        assert call.saved == []
        assert json.loads(call.conversation().items_json)  # il "no" non ha annullato
        call.say("sì")
    assert call.saved[0]["pickup_date"] == TOMORROW


def test_tonight_at_the_recap_cancels_a_day_said_earlier():
    call = _script()
    call.extractions.append(recorded(items=[_MARGHERITA], name="Giacomo", time="20:00"))
    with call.open():
        call.say("una margherita per domani alle 8, sono Giacomo")
        response = call.say("no, stasera")
        assert "per stasera alle 20" in response.response_message
        assert call.conversation().pickup_date is None


def test_day_at_the_pickup_time_question_is_respected():
    call = _script()
    with call.open("collecting_pickup_time", [_MARGHERITA]):
        conversation = call.conversation()
        conversation.customer_name = "Giacomo"
        call.session.add(conversation)
        call.session.commit()
        response = call.say("domani alle 8")
        assert "per domani venerdì alle 20" in response.response_message
        assert call.conversation().pickup_date == TOMORROW


def test_day_alone_at_the_pickup_time_question_is_not_a_failure():
    call = _script()
    with call.open("collecting_pickup_time", [_MARGHERITA]):
        conversation = call.conversation()
        conversation.customer_name = "Giacomo"
        call.session.add(conversation)
        call.session.commit()
        response = call.say("per domani")
        assert response.state == "collecting_pickup_time"
        assert "Non ho capito" not in response.response_message
        assert "misses" not in call.conversation().slot_attempts_json


def test_day_at_the_name_question_is_not_a_name():
    call = _script()
    with call.open("collecting_name", [_MARGHERITA]):
        response = call.say("per domani")
        assert call.conversation().customer_name is None
        assert call.conversation().pickup_date == TOMORROW
        assert response.state == "collecting_name"


def test_weekday_used_as_a_name_is_not_a_day():
    call = _script()
    with call.open("collecting_name", [_MARGHERITA]):
        call.say("Domenica")
        assert call.conversation().customer_name == "Domenica"
        assert call.conversation().pickup_date is None


def test_weekday_as_a_name_inside_an_order_is_not_a_day():
    call = _script()
    call.extractions.append(recorded(items=[_MARGHERITA], name="Domenica"))
    with call.open():
        call.say("una margherita, sono Domenica")
        assert call.conversation().pickup_date is None


def test_today_weekday_means_today_while_slots_are_left():
    call = _script()  # giovedì 16:00
    call.extractions.append(recorded(items=[_MARGHERITA], time="20:00"))
    with call.open():
        call.say("una margherita per giovedì alle 8")
        assert call.conversation().pickup_date is None


def test_today_weekday_means_next_week_when_the_kitchen_is_closed():
    call = _script(THURSDAY_23)
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        call.say("una margherita per giovedì")
        assert call.conversation().pickup_date == "2026-10-08"


def test_far_date_recap_names_the_date():
    call = _script()
    call.extractions.append(recorded(items=[_MARGHERITA], name="Giacomo", time="20:00"))
    with call.open():
        response = call.say("una margherita per il 21 ottobre alle 8, sono Giacomo")
    assert "per mercoledì 21 ottobre alle 20" in response.response_message


def test_past_date_means_next_year():
    with Call(THURSDAY_16, CDS, lambda *a, **k: None).open():
        assert chat_module._spoken_pickup_day("per il 5 settembre") == datetime.date(2027, 9, 5)
        assert chat_module._spoken_pickup_day("per il 5/10") == datetime.date(2026, 10, 5)
        assert chat_module._spoken_pickup_day("alle 8 2 margherite") is None  # non è una data


def test_cancel_forgets_the_day():
    call = _script()
    call.extractions.append(recorded(items=[_MARGHERITA]))
    call.extractions.append(recorded("cancel_order"))
    with call.open():
        call.say("una margherita per domani")
        call.say("annulla l'ordine")
        assert call.conversation().pickup_date is None


def test_reservation_for_tomorrow_does_not_touch_the_pickup_day():
    with _script().open() as call:
        call.say("vorrei prenotare un tavolo per domani")
        assert call.conversation().pickup_date is None


def test_usual_order_recap_names_the_day():
    call = _script()
    with call.open():
        conversation = call.conversation()
        conversation.favorite_pizzas_json = '["Margherita"]'
        conversation.customer_name = "Giacomo"
        call.session.add(conversation)
        call.session.commit()
        call.say("2 pizze per domani alle 20")
        response = call.say("sì")
    assert "per domani venerdì alle 20" in response.response_message


# ── Pizza fuori menu: a voce e nel prezzo ────────────────────────────────────


@pytest.mark.parametrize("items, spoken", [
    ([item(PERSONAL, add=["salamino"])], "una margherita con salamino"),
    ([item(PERSONAL, 2, add=["salamino"])], "2 margherite con salamino"),
    ([item(PERSONAL, add=["melanzane", "gorgonzola"])], "una margherita con melanzane e gorgonzola"),
    ([item(PERSONAL, add=["melanzane", "salamino", "gorgonzola"])], "una margherita con melanzane, salamino e gorgonzola"),
    ([item(PERSONAL, add=["würstel"], remove=["pomodoro"])], "una bianca con würstel"),
    ([item("Margherita", add=["patatine"])], "una margherita con patatine"),
    ([item("Margherita", remove=["origano"])], "una margherita senza origano"),
    ([item(PERSONAL)], "una margherita"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_off_menu_pizza_is_spoken_as_margherita(items, spoken):
    stored = [{**i, "sale_unit": "piece"} for i in recorded(items=items)["items"]]
    text = chat_module.format_items_for_customer(stored)
    assert text == spoken
    assert "personalizzat" not in text.lower()


def test_recap_of_the_real_call_never_says_personalizzata():
    call = _script()
    call.extractions.append(recorded(items=[item(PERSONAL, 2, add=["salamino"])], name="Giacomo", time="20:00"))
    with call.open():
        response = call.say("2 salamino per domani alle 8, sono Giacomo")
    assert response.response_message == "Allora: 2 margherite con salamino, per domani venerdì alle 20, a nome Giacomo. Confermo?"


@pytest.mark.parametrize("items, total", [
    ([item("Margherita")], 5.80),  # prezzo del menu di CdS
    ([item(PERSONAL, add=["salamino"])], 7.80),  # Margherita + 1 aggiunta
    ([item(PERSONAL, 2, add=["salamino"])], 15.60),  # per ogni pizza
    ([item(PERSONAL, add=["melanzane", "gorgonzola"])], 9.80),  # + €2,00 per ogni aggiunta
    ([item(PERSONAL, add=["melanzane", "salamino", "gorgonzola"])], 11.80),
    ([item(PERSONAL, add=["würstel"], remove=["pomodoro"])], 7.80),  # le rimozioni non scontano
], ids=["margherita", "una_aggiunta", "per_pizza", "due_aggiunte", "tre_aggiunte", "rimozione"])
def test_off_menu_price_is_margherita_plus_two_euro_per_addition(items, total):
    with Call(THURSDAY_16, CDS, lambda *a, **k: None).open() as call, \
            patch.object(chat_module, "get_dough_surcharge", return_value=0.0):
        stored = recorded(items=items)["items"]
        _, amount = chat_module.enrich_items_with_pricing(call.session, stored, restaurant_id=RID)
    assert amount == total


def test_off_menu_order_is_saved_with_the_addition_and_the_price():
    call = _script()
    call.extractions.append(recorded(items=[item(PERSONAL, 2, add=["salamino"])], name="Giacomo", time="20:00"))
    with call.open(), patch.object(chat_module, "get_dough_surcharge", return_value=0.0):
        call.say("2 salamino per domani alle 8, sono Giacomo")
        call.say("sì")
    saved = call.saved[0]["items"][0]
    assert saved["add_ingredients"] == ["salamino"]
    assert (saved["base_price"], saved["extras_price"], saved["total_price"]) == (5.80, 2.00, 15.60)


@pytest.mark.parametrize("message, removed, expected", [
    ("tre porchetta senza rucola", ["rucola"], ["porchetta"]),
    ("una speck senza origano", ["origano"], ["speck"]),
    ("una margherita e una porchetta senza rucola", ["rucola"], ["porchetta"]),
    ("una hot dog e patatine senza la senape", ["senape"], ["hot dog", "patatine"]),
    ("una senza pomodoro", ["pomodoro"], []),
    ("una pizza senza pomodoro", ["pomodoro"], []),
    ("una bianca senza origano", ["pomodoro", "origano"], []),
    ("vorrei qualcosa senza glutine", ["pomodoro"], []),
])
def test_off_menu_name_lost_by_the_llm_is_recovered_from_the_sentence(message, removed, expected):
    items = [{"pizza_name": PERSONAL, "add_ingredients": [], "remove_ingredients": removed}]
    service._restore_off_menu_names(items, message)
    assert items[0]["add_ingredients"] == expected


def _say_with_llm_item(name, intent="add_items", sold_out=()):
    call = _script()
    call.extractions.append(recorded(intent, items=[item(name, 3, remove=["rucola"])]))
    with call.open(), patch.object(chat_module, "get_sold_out_item_names", return_value=set(sold_out)):
        response = call.say(f"tre {name} senza rucola")
        return response, _normalized_items(call.conversation().items_json)


def test_off_menu_name_returned_by_the_llm_becomes_margherita_with_the_addition():
    response, items = _say_with_llm_item("Porchetta")
    assert (items[0]["name"], items[0]["add"], items[0]["remove"]) == (PERSONAL, ["porchetta"], ["rucola"])
    assert "menu" not in response.response_message


def test_item_sold_only_by_another_restaurant_is_off_menu_here():
    """Porchetta è di Pizza a Pezzi: stesso DB, ma per CdS è fuori menu, non un refuso."""
    call = _script()
    call.extractions.append(recorded(items=[item("Porchetta", 3, remove=["rucola"])]))
    with call.open():
        call.session.add(MenuItem(name="Porchetta", category="pizza", pizza_type="Normale", price=19.9, restaurant_id="altro-locale"))
        call.session.commit()
        response = call.say("tre porchetta senza rucola")
        items = _normalized_items(call.conversation().items_json)
    assert (items[0]["name"], items[0]["add"]) == (PERSONAL, ["porchetta"])
    assert "Forse intendevi" not in response.response_message


def test_typo_of_a_menu_item_keeps_the_suggestion():
    response, items = _say_with_llm_item("Capriciosa")
    assert "Forse intendevi Capricciosa" in response.response_message
    assert items[0]["name"] == "Capriciosa"


def test_sold_out_menu_item_is_refused_and_not_turned_into_a_margherita():
    response, items = _say_with_llm_item("Diavola", sold_out=["diavola"])
    assert items[0]["name"] == "Diavola"
    assert response.state == "collecting_items"


def test_additions_said_by_the_llm_are_never_overwritten():
    items = [{"pizza_name": PERSONAL, "add_ingredients": ["speck"], "remove_ingredients": ["origano"]}]
    service._restore_off_menu_names(items, "una porchetta senza origano")
    assert items[0]["add_ingredients"] == ["speck"]
    menu_item = [{"pizza_name": "Capricciosa", "add_ingredients": [], "remove_ingredients": ["olive"]}]
    service._restore_off_menu_names(menu_item, "una capricciosa senza olive")
    assert menu_item[0]["add_ingredients"] == []


def test_customer_sms_shows_margherita_plus_the_addition():
    lines = service._build_pizza_lines([{**recorded(items=[item(PERSONAL, 2, add=["salamino"])])["items"][0], "sale_unit": "piece"}])
    assert lines == ["- 2x Margherita", "    + salamino"]


# ── docs/regole_ordine.md ────────────────────────────────────────────────────


def test_rules_document_only_cites_tests_that_exist():
    """Ogni regola cita un test reale: un test rinominato o tolto fa fallire questo controllo."""
    root = Path(__file__).parents[1]
    document = (root / "docs" / "regole_ordine.md").read_text(encoding="utf-8")
    cited = set(re.findall(r"`(tests/[\w/]+\.py)::(\w+)`", document))
    assert len(cited) > 30
    missing = []
    for path, name in sorted(cited):
        defined = {
            node.name for node in ast.walk(ast.parse((root / path).read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef)
        }
        if name not in defined:
            missing.append(f"{path}::{name}")
    assert missing == []


def test_rules_document_cites_every_golden_case():
    document = (Path(__file__).parents[1] / "docs" / "regole_ordine.md").read_text(encoding="utf-8")
    assert [case.id for case in CASES if f"`{case.id}`" not in document] == []
