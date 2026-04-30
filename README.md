# takeit-local

Backend FastAPI per gestione menu, ordini, sessioni conversazionali e integrazioni voce/WhatsApp per una pizzeria locale.

## Panoramica del codebase

## Stack
- **FastAPI + SQLModel + SQLite** (database locale `takeit.db`).
- Routing organizzato in moduli sotto `app/routes/`.
- Logica di conversazione e sincronizzazione dati in `app/services/`.

## Entry point
- `app/main.py` avvia l'app FastAPI, monta statici, registra i router e svolge operazioni di startup (migrazioni SQL "best effort", sync menu, preload prompt/audio, check variabili Twilio).

## Variabili ambiente operative
- `OPENAI_API_KEY`: richiesta quando l'agente deve chiamare OpenAI.
- `ADMIN_API_KEY`: richiesta per `/menu`, `/orders`, `/logs` e `/owner-command`. Le dashboard la chiedono una volta e la salvano in `localStorage`.
- `TWILIO_AUTH_TOKEN`: richiesta per verificare le firme dei webhook `/voice/incoming`, `/voice/gather` e `/voice/process`.
- `SKIP_TWILIO_SIGNATURE_VALIDATION=true`: solo sviluppo locale, disattiva la verifica firma Twilio.
- `PUBLIC_BASE_URL`: URL pubblico usato per ricostruire la firma Twilio e generare gli URL audio.
- `SQL_ECHO=true`: abilita il log SQL dettagliato solo quando serve debugging.
- `VOICE_AUDIO_CACHE_TTL_SECONDS`: TTL opzionale della cache audio ElevenLabs, default 24 ore.
- `VOICE_AUDIO_CACHE_MAX_ITEMS`: limite opzionale di entry audio cached in memoria, default 128.

## Modello dati
- `MenuItem`: anagrafica del menu.
- `Order` + `OrderItem`: ordini e righe d'ordine.
- `ConversationSession` + `ConversationLog`: stato e storico della conversazione utente.

## Router principali
- `app/routes/menu.py`: CRUD menu e ricerca.
- `app/routes/orders.py`: elenco ordini e cambio stato.
- `app/routes/chat.py`: logica conversazionale (merge item, validazione, suggerimenti typo).
- `app/routes/voice.py`, `app/routes/tts.py`, `app/routes/owner_command.py`, `app/routes/sessions.py`, `app/routes/logs.py`: endpoint ausiliari per canali voce, sessioni e diagnostica.

## Criticità individuate

1. **Migrazioni fragili in startup**
   - In `app/main.py` le ALTER TABLE sono tentate e poi ignorate in caso d'errore con `except Exception: pass`.
   - Rischio: errori reali (schema inconsistente, lock DB, SQL errata) vengono nascosti.

2. **README storico vuoto / documentazione insufficiente**
   - Prima di questo aggiornamento il README conteneva solo il titolo, senza setup, architettura né workflow.

3. **Assenza di test automatici nel repository**
   - Non risultano suite `pytest` o test di regressione.
   - Rischio: bug sui flussi conversazionali e sui merge degli item non intercettati.

4. **Possibile duplicazione stato ordine non controllata da macchina a stati**
   - In `orders.py` lo status è validato contro una whitelist, ma senza regole di transizione (es. `completed -> preparing` potenzialmente consentito).

## Attività proposte (backlog mirato)

### 1) Correggere un refuso
- **Attività**: uniformare etichette e stringhe utente tra "menu"/"menù" e validare eventuali refusi nei messaggi conversazionali in `app/routes/chat.py` (es. testi suggerimenti e feedback disponibilità).
- **Deliverable**: patch di copy + test snapshot dei messaggi principali.

### 2) Correggere un bug
- **Attività**: introdurre migrazioni versionate (Alembic) e rimuovere il pattern `except Exception: pass` dalle ALTER TABLE in startup.
- **Deliverable**: script migration idempotenti, logging errori esplicito e fallback controllato.

### 3) Correggere un commento o discrepanza documentale
- **Attività**: mantenere il README allineato con gli endpoint reali e con le dipendenze runtime (Twilio/Base44), includendo sezione "come avviare" e "variabili ambiente richieste".
- **Deliverable**: checklist di coerenza docs + aggiornamento README a ogni aggiunta endpoint.

