"""Regressione delle regole d'ordine: frasi reali → risultato atteso.

Ogni caso indica prodotti, quantità, aggiunte, rimozioni, impasto, giorno, orario e nome
che devono trovarsi nella sessione dopo la frase, con il menu reale di Corte del Sole
(tests/fixtures/cds_menu.json).

Due modi di eseguire gli stessi casi:

    pytest tests/test_order_rules_golden.py             # estrazione registrata, senza rete
    pytest tests/test_order_rules_golden.py --run-llm   # anche LLM vero, una run per caso

L'LLM vero si usa solo se il commit modifica il prompt o l'estrazione dell'ordine.
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
from app.models import ConversationSession, MenuItem, OrderSideEffect
from app.schemas import ChatRequest
from app.services import conversation_service as service
from app.services.daily_summary import flavour_totals
from app.services.order_sync import build_order_items
from tests.test_opening_hours_flow import _frozen_datetime_module

LIVE_RUNS = 1
ROME = ZoneInfo("Europe/Rome")
RID = "rest-golden"
MENU = json.loads((Path(__file__).parent / "fixtures" / "cds_menu.json").read_text(encoding="utf-8"))
DOUGHS = json.loads((Path(__file__).parents[1] / "app" / "dough_data.json").read_text(encoding="utf-8"))
ALL_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
CDS = {"id": RID, "name": "Corte Del Sole", "min_prep_minutes": 30, "opening_hours": {d: "19:00-22:00" for d in ALL_DAYS}}
MONDAY_CLOSED = {**CDS, "opening_hours": {**CDS["opening_hours"], "monday": ""}}
NO_CUSTOM_PIZZA = {**CDS, "custom_pizza_enabled": False}
NEXT_DAY_ONLY = {**CDS, "phone_orders_next_day_only": True}

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
    Case("due_tonno_senza_cipolla", "due tonno senza cipolla",
         [item(PERSONAL, 2, add=["tonno"], remove=["cipolla"])],
         llm=recorded(items=[item(PERSONAL, 2, add=["tonno"], remove=["cipolla"])])),
    Case("salamino_e_rucola_senza_pomodoro", "una salamino e rucola senza pomodoro",
         [item(PERSONAL, add=["salamino", "rucola"], remove=["pomodoro"])],
         llm=recorded(items=[item(PERSONAL, add=["salamino", "rucola"], remove=["pomodoro"])])),
    Case("salsiccia_senza_rucola", "tre salsiccia senza rucola",
         [item(PERSONAL, 3, add=["salsiccia"], remove=["rucola"])],
         llm=recorded(items=[item(PERSONAL, 3, add=["salsiccia"], remove=["rucola"])])),
    # ── Solo ingredienti del locale o alias: il resto non diventa un'aggiunta ─
    Case("non_ingrediente_kebab", "due kebab senza cipolla", final_state="collecting_items",
         llm=recorded(items=[item(PERSONAL, 2, add=["kebab"], remove=["cipolla"])]),
         reply_contains="Cosa desidera ordinare?"),
    Case("non_ingrediente_porchetta", "tre porchetta senza rucola", final_state="collecting_items",
         llm=recorded(items=[item(PERSONAL, 3, add=["porchetta"], remove=["rucola"])]),
         reply_contains="Cosa desidera ordinare?"),
    Case("nome_di_persona_non_e_aggiunta", "una Mario", final_state="collecting_items",
         llm=recorded(items=[item(PERSONAL, add=["mario"])]), reply_contains="Cosa desidera ordinare?"),
    Case("nome_del_locale_su_una_pizza_del_menu", "una margherita con Corte del sole", [_MARGHERITA],
         llm=recorded(items=[item("Margherita", add=["Corte del sole"])])),
    # ── Dalle chiamate reali del 4 novembre, con le parole esatte ────────────
    Case("reale_corte_del_sole", "Corte del sole.", final_state="collecting_items",
         reply_contains="Cosa desidera ordinare?"),
    Case("reale_corte_del_sole_cucina_chiusa", "Corte del sole.", now=THURSDAY_23, final_state="confirming_next_day",
         reply_contains="La cucina stasera è chiusa. Vuole ordinare per domani o prenotare un tavolo?"),
    Case("reale_tavolo_dentro_un_ordine", "Posso prenotare un tavolo per 2 sabato 20 novembre", now=THURSDAY_23,
         state="confirming_next_day", final_state="collecting_reservation_date",
         reply_contains="Venerdì è il 20, sabato è il 21: quale dei due?"),
    Case("reale_prenotare_una_margherita", "Vorrei prenotare una margherita per domani", [_MARGHERITA], day=TOMORROW,
         llm=recorded(items=[_MARGHERITA]), final_state="collecting_name"),
    Case("reale_no_alla_domanda_sulle_persone", "No no no no.", state="collecting_reservation_party",
         final_state="collecting_reservation_party", reply_contains="Va bene, cosa devo correggere?"),
    # ── Ordine o prenotazione: decide il contenuto ───────────────────────────
    Case("prenotarmi_una_capricciosa", "vorrei prenotarmi una capricciosa", [item("Capricciosa")],
         llm=recorded(items=[item("Capricciosa")])),
    Case("mi_tiene_da_parte", "mi tiene da parte una margherita per domani", [_MARGHERITA], day=TOMORROW,
         llm=recorded(items=[_MARGHERITA]), final_state="collecting_name"),
    Case("vorrei_prenotare_e_basta", "vorrei prenotare", final_state="collecting_items",
         reply_contains="Vuole ordinare da asporto o prenotare un tavolo?"),
    Case("siamo_in_quattro", "siamo in 4 per domani", final_state="collecting_reservation_time",
         reply_contains="A che ora?"),
    Case("speck_senza_origano", "una speck senza origano",
         [item(PERSONAL, add=["speck"], remove=["origano"])],
         llm=recorded(items=[item(PERSONAL, add=["speck"], remove=["origano"])])),
    # ── Alias: non vale se la parola è già una voce del menu ─────────────────
    Case("bondola_e_una_pizza_del_menu", "una bondola", [item("Bondola")], llm=recorded(items=[item("Bondola")])),
    Case("due_bondola_con_orario", "due bondola per le 8", [item("Bondola", 2)], time="20:00",
         llm=recorded(items=[item("Bondola", 2)], time="20:00")),
    # ── custom_pizza_enabled = false: niente Margherita, gusti più simili ────
    Case("fuori_menu_rifiutata", "una salamino", restaurant=NO_CUSTOM_PIZZA, final_state="collecting_items",
         llm=recorded(items=[item(PERSONAL, add=["salamino"])]), reply_contains="Salamino oggi non c'è, abbiamo"),
    Case("fuori_menu_rifiutata_giorno_e_orario_restano", "2 salamino per domani alle 8", day=TOMORROW, time="20:00",
         restaurant=NO_CUSTOM_PIZZA, final_state="collecting_items",
         llm=recorded(items=[item(PERSONAL, 2, add=["salamino"])], time="20:00"), reply_contains="Quale vuole?"),
    Case("flag_spento_pizza_del_menu_con_aggiunta", "una capricciosa con rucola", [item("Capricciosa", add=["rucola"])],
         restaurant=NO_CUSTOM_PIZZA, llm=recorded(items=[item("Capricciosa", add=["rucola"])])),
    # ── Preordine fisso: un altro giorno non si ignora ───────────────────────
    Case("preordine_altro_giorno", "una margherita per sabato", [_MARGHERITA], day=TOMORROW, restaurant=NEXT_DAY_ONLY,
         llm=recorded(items=[_MARGHERITA]), final_state="confirming_next_day",
         reply_contains="Prendiamo ordini solo per domani venerdì, va bene?"),
    Case("preordine_stesso_giorno", "una margherita per domani", [_MARGHERITA], day=TOMORROW, restaurant=NEXT_DAY_ONLY,
         llm=recorded(items=[_MARGHERITA]), final_state="collecting_name"),
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


def test_no_to_the_closed_day_proposal_asks_for_the_day_and_keeps_the_order():
    call = _script(SUNDAY_16, MONDAY_CLOSED)
    call.extractions.append(recorded(items=[_MARGHERITA], time="20:00"))
    with call.open():
        call.say("una margherita per domani alle 8")
        response = call.say("no")
        assert (response.state, response.response_message) == ("confirming_next_day", "Per quale giorno le serve?")
        assert json.loads(call.conversation().items_json)
        response = call.say("mercoledì")
        assert response.response_message == "Perfetto, il ritiro è per mercoledì. A che nome?"
        call.say("Giacomo")
        call.say("sì")
    assert (call.saved[0]["pickup_date"], call.saved[0]["pickup_time"]) == ("2026-10-07", "20:00")


def test_no_then_a_closed_day_reapplies_the_rules():
    call = _script(SUNDAY_16, MONDAY_CLOSED)
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        call.say("una margherita per domani")
        call.say("no")
        response = call.say("lunedì")  # di domenica lunedì è domani
        assert response.response_message == "Domani siamo chiusi, il primo giorno disponibile è martedì, va bene?"
        response = call.say("sì")
        assert response.response_message == "Perfetto, il ritiro è per martedì. A che nome?"


def test_no_then_tonight_with_the_kitchen_open_is_for_today():
    call = _script(SUNDAY_16, MONDAY_CLOSED)
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        call.say("una margherita per domani")
        call.say("no")
        response = call.say("stasera")
        assert response.state == "collecting_name"
        assert call.conversation().pickup_date is None


def test_no_to_the_next_day_question_with_the_kitchen_closed_asks_for_the_day():
    call = _script(THURSDAY_23)
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        response = call.say("una margherita")
        assert "vuole ordinare per domani?" in response.response_message
        response = call.say("no")
        assert response.response_message == "Per quale giorno le serve?"
        call.say("sabato")
        assert call.conversation().pickup_date == "2026-10-03"


def test_yes_to_the_day_question_is_not_an_answer():
    call = _script(SUNDAY_16, MONDAY_CLOSED)
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        call.say("una margherita per domani")
        call.say("no")
        response = call.say("sì")
        assert response.state == "confirming_next_day"
        assert response.response_message == "Mi scusi, non ho capito. Per quale giorno le serve?"
        assert call.conversation().pickup_date is None


def test_explicit_decline_of_the_day_question_closes_the_call_without_saving():
    call = _script(SUNDAY_16, MONDAY_CLOSED)
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        call.say("una margherita per domani")
        call.say("no")
        response = call.say("no grazie")
        assert response.state == "completed"
    assert call.saved == []


# ── Preordine fisso: "Prendiamo ordini solo per <giorno>, va bene?" ──────────


def test_fixed_day_other_day_is_asked_and_yes_keeps_order_and_time():
    call = _script(THURSDAY_16, NEXT_DAY_ONLY)
    call.extractions.append(recorded(items=[_MARGHERITA], time="20:00"))
    with call.open():
        response = call.say("una margherita per sabato alle 8")
        assert (response.state, response.response_message) == (
            "confirming_next_day", "Prendiamo ordini solo per domani venerdì, va bene?",
        )
        response = call.say("sì")
        assert response.response_message == "Perfetto, il ritiro è per domani, venerdì. A che nome?"
        response = call.say("Giacomo")
        assert response.response_message == "Allora: una margherita, per domani venerdì alle 20, a nome Giacomo. Confermo?"
        call.say("sì")
    assert (call.saved[0]["pickup_date"], call.saved[0]["pickup_time"]) == (TOMORROW, "20:00")


def test_fixed_day_with_tomorrow_closed_names_the_next_open_day():
    call = _script(SUNDAY_16, {**NEXT_DAY_ONLY, "opening_hours": MONDAY_CLOSED["opening_hours"]})
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        response = call.say("una margherita per domani")
        assert response.response_message == "Prendiamo ordini solo per martedì, va bene?"


def test_fixed_day_no_asks_for_the_day_and_reapplies_the_rule():
    call = _script(THURSDAY_16, NEXT_DAY_ONLY)
    call.extractions.append(recorded(items=[_MARGHERITA]))
    with call.open():
        call.say("una margherita per sabato")
        response = call.say("no")
        assert response.response_message == "Per quale giorno le serve?"
        response = call.say("domenica")
        assert response.response_message == "Prendiamo ordini solo per domani venerdì, va bene?"
        call.say("no")
        response = call.say("domani")
        assert response.response_message == "Perfetto, il ritiro è per domani, venerdì. A che nome?"


def test_fixed_day_other_day_at_the_recap_is_asked_and_not_confirmed():
    call = _script(THURSDAY_16, NEXT_DAY_ONLY)
    call.extractions.append(recorded(items=[_MARGHERITA], name="Giacomo", time="20:00"))
    with call.open():
        call.say("una margherita per le 8, sono Giacomo")
        response = call.say("sì ma per sabato")
        assert response.response_message == "Prendiamo ordini solo per domani venerdì, va bene?"
        assert call.saved == []


def test_fixed_day_other_day_at_the_pickup_time_question_is_asked():
    call = _script(THURSDAY_16, NEXT_DAY_ONLY)
    with call.open("collecting_pickup_time", [_MARGHERITA]):
        response = call.say("sabato alle 8")
        assert response.response_message == "Prendiamo ordini solo per domani venerdì, va bene?"


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


# ── custom_pizza_enabled ─────────────────────────────────────────────────────


def _no_custom(*extractions):
    call = _script(THURSDAY_16, NO_CUSTOM_PIZZA)
    call.extractions.extend(extractions)
    return call


def test_custom_pizza_flag_defaults_to_true():
    for value in ({}, {"custom_pizza_enabled": None}, {"custom_pizza_enabled": True}):
        with Call(THURSDAY_16, {**CDS, **value}, lambda *a, **k: None).open():
            assert service.is_custom_pizza_enabled(RID) is True
    with Call(THURSDAY_16, NO_CUSTOM_PIZZA, lambda *a, **k: None).open():
        assert service.is_custom_pizza_enabled(RID) is False


def test_custom_pizza_null_still_becomes_margherita():
    call = _script(THURSDAY_16, {**CDS, "custom_pizza_enabled": None})
    call.extractions.append(recorded(items=[item(PERSONAL, add=["salamino"])]))
    with call.open():
        call.say("una salamino")
        assert _normalized_items(call.conversation().items_json)[0]["add"] == ["salamino"]


def test_off_menu_refused_names_three_similar_flavours_and_saves_nothing():
    call = _no_custom(recorded(items=[item(PERSONAL, add=["salamino"])]))
    with call.open():
        response = call.say("una salamino")
        assert re.fullmatch(r"Salamino oggi non c'è, abbiamo [^,]+, [^,]+ e [^,]+\. Quale vuole\?", response.response_message)
        assert response.state == "collecting_items"
        assert json.loads(call.conversation().items_json) == []
        names = re.search(r"abbiamo (.+)\. Quale", response.response_message).group(1).replace(" e ", ", ").split(", ")
        assert set(names) <= {entry["name"] for entry in MENU} and len(names) == 3


def test_similar_flavours_look_at_names_and_ingredients():
    menu = [
        {"name": "Marinara", "ingredients": ["pomodoro", "aglio"]},
        {"name": "Tonnara", "ingredients": ["tonno", "cipolla"]},
        {"name": "Mare", "ingredients": ["tonno", "gamberi"]},
        {"name": "Boscaiola", "ingredients": ["funghi", "salsiccia"]},
    ]
    assert chat_module._similar_menu_flavours(["tonno"], menu)[:2] == ["Tonnara", "Mare"]
    assert chat_module._similar_menu_flavours(["salsiccia"], menu)[0] == "Boscaiola"
    assert chat_module._similar_menu_flavours([], menu) == ["Marinara", "Tonnara", "Mare"]


def test_simplest_flavour_wins_a_tie():
    assert chat_module._similar_menu_flavours(["salamino"], MENU)[0] == "Diavola"


def test_off_menu_refused_when_the_llm_returns_the_bare_name():
    call = _no_custom(recorded(items=[item("Speck")]))
    with call.open():
        response = call.say("una speck")
        assert response.response_message.startswith("Speck oggi non c'è, abbiamo")


def test_off_menu_refused_keeps_the_other_pizzas_day_and_time():
    call = _no_custom(recorded(items=[_MARGHERITA, item(PERSONAL, add=["salamino"])], time="20:00"))
    with call.open():
        response = call.say("una margherita e una salamino per domani alle 8")
        assert "Salamino oggi non c'è" in response.response_message
        conversation = call.conversation()
        assert [i["name"] for i in _normalized_items(conversation.items_json)] == ["Margherita"]
        assert (conversation.pickup_date, conversation.pickup_time) == (TOMORROW, "20:00")


def test_chosen_flavour_gets_the_quantity_said_before():
    call = _no_custom(recorded(items=[item(PERSONAL, 2, add=["salamino"])]), recorded(items=[item("Diavola")]))
    with call.open():
        call.say("2 salamino")
        call.say("la diavola")
        assert _normalized_items(call.conversation().items_json)[0]["quantity"] == 2


def test_chosen_flavour_with_its_own_quantity_keeps_it():
    call = _no_custom(recorded(items=[item(PERSONAL, 2, add=["salamino"])]), recorded(items=[item("Diavola")]))
    with call.open():
        call.say("2 salamino")
        call.say("una diavola")
        assert _normalized_items(call.conversation().items_json)[0]["quantity"] == 1


def test_off_menu_refused_at_closed_kitchen_agrees_the_day_first():
    call = _script(THURSDAY_23, NO_CUSTOM_PIZZA)
    call.extractions.append(recorded(items=[item(PERSONAL, add=["salamino"])]))
    with call.open():
        response = call.say("una salamino")
        assert "vuole ordinare per domani?" in response.response_message
        response = call.say("sì")
        assert response.response_message.startswith("Perfetto, il ritiro è per domani, venerdì. Salamino oggi non c'è, abbiamo")
        assert response.state == "collecting_items"


def test_sold_out_and_typos_are_not_affected_by_the_flag():
    call = _no_custom(recorded(items=[item("Capriciosa")]))
    with call.open():
        response = call.say("una capriciosa")
        assert "Forse intendevi Capricciosa" in response.response_message


# ── Alias ────────────────────────────────────────────────────────────────────


def test_alias_is_skipped_when_the_word_is_on_the_menu_of_the_restaurant():
    assert service._apply_aliases("una bondola", MENU) == "una bondola"
    assert service._apply_aliases("una Bondola", MENU) == "una Bondola"


def test_alias_applies_where_the_word_is_not_on_the_menu():
    assert service._apply_aliases("una bondola", [{"name": "Bufala"}]) == "una mortadella"
    assert service._apply_aliases("una bondola") == "una mortadella"
    assert service._apply_aliases("una wurstel e una verde", MENU) == "una würstel e una gorgonzola"


def test_bondola_is_the_pizza_at_its_menu_price():
    call = _script()
    call.extractions.append(recorded(items=[item("Bondola")], name="Giacomo", time="20:00"))
    with call.open(), patch.object(chat_module, "get_dough_surcharge", return_value=0.0):
        response = call.say("una bondola per le 8, sono Giacomo")
        assert "una bondola" in response.response_message
        call.say("sì")
    assert (call.saved[0]["items"][0]["pizza_name"], call.saved[0]["items"][0]["total_price"]) == ("Bondola", 11.0)


# ── Nome su Base44, riepilogo giornaliero, preferiti ─────────────────────────


def _base44_item(**extra):
    return {
        "pizza_name": PERSONAL, "quantity": 2, "sale_unit": "piece", "dough_type": "classica",
        "add_ingredients": ["salamino"], "remove_ingredients": [], "base_price": 5.8,
        "extras_price": 2.0, "total_price": 15.6, **extra,
    }


def test_off_menu_is_saved_on_base44_as_margherita_with_the_additions():
    saved = build_order_items([_base44_item()])[0]
    assert saved["pizza_name"] == "Margherita"
    assert saved["add_ingredients"] == ["salamino"]
    assert (saved["base_price"], saved["extras_price"], saved["total_price"]) == (5.8, 2.0, 15.6)


def test_menu_names_are_saved_on_base44_unchanged():
    assert build_order_items([_base44_item(pizza_name="Bondola")])[0]["pizza_name"] == "Bondola"


def _summary_order(*items):
    return {"items": [{"sale_unit": "piece", "add_ingredients": [], **i} for i in items]}


def test_daily_summary_sums_margherite_by_additions():
    lines = flavour_totals([
        _summary_order(
            {"pizza_name": "Margherita", "quantity": 2, "add_ingredients": ["salamino"]},
            {"pizza_name": "Margherita", "quantity": 1},
            {"pizza_name": "Margherita", "quantity": 1, "add_ingredients": ["gorgonzola", "melanzane"]},
        ),
        _summary_order(
            {"pizza_name": "Margherita", "quantity": 1, "add_ingredients": ["salamino"]},
            {"pizza_name": "Margherita", "quantity": 1, "add_ingredients": ["melanzane", "gorgonzola"]},
            {"pizza_name": "Bondola", "quantity": 1},
        ),
    ])
    assert lines == [
        "Bondola: 1 pezzo",
        "Margherita: 1 pezzo",
        "Margherita +gorgonzola +melanzane: 2 pezzi",
        "Margherita +salamino: 3 pezzi",
    ]


def test_daily_summary_sums_orders_saved_before_the_rename_with_the_new_ones():
    lines = flavour_totals([
        _summary_order({"pizza_name": PERSONAL, "quantity": 2, "add_ingredients": ["salamino"]}),
        _summary_order({"pizza_name": "Margherita", "quantity": 1, "add_ingredients": ["salamino"]}),
    ])
    assert lines == ["Margherita +salamino: 3 pezzi"]


def test_off_menu_pizza_is_not_stored_as_a_favourite():
    call = _script()
    call.extractions.append(recorded(items=[item(PERSONAL, add=["salamino"]), item("Diavola")], name="Giacomo", time="20:00"))
    with call.open():
        call.say("una salamino e una diavola per le 8, sono Giacomo")
        call.say("sì")
        jobs = call.session.exec(select(OrderSideEffect).where(OrderSideEffect.kind == "customer_upsert")).all()
    assert json.loads(jobs[0].payload_json)["pizza_names"] == ["Diavola"]


def _usual_call(favourites):
    call = _script()
    call.extractions.append(recorded("unknown"))
    return call, json.dumps(favourites)


def test_favourite_saved_as_personalizzata_is_never_proposed():
    call, favourites = _usual_call(["Personalizzata"])
    with call.open():
        conversation = call.conversation()
        conversation.favorite_pizzas_json = favourites
        call.session.add(conversation)
        call.session.commit()
        response = call.say("2 pizze")
        assert "solite" not in response.response_message
        assert "personalizzat" not in response.response_message.lower()


def test_other_favourites_are_still_proposed():
    call, favourites = _usual_call(["Personalizzata", "Diavola"])
    with call.open():
        conversation = call.conversation()
        conversation.favorite_pizzas_json = favourites
        call.session.add(conversation)
        call.session.commit()
        response = call.say("2 pizze")
        assert response.response_message == "Le preparo le solite? Diavola"


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
    response, items = _say_with_llm_item("Speck")
    assert (items[0]["name"], items[0]["add"], items[0]["remove"]) == (PERSONAL, ["speck"], ["rucola"])
    assert "menu" not in response.response_message


def test_item_sold_only_by_another_restaurant_is_off_menu_here():
    """Speck è solo di un altro locale: stesso DB, ma per CdS è fuori menu, non un refuso."""
    call = _script()
    call.extractions.append(recorded(items=[item("Speck", 3, remove=["rucola"])]))
    with call.open():
        call.session.add(MenuItem(name="Speck", category="pizza", pizza_type="Normale", price=19.9, restaurant_id="altro-locale"))
        call.session.commit()
        response = call.say("tre speck senza rucola")
        items = _normalized_items(call.conversation().items_json)
    assert (items[0]["name"], items[0]["add"]) == (PERSONAL, ["speck"])
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
