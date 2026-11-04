"""L'esito del CallLog riflette ciò che la sessione ha salvato.

Chiamata e7dd5553 (4 novembre): prenotazione per 4 confermata e salvata, poi due silenzi;
il CallLog è rimasto "nessun_ordine" / "Chiamata terminata: nessun input ricevuto".
Il silenzio passa dal vero endpoint /voice/gather. Base44 è simulato.
"""
import asyncio
import datetime
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

import app.routes.chat as chat_module
import app.routes.voice as voice_module
import tests.test_opening_hours_flow as hours
import tests.test_reservation_covers_flow as covers
from app.models import ConversationSession, Order
from app.services import base44_client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import recalc_call_log_outcomes as recalc  # noqa: E402


class _FakeRequest:
    async def form(self):
        return {"CallSid": "CA1"}


async def _silence(session, session_id, times=2):
    """`times` turni senza parlato su /voice/gather, poi attende gli aggiornamenti del CallLog."""
    twiml = ""
    for _ in range(times):
        response = await voice_module.voice_gather(
            _FakeRequest(), session_id=session_id, SpeechResult="", session=session,
        )
        twiml = response.body.decode()
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    await asyncio.gather(*pending)
    return twiml


class _CallLogMixin:
    def patch_call_log(self, engine):
        self.updates = []
        self.record = {"id": "cl-1", "session_id": "s-1", "started_at": "2026-11-04T17:15:16+00:00"}
        async def no_audio(*args, **kwargs):
            return "<Say>audio</Say>"

        async def no_verify(request):
            return None

        for p in (
            patch.object(voice_module, "_db_engine", engine),
            patch.object(voice_module, "_verify_twilio_request", no_verify),
            patch.object(voice_module, "_audio_element_async", no_audio),
            patch.object(base44_client, "find_call_log_by_sid", side_effect=lambda sid, **kw: dict(self.record)),
            patch.object(base44_client, "update_call_log", side_effect=lambda cid, data, **kw: self.updates.append(data)),
        ):
            p.start()
            self.addCleanup(p.stop)


class ReservationThenSilenceTests(_CallLogMixin, covers._ReservationHarness):
    def setUp(self):
        super().setUp()
        self.patch_call_log(self.engine)

    def _confirm(self):
        r = self.book("vorrei prenotare un tavolo", "domani", "alle 20", "4 persone", "Giacomo", "sì")
        self.assertEqual(r.state, "reservation_completed")
        return r

    def test_confirmed_reservation_followed_by_silence_stays_prenotazione(self):
        self._confirm()
        twiml = asyncio.run(_silence(self.session, "s-1"))
        self.assertIn("<Hangup/>", twiml)
        self.assertEqual(len(self.updates), 1)
        data = self.updates[0]
        self.assertEqual(data["outcome"], "prenotazione")
        self.assertIn("Prenotazione per 4 persone, 2026-10-02 alle 20:00", data["summary"])
        self.assertNotIn("nessun input", data["summary"])
        self.assertIn("Utente: vorrei prenotare un tavolo", data["transcript"])

    def test_summary_carries_the_reservation_reference(self):
        r = self._confirm()
        reference = json.loads(self.conversation().reservation_json)["reservation_id"]
        self.assertTrue(reference)
        asyncio.run(_silence(self.session, "s-1"))
        self.assertIn(f"(rif. {reference})", self.updates[0]["summary"])
        # Stesso riassunto del turno di conferma: l'esito non cambia tra i due aggiornamenti
        self.assertEqual(voice_module._call_log_outcome_for_result(r)[1], self.updates[0]["summary"])

    def test_silence_before_confirming_is_still_nessun_ordine(self):
        self.book("vorrei prenotare un tavolo", "domani", "alle 20", "4 persone", "Giacomo")
        asyncio.run(_silence(self.session, "s-1"))
        self.assertEqual(self.updates[0]["outcome"], "nessun_ordine")
        self.assertEqual(self.updates[0]["summary"], "Chiamata terminata: nessun input ricevuto")

    def test_hang_up_after_confirming_with_the_update_lost_is_prenotazione(self):
        self._confirm()
        asyncio.run(voice_module._call_log_close_from_status("CA1", "completed", "39"))
        data = self.updates[0]
        self.assertEqual(data["outcome"], "prenotazione")
        self.assertIn("Prenotazione per 4 persone", data["summary"])
        self.assertIn("(Twilio: completed)", data["summary"])
        self.assertEqual(data["duration_seconds"], 39)


