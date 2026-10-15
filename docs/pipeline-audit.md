# Audit pipeline vocale TakeIt — latenza

Data: 2026-10-15. Riferimenti a righe del codice dopo il commit che aggiunge la strumentazione.

## 1. Come arriva l'audio da Twilio

**Niente Media Streams / websocket.** La chiamata è pilotata a webhook TwiML:

- `POST /voice/incoming` (`app/routes/voice.py:1247`) risponde con `<Gather input="speech">` che contiene il saluto in `<Play>`.
- Twilio ascolta il cliente, fa lui il riconoscimento vocale e chiama `POST /voice/gather` (`voice.py:1542`) con il testo già trascritto in `SpeechResult`. Al server non arriva mai l'audio.
- La risposta è un nuovo TwiML: `<Gather>` + `<Play>URL</Play>` (`_build_response_twiml`, `voice.py:1118`), oppure `<Play>` + `<Hangup/>` / `<Dial>` negli stati finali.
- **Ramo filler** (`_needs_filler`, `voice.py:389`): per i messaggi che richiedono l'LLM, `/voice/gather` lancia `chat()` in background e risponde subito con `<Play>` di "Un momento..." (dalla cache, al massimo ogni 20 s: `_FILLER_COOLDOWN`, `voice.py:333`) + `<Redirect>` a `/voice/process` (`voice.py:1701`). `/voice/process` (`voice.py:1186`) attende il task (max 10 s, `voice.py:1213`) e restituisce il TwiML con la risposta vera.
- Audio: `<Play>` punta a `/voice/audio/{file}.mp3` (frasi in cache, `voice.py:994`) oppure a `/voice/stream/{id}` (`voice.py:1007`), che fa da proxy allo streaming ElevenLabs. Se ElevenLabs non è configurato si usa `<Say voice="Polly.Giorgio">`.

## 2. STT

- **Provider:** Twilio `<Gather input="speech">`, cioè il riconoscimento integrato di Twilio (motore Google).
- **Modello:** non specificato (`speechModel` assente) → modello di default di Twilio per `it-IT`. Nessun `enhanced`, nessun `hints`.
- **Streaming:** no, non per noi: riceviamo solo il risultato finale, nessun risultato parziale (`partialResultCallback` non usato).
- **Endpointing / VAD:** `_GATHER_ATTRS` (`voice.py:311-313`): `speechTimeout="auto"` (Twilio decide da solo quando il cliente ha finito; non è configurabile in ms), `timeout="5"` (5 s di silenzio iniziale prima del ramo no-input), `actionOnEmptyResult="true"`.

## 3. LLM

- **Provider/modello:** OpenAI Chat Completions, `gpt-4o-mini` (`OPENAI_MODEL`, `app/services/conversation_service.py:2313`), unica chiamata per turno in `extract_order_from_text` (`conversation_service.py:2930`, chiamata a `:2978`), invocata da `chat()` (`app/routes/chat.py:3806`).
- **Parametri:** `response_format={"type": "json_object"}`, `max_tokens=200`, `temperature=0`, `timeout=8`. Client httpx sincrono con keep-alive 60 s (`conversation_service.py:2337`).
- **Streaming:** **no**. La risposta è JSON strutturato (intent, items, nome, orario) che il backend usa per costruire la frase: il testo detto al cliente è generato da Python, non dall'LLM.
- **System prompt:** stato `collecting_items` ≈ 17.000 caratteri con il menu locale di 47 voci (`app/menu_data.json`) → **circa 4.000-4.500 token** (stima caratteri/4; il menu reale su Base44 può cambiarlo, il valore esatto è nel log `[OpenAI] ... tokens_in=`). Stati `collecting_name` / `collecting_pickup_time`: prompt ridotto ≈ 2.600 caratteri (~650 token). Il system prompt è statico per sfruttare il prefix caching di OpenAI; il contesto di sessione va nel messaggio utente.
- **Fast path:** molti stati (nome, orario, conferme, slot della pizza al taglio, data/ora prenotazione) sono risolti in Python senza LLM.
- **Tool call:** l'LLM **non usa tool calling** OpenAI. Durante un turno `chat()` fa però chiamate esterne sincrone, ora tracciate come "tool": menu e ristorante da Base44 (con cache), impasti, ricerca/aggiornamento cliente, disponibilità tavoli/prenotazioni, salvataggio prenotazione e ordine su Base44, SMS/WhatsApp Twilio. Le side effect dell'ordine (`_ORDER_SIDE_EFFECTS_EXECUTOR`, `chat.py:85`) girano fuori dal percorso critico e non vengono registrate.
- Dopo ogni risposta parte `_prefetch_openai_connection` (`voice.py:1087`), una chiamata fittizia `max_tokens=1` per tenere calda la connessione.