### 4) Migliorare un test
- **Attività**: creare prima suite `pytest` per funzioni pure in `app/routes/chat.py` (`merge_items`, `remove_items_from_order`, `apply_intent_to_items`) con casi edge su quantità, extra ingredienti, size e impasto.
- **Deliverable**: almeno 12 test parametrizzati e copertura minima del 70% sul modulo chat.

## Prossimi passi consigliati
1. Setup Alembic e baseline migration.
2. Introduzione test unitari su funzioni pure (priorità alta).
3. Test API integrati (`TestClient`) per `/orders` e `/menu`.
4. Hardening osservabilità startup (log strutturati).

## Valutazione estrazione ordine

L'harness `scripts/evaluate_order_extraction.py` valida i casi in `tests/fixtures/order_extraction_cases.json` senza chiamate esterne:

```bash
.venv/bin/python scripts/evaluate_order_extraction.py
```

Per controllare regressioni reali del modello si può usare la modalità live, che richiede `OPENAI_API_KEY`, misura la latenza per caso e può salvare un artifact JSONL confrontabile:

```bash
.venv/bin/python scripts/evaluate_order_extraction.py \
  --live \
  --case-id set_pickup_time_evening_ambiguous \
  --jsonl-output tmp/order-eval.jsonl \
  --max-latency-ms 2000
```

Usare `--fail-fast` durante il debug rapido e più `--case-id` quando si vuole isolare un comportamento specifico.


## Riduzione latenza (focus agente voce AI per pizzeria d'asporto)

### 1) Ridurre chiamate esterne nel turno conversazionale
- Evitare chiamate sincrone a servizi esterni durante il turno utente (OpenAI, Base44, ElevenLabs):
  - rispondere prima al cliente,
  - sincronizzare ordine/CRM in background con retry.
- Per Base44, usare un contatore ordine locale + riallineamento asincrono invece di calcolare sempre `count + 1` via rete.

### 2) Time budget stretto per ogni step
- Definire SLA per turni vocali: **< 1.2s** per risposta breve, **< 2.0s** per estrazione complessa.
- Impostare timeout più aggressivi e fallback rapidi (es. testo breve con Polly se TTS supera soglia).

### 3) Prompt e token optimization
- Ridurre il prompt ai soli campi necessari per lo stato corrente (`collecting_items`, `collecting_name`, ecc.).
- Usare output strutturato minimale (JSON compatto) e limitare il contesto storico ai turni utili.

### 4) Caching aggressivo TTS e frasi frequenti
- Estendere il prewarm di frasi ad alta frequenza e varianti orarie.
- Introdurre cache LRU con TTL per audio e pulizia periodica file vecchi in `/tmp/takeit_audio`.
- La cache voce usa TTL/LRU e mantiene pinned le frasi di prewarm più frequenti.

### 5) Parallelismo controllato
- Quando possibile, eseguire in parallelo:
  - lookup cliente,
  - normalizzazione input,
  - preparazione risposta TTS.
- Evitare blocchi CPU nel thread principale (spostare eventuali task pesanti in worker dedicati).

### 6) Telemetria obbligatoria per trovare i veri colli di bottiglia
- Misurare e loggare p50/p95/p99 per:
  - ASR/Twilio input,
  - chiamata LLM,
  - generazione TTS,
  - round-trip completo turno voce.
- Aggiungere `request_id/session_id` in tutti i log per correlare gli step.
- L'endpoint admin `GET /logs/latency` espone una finestra rolling in memoria
  con `count`, `min_ms`, `p50_ms`, `p95_ms`, `p99_ms` e `max_ms` per path chat/LLM.

### 7) Ottimizzazione del fallback voce
- Mantenere `Polly` come fallback immediato quando ElevenLabs non è pronto.
- In stati ad alta frequenza (conferme, prompt brevi) usare direttamente audio cached o fallback locale.

### 8) Piano pratico a impatto rapido (2 sprint)
1. **Sprint 1**: metriche latenza + timeout stretti + fallback rapido + async sync Base44.
2. **Sprint 2**: slimming prompt per stato + cache TTS LRU + test performance con target p95.