class OrderThenSilenceTests(_CallLogMixin, unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)
        self.session.add(ConversationSession(session_id="s-1", state="completed", completed=True, restaurant_id="r"))
        self.session.add(Order(
            conversation_session_id="s-1", customer_name="Giacomo", pickup_time="20:00",
            order_number=12, base44_id="b44-order",
        ))
        self.session.commit()
        self.patch_call_log(self.engine)

    def test_confirmed_order_followed_by_silence_stays_ordine(self):
        asyncio.run(_silence(self.session, "s-1"))
        data = self.updates[0]
        self.assertEqual(data["outcome"], "ordine")
        self.assertEqual(data["order_id"], "b44-order")
        self.assertEqual(data["summary"], "Ordine #12 confermato")

    def test_hang_up_after_order_with_the_update_lost_is_ordine(self):
        asyncio.run(voice_module._call_log_close_from_status("CA1", "completed", "39"))
        data = self.updates[0]
        self.assertEqual((data["outcome"], data["order_id"]), ("ordine", "b44-order"))
        self.assertIn("Ordine #12 confermato", data["summary"])


class SessionWithoutRecordsTests(_CallLogMixin, unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)
        self.session.add(ConversationSession(session_id="s-1", state="collecting_items", restaurant_id="r"))
        self.session.commit()
        self.patch_call_log(self.engine)

    def test_no_input_call_is_nessun_ordine(self):
        asyncio.run(_silence(self.session, "s-1"))
        self.assertEqual(self.updates[0]["outcome"], "nessun_ordine")

    def test_first_silence_does_not_close_the_call(self):
        twiml = asyncio.run(_silence(self.session, "s-1", times=1))
        self.assertEqual(self.updates, [])
        self.assertIn("<Gather", twiml)


class _DateRetryBase(hours._CorteHarness):
    def message_on(self, day, restaurant=None):
        now = datetime.datetime(*day, 12, 0, tzinfo=hours.ROME)
        self.restaurant = restaurant or hours.CDS
        with patch.object(chat_module, "datetime", hours._frozen_datetime_module(now)):
            return chat_module._reservation_date_retry_message(hours.CDS_ID)


class DateRetryExampleTests(_DateRetryBase):
    def test_example_is_close_to_today(self):
        self.assertEqual(
            self.message_on((2026, 11, 4)),
            "Non ho capito la data. Può dirmi il giorno? Per esempio domani, o sabato 7 novembre.",
        )

    def test_friday_does_not_propose_tomorrow_twice(self):
        self.assertIn("Per esempio domani, o domenica 8 novembre.", self.message_on((2026, 11, 6)))

    def test_saturday_proposes_next_saturday(self):
        self.assertIn("Per esempio domani, o sabato 14 novembre.", self.message_on((2026, 11, 7)))

    def test_closed_days_are_not_proposed(self):
        # Chiuso giovedì e sabato: niente "domani" di mercoledì, niente sabato
        hours_week = {d: "19:00-22:00" for d in ("monday", "tuesday", "wednesday", "friday", "sunday")}
        message = self.message_on((2026, 11, 4), {**hours.CDS, "opening_hours": hours_week})
        self.assertIn("Per esempio venerdì 6 novembre.", message)

    def test_every_example_is_read_back_as_that_same_day(self):
        start = datetime.date(2026, 11, 1)
        for offset in range(30):
            today = start + datetime.timedelta(days=offset)
            now = datetime.datetime(today.year, today.month, today.day, 12, 0, tzinfo=hours.ROME)
            with self.subTest(today=today), patch.object(chat_module, "datetime", hours._frozen_datetime_module(now)):
                message = chat_module._reservation_date_retry_message(hours.CDS_ID)
                example = message.split(", o ")[1].rstrip(".")
                day = datetime.date.fromisoformat(chat_module._extract_reservation_date(example))
                self.assertEqual(chat_module._format_reservation_date_it(day.isoformat()), example)
                self.assertGreater(day, today)