## 4. TTS

- **Provider/modello:** ElevenLabs, `eleven_flash_v2_5` (`ELEVENLABS_MODEL_ID`), `language_code="it"`.
- **Voce:** `ELEVENLABS_VOICE_ID` (variabile d'ambiente su Railway, non nel repository).
- **Formato:** nessun `output_format` passato → default ElevenLabs **MP3 44,1 kHz 128 kbps** (`audio/mpeg`). Non `ulaw_8000`.
- **Streaming:** sì verso ElevenLabs (`/v1/text-to-speech/{voice}/stream`, `voice.py:1031`), con proxy dei byte a Twilio (`StreamingResponse`, `voice.py:1084`) a blocchi da 4 KB (`aiter_bytes(chunk_size=4096)`, `voice.py:1057`). Lo stream parte solo quando Twilio richiede l'URL del `<Play>`, cioè dopo aver ricevuto il TwiML.
- **Transcodifica:** nessuna da parte nostra. È Twilio a decodificare l'MP3 e convertirlo in µ-law 8 kHz per la rete telefonica.
- **Cache:** 11 frasi frequenti pre-generate all'avvio (`_CACHED_PHRASES`, `voice.py:344`), più ogni testo già sintetizzato con `_synthesize_async` (TTL 24 h). Una risposta in cache non chiama ElevenLabs.

## 5. Testo LLM → TTS: a frasi o a risposta completa?

**A risposta completa.** L'LLM restituisce JSON intero; `chat()` costruisce la frase finale; solo allora il TwiML con `<Play>` parte verso Twilio, e solo dopo Twilio richiede l'audio. Non c'è segmentazione per frasi.

## 6. Regioni

- **Railway:** non configurata nel repository (nessun `railway.toml`/`railway.json`) e la CLI Railway qui non è autenticata. Va letta in Railway → servizio → Settings → Regions.
- **Twilio:** nessuna edge o region configurata nel codice (né `edge` sul client né sui webhook) → default Twilio **US1** (i webhook partono dagli Stati Uniti). Ogni turno fa almeno 2 giri HTTP Twilio→Railway (`/voice/gather` + richiesta dell'audio), 3 con il filler (`/voice/process`).

## 7. Le 3 cause di latenza più probabili

1. **Endpointing e STT di `<Gather>`.** Con `speechTimeout="auto"` Twilio aspetta una pausa di silenzio prima di chiudere il turno (in genere circa 1-2 s), poi trascrive tutto in blocco e solo allora chiama il webhook. È tempo che il cliente sente come attesa e che il server non vede: con questa architettura non è misurabile (vedi sotto).
2. **LLM senza streaming con prompt grande, in serie col TTS.** gpt-4o-mini con ~4.000+ token di system prompt e output JSON fino a 200 token va completato per intero prima che possa partire qualsiasi audio. Se il prefix cache OpenAI è freddo (primo turno dopo minuti di inattività) il tempo cresce. Nel ramo filler si aggiunge un giro Twilio in più (riproduzione del filler, `<Redirect>`, `/voice/process`).
3. **Consegna dell'audio: `<Play>` di un URL MP3.** Dopo il TwiML Twilio deve fare una nuova richiesta HTTP, il nostro proxy apre lo stream ElevenLabs, l'MP3 44,1 kHz va decodificato e convertito da Twilio. Non è documentato che `<Play>` riproduca un MP3 mentre lo scarica: se Twilio scarica tutto il file prima di suonarlo, il vantaggio dello streaming sparisce e conta l'intera sintesi (per questo misuriamo anche `tts_complete`). Si sommano i giri di rete Twilio US1 ↔ Railway.

## Strumentazione (Fase B)

Modulo `app/turn_latency.py`. Per ogni chiamata (chiave `call_sid`) i timestamp sono `time.monotonic()` in ms relativi all'arrivo di `/voice/incoming`. Tutto resta in memoria durante la chiamata; quando Twilio manda lo status finale a `/voice/status`, un task in background (`flush_call`) scrive i turni sul CallLog Base44. Il percorso della chiamata fa solo assegnazioni in un dict, nessun I/O.

| Timestamp | Dove viene preso | Note |
|---|---|---|
| `user_speech_end` | — | **null**: con `<Gather>` l'audio non arriva al server e Twilio non manda l'istante di fine parlato. Si può avere solo passando a Media Streams. |
| `stt_final` | arrivo di `POST /voice/gather` | testo finale disponibile al server |
| `llm_request_sent` | prima di `chat.completions.create` | null nei turni risolti in Python senza LLM |
| `llm_first_token` | — | **null**: senza streaming il primo token arriva insieme alla risposta completa |
| `llm_complete` | risposta OpenAI ricevuta (anche in errore, con `llm_error`) | |
| `tts_request_sent` | `/voice/stream`: apertura della richiesta a ElevenLabs | null se la risposta era in cache o con Polly |
| `tts_first_byte` | primo blocco da ElevenLabs | è il primo blocco da 4 KB (~250 ms di audio), non il primo byte in senso stretto: misurarlo davvero richiederebbe cambiare la dimensione dei blocchi inviati a Twilio |
| `first_audio_sent_to_twilio` | stream: primo blocco scritto sulla risposta HTTP a Twilio. Cache/Polly: costruzione del TwiML (Twilio scarica l'MP3 o sintetizza subito dopo) | `audio_source` dice quale caso |
| `tts_complete` | ultimo byte dello stream | extra, utile per la causa 3 |
| `filler_audio_sent` | TwiML con il filler | extra, solo ramo filler |
| `tools[]` | `name`, `start`, `end` delle chiamate esterne nel thread di `chat()` | decoratore `traced_tool` |

Altri campi per turno: `state`, `path` (`direct`/`filler`), `audio_source` (`stream`/`cache`/`polly`), `llm_model`, `llm_prompt_tokens`, `llm_cached_tokens`; `durations` (durata di ogni stadio); `perceived_latency` = `first_audio_sent_to_twilio − user_speech_end`, calcolata da `stt_final` finché `user_speech_end` è null (`perceived_from` lo dice). **Quindi la latenza percepita misurata esclude l'endpointing di Twilio (causa 1) e il tempo che Twilio impiega a suonare l'audio.**

Il saluto iniziale non è un turno del cliente e non viene registrato.

### Dove si salvano: campo `latency_turns` nel CallLog

Ho scelto un campo JSON (array di oggetti) sul CallLog esistente invece di una nuova entità TurnLog perché:
- una sola scrittura per chiamata (un update del record già esistente, trovato per `call_sid` come fa già `/voice/status`), invece di N create;
- nessuna nuova entità con le sue regole RLS da mantenere;
- il report legge una chiamata con un solo record.

Il limite è che non si possono filtrare i turni lato Base44. Per un report su qualche centinaio di chiamate non serve.

### Limiti noti

- Se il processo si riavvia durante una chiamata (deploy), i turni già raccolti si perdono; quelli successivi finiscono in una traccia marcata `partial: true` con tempi relativi al primo turno visto.
- Se Twilio non manda lo status callback, la traccia viene scartata dalla memoria dopo 2 ore.
- Se `/voice/stream` viene servito da un'altra istanza, i tempi TTS di quel turno restano null.

## Report (Fase C)

```
python scripts/latency_report.py -n 50 [--restaurant <id>]
```

Stampa mediana e p95 per stadio, per ogni tool e per la latenza percepita delle ultime N chiamate che hanno `latency_turns`.