class DateRetryFlowTests(covers._ReservationHarness):
    def test_unclear_date_uses_the_computed_example(self):
        self.say("vorrei prenotare un tavolo")
        r = self.say("boh non saprei")
        self.assertEqual(r.state, "collecting_reservation_date")
        # Giovedì 1 ottobre 2026 nel test: il prossimo sabato è il 3
        self.assertEqual(r.response_message, "Non ho capito la data. Può dirmi il giorno? Per esempio domani, o sabato 3 ottobre.")
        self.assertNotIn("maggio", r.response_message)


class RecalcScriptTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 11, 4, 18, 0, tzinfo=datetime.timezone.utc)

    def setUp(self):
        self.reservation = {
            "id": "res-1", "session_id": "s-res", "party_size": 4.0, "date": "2026-11-07",
            "time": "20:00", "status": "confermata",
        }
        self.order = {"id": "ord-1", "session_id": "s-ord", "order_number": 7.0}
        self.logs = [
            self._log("wrong-res", "s-res", "nessun_ordine"),
            self._log("wrong-ord", "s-ord", "abbandonata"),
            self._log("right-res", "s-res", "prenotazione"),
            self._log("nothing-saved", "s-empty", "nessun_ordine"),
            self._log("no-session", None, "nessun_ordine"),
            self._log("too-old", "s-res", "nessun_ordine", started="2026-09-01T10:00:00+00:00"),
        ]
        self.writes = []

        def fake_query(entity, query, timeout=10.0):
            source = {"Order": [self.order], "Reservation": [self.reservation]}[entity]
            return [r for r in source if r["session_id"] == query["session_id"]]

        class FrozenDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return RecalcScriptTests.NOW

        for p in (
            patch.dict(os.environ, {"BASE44_TOKEN": "t"}),
            patch.object(recalc, "fetch_all_call_logs", side_effect=lambda: self.logs),
            patch.object(base44_client, "query_entities", side_effect=fake_query),
            patch.object(base44_client, "update_entity", side_effect=lambda e, i, d, **kw: self.writes.append((e, i, d))),
            patch.object(recalc, "datetime", types.SimpleNamespace(
                datetime=FrozenDateTime, timedelta=datetime.timedelta, timezone=datetime.timezone,
            )),
        ):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _log(log_id, session_id, outcome, started="2026-11-04T17:15:16+00:00"):
        return {
            "id": log_id, "session_id": session_id, "outcome": outcome, "started_at": started,
            "summary": "vecchio", "caller_phone": "+393331234567",
        }

    def test_without_apply_lists_and_never_writes(self):
        with patch("builtins.print") as printed:
            self.assertEqual(recalc.main([]), 0)
        output = "\n".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertEqual(self.writes, [])
        self.assertIn("CallLog da correggere negli ultimi 30 giorni: 2", output)
        self.assertIn("'nessun_ordine' -> 'prenotazione'", output)
        self.assertIn("--apply", output)
        self.assertNotIn("+393331234567", output)

    def test_apply_writes_only_the_mismatched_logs_and_only_those_fields(self):
        self.assertEqual(recalc.main(["--apply"]), 0)
        self.assertEqual(self.writes, [
            ("CallLog", "wrong-res", {
                "outcome": "prenotazione",
                "summary": "Prenotazione per 4 persone, 2026-11-07 alle 20:00 (rif. res-1)",
            }),
            ("CallLog", "wrong-ord", {
                "outcome": "ordine", "summary": "Ordine #7 confermato", "order_id": "ord-1",
            }),
        ])

    def test_days_narrows_the_window(self):
        self.logs = [self._log("wrong-res", "s-res", "nessun_ordine", started="2026-10-20T10:00:00+00:00")]
        self.assertEqual(recalc.main(["--apply", "--days", "7"]), 0)
        self.assertEqual(self.writes, [])

    def test_group_request_is_marked_to_confirm(self):
        patch_data = recalc.expected_patch([], [{**self.reservation, "party_size": 12, "status": "da_confermare"}])
        self.assertIn("da confermare", patch_data["summary"])


if __name__ == "__main__":
    unittest.main()
