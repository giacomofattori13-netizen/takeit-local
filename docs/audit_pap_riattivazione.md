# Audit riattivazione Pizza a Pezzi

Data: 2026-10-26. Prima stesura: solo diagnosi, nessun codice applicativo modificato.
Aggiornamento 2026-10-26: notifiche e SMS resi per locale (punto 4, buchi 2, 5 e 6; punto 5). Le parti risolte sono segnate con **RISOLTO**.
Aggiornamento 2026-10-29: percorso di SQLite configurabile con `DB_PATH` (punto 1). **In corso, manca il Volume su Railway.**
Aggiornamento 2026-10-29: riepilogo giornaliero degli ordini al titolare e `owner_order_sms_enabled` (punto 6).
Base44 appId `69c54bc5c44250d7da397903`. I record Restaurant sono stati letti in sola lettura dall'API Base44 il 2026-10-26 alle 13:00.

## Sintesi

| # | Tema | Stato | Rischio |
|---|------|-------|---------|
| 1 | CallLog | Su Base44, non su SQLite | Basso per il CallLog. **Alto** per l'outbox degli ordini (`OrderSideEffect`), che sta su SQLite. **In corso, manca il Volume su Railway**: il codice legge già `DB_PATH` |
| 2 | File di fallback | Contengono Pizza a Pezzi e vengono filtrati per tenant | Basso. `dough_data.json` è condiviso da tutti i locali |
| 3 | Tenant PaP | Il record esiste, ma `agent_phone` è un segnaposto e `agent_active` è false | Bloccante finché non si configura |
| 4 | Logica PaP | Le 5 domande e il preordine sono implementati | Medio: porzione chiesta solo per i tranci. **RISOLTO**: giorno di ritiro e nome/telefono del locale nell'SMS al cliente |
| 5 | Notifiche a Fabio | **RISOLTO**: SMS a `Restaurant.owner_notify_phone` a ogni nuovo ordine e per gli ordini non salvati, `OWNER_PHONE` solo come ripiego | Basso, se `owner_notify_phone` è compilato. Restano aperti i comandi SMS (un solo `OWNER_PHONE`) |
| 6 | Riepilogo giornaliero | Codice pronto, spento su Base44 (`daily_summary_enabled` null) | Basso. Da provare con l'anteprima prima di attivarlo |

---

## 1. CallLog: dove viene salvato

**Stato attuale**

- Il CallLog è un'entità **Base44** (`/entities/CallLog`), non SQLite.
  - Creazione e aggiornamento: `app/services/base44_client.py:312-360` (`create_call_log`, `update_call_log`, `find_call_log_by_sid` per `call_sid`).
  - Creazione all'inizio della chiamata con esito `abbandonata`: `app/routes/voice.py:129-160`, chiamata da `voice.py:1332`.
  - Chiamata con agente spento (`fuori_orario`), creata in un solo passo: `voice.py:99-126`, chiamata da `voice.py:1287`.
  - Esito reale dopo ordine o prenotazione: `voice.py:186-220` (chiamato da `voice.py:1235` e `voice.py:1762`).
  - Chiusura su riaggancio tramite callback Twilio `/voice/status`: `voice.py:223-265` e `voice.py:1522-1536`.
  - Latenze per turno (`latency_turns`) tenute in memoria e scritte sul CallLog a fine chiamata: `app/turn_latency.py:220-231`.
- Tutte le scritture sono fire-and-forget (`asyncio.create_task`). Se Base44 non risponde, il CallLog si perde e resta solo una riga nei log di Railway.
- **SQLite** (`sqlite:///./takeit.db` di default, `app/db.py`, sul filesystem del container; dal 2026-10-29 il percorso si imposta con `DB_PATH`) contiene invece le tabelle in `app/models.py`: `MenuItem`, `Order`, `OrderItem`, `OrderSideEffect`, `ConversationSession` e `ConversationLog`.
  - Nel repo non c'è nessuna configurazione Railway (`railway.json` o `Procfile`), quindi non posso verificare se è montato un Volume su quel percorso. **Se non c'è un Volume, il contenuto di `takeit.db` si perde a ogni deploy.** Questo riguarda `ConversationLog` (il log dei turni letto da `/logs`), le sessioni in corso e l'outbox `OrderSideEffect`, non il CallLog.

**Rischio**

- CallLog: basso. È su Base44 e sopravvive ai deploy. Unico punto debole: la scrittura è fire-and-forget e non viene ritentata.
- Outbox ordini: **alto**. Alla conferma, l'ordine viene salvato su Base44 in modo sincrono (`app/routes/chat.py:1440-1470`). Se Base44 non risponde, il salvataggio passa a un job `base44_order` in `OrderSideEffect` (SQLite), con retry e avviso al titolare dopo l'ultimo tentativo (`chat.py:1295-1330`). Se arriva un deploy mentre il job è in retry, il job sparisce: il cliente ha sentito "ordine confermato", ma l'ordine non arriva su Base44 e nessuno riceve l'avviso. Lo stesso vale per gli SMS di conferma al cliente in coda.
- `ConversationLog` e le sessioni si perdono a ogni deploy. Per la diagnosi a posteriori restano soltanto il CallLog (che contiene la trascrizione) e i log di Railway.

**Correzione proposta (non applicata)**

1. Verificare su Railway se sul servizio c'è un Volume montato sulla directory di lavoro, o in alternativa spostare `DATABASE_URL` sul percorso di un Volume, ad esempio `sqlite:////data/takeit.db` letto da una variabile d'ambiente.
2. Rispettare la regola già in vigore: nessun deploy dalle 17 alle 21. Prima di un deploy, controllare che in `OrderSideEffect` non ci siano job `pending` o `retry`.
3. Facoltativo: un retry breve (1-2 tentativi) su `create_call_log`.

**Aggiornamento 2026-10-26: ancora aperto.** La notifica al titolare per ogni nuovo ordine è un job `owner_new_order` nella stessa outbox su SQLite. Senza un Volume, anche una notifica in attesa di retry si perde con un deploy.

**Aggiornamento 2026-10-29: in corso, manca il Volume su Railway.** `app/db.py` legge il percorso del database da `DB_PATH` (senza la variabile resta `./takeit.db`), crea all'avvio la cartella se non esiste e scrive nei log `[Startup] Database SQLite: <percorso>`. Nessun altro punto del codice apre `takeit.db` con un percorso scritto a mano: tutti passano da `app.db`. Test: `tests/test_db_path.py`. Per chiudere il punto:

- su Railway, creare un Volume montato su `/data` e impostare `DB_PATH=/data/takeit.db`;
- dopo il deploy, verificare nei log la riga `[Startup] Database SQLite: /data/takeit.db`, poi fare un secondo deploy e controllare che sessioni e `ConversationLog` siano ancora lì.

---

## 2. File di fallback `restaurant_data.json` e `menu_data.json`

**A quale ristorante si riferiscono**

- `app/restaurant_data.json`: **Pizza a Pezzi** (`"id": "6a22d781b615baedb412be35"`), con orari 17-21, lunedì chiuso, `phone_orders_next_day_only: true` e `agent_active: true`. Sostituito nel commit `a00cb9f`.
- `app/menu_data.json`: **Pizza a Pezzi**, 47 voci, tutte con `restaurant_id = 6a22d781b615baedb412be35`. Sostituito nel commit `730635f`.
- `app/dough_data.json`: 5 impasti **senza `restaurant_id`**, quindi uguali per tutti i locali.

**Quando vengono serviti**

- Ristorante, `load_restaurant` in `app/services/conversation_service.py:963-1000`:
  - Ordine di lettura: prima la cache, poi Base44 (bloccante, con timeout breve). Il file si usa **solo se Base44 non risponde a cache vuota**.
  - Filtro per tenant (`conversation_service.py:992`): il file viene servito solo se `restaurant_id == ""` oppure se `local["id"] == restaurant_id`. Per Corte del Sole quindi non viene mai servito.
  - Il file entra in cache già scaduto, così al turno successivo si riprova Base44.
- Menu, `load_menu_from_base44` in `conversation_service.py:324-375`:
  - Con `restaurant_id`: prima Base44, poi il menu Base44 in cache anche se scaduto, e solo alla fine `_load_menu_from_file(restaurant_id)` (`conversation_service.py:301-311`), che **filtra le voci per `restaurant_id`**. Per Corte del Sole restituisce una lista vuota.
  - Con `restaurant_id == ""` (percorso legacy): il file viene servito sempre, così com'è.
- Impasti, `load_doughs` in `conversation_service.py:393-410`: il file viene servito a tutti, senza filtro.
- Risoluzione del tenant, `resolve_restaurant_from_phone` in `conversation_service.py:1044-1096`:
  - **Non usa mai i file.** Se Base44 non risponde, l'esito è `unavailable` e il chiamante sente il messaggio di servizio non disponibile (`voice.py:1264-1281`).
  - Quindi i file entrano in gioco solo dopo che il tenant è stato risolto da `agent_phone`.
- Altri usi dei file:
  - `menu_sync._restaurant_ids_to_sync` (`app/services/menu_sync.py:12-25`) ricava gli id dei locali dal file quando Base44 non risponde all'avvio.
  - `owner_command._resolve_restaurant_id` (`app/routes/owner_command.py:139-146`): se la richiesta non specifica `restaurant_id`, agisce sull'unico locale presente in `menu_data.json`, cioè Pizza a Pezzi.
  - `sms._toggle_item` e `owner_command._mirror_to_menu_file` scrivono sul file nel container. Le modifiche si perdono al deploy, ma Base44 resta la fonte di verità.

**Rischio**

- Basso per il routing tra tenant: il fallback tiene conto del tenant per menu e ristorante.
- Il file ristorante ha `agent_active: true`, mentre su Base44 oggi è `false`. Se Base44 non risponde a cache vuota, PaP risulterebbe attivo anche se è stato spento dal gestionale. Il caso è raro, perché a quel punto la risoluzione del tenant è già riuscita su Base44 pochi millisecondi prima e ha messo il record in cache (`conversation_service.py:1074`).
- I file sono un'istantanea: prezzi, orari e voci esaurite possono essere più vecchi di Base44.
- Il percorso con `restaurant_id == ""`, cioè `/chat` senza tenant e `fetch_and_save_restaurant()` all'avvio (`app/main.py:61`), usa il **primo** Restaurant restituito da Base44 (oggi Pizza a Pezzi) e il menu del file PaP. Le chiamate vocali non lo usano.

**Correzione proposta (non applicata)**

1. Prima del go-live, rigenerare i file con `scripts/export_menu.py`, così sono allineati a Base44.
2. Nel file ristorante, lasciare `agent_active` coerente con lo stato desiderato, oppure toglierlo dal file.
3. Valutare se rendere `dough_data.json` specifico per locale. Oggi gli impasti sono condivisi.

---

## 3. Tenant Pizza a Pezzi

**Stato attuale (Base44, letto il 2026-10-26)**

| Campo | Pizza a Pezzi | Corte del Sole |
|---|---|---|
| id | `6a22d781b615baedb412be35` | `69c54e24dededf37c50139ef` |
| agent_phone | **`+10000000000`** (segnaposto) | `+16067334996` |
| agent_active | **false** | true |
| phone_orders_next_day_only | true | null |
| reservations_enabled | false | true |
| opening_hours | lunedì closed, martedì-domenica 17:00-21:00 | tutti i giorni 19:00-22:00 |
| price_per_kg hot/cold | 19.9 / 18.5 | null |
| handoff_phone, phone, email, notification_email | null | valorizzati |

Il record esiste. Le chiamate non lo raggiungono, perché `+10000000000` non corrisponde a nessun numero Twilio. Anche se lo raggiungessero, l'agente risponderebbe "chiuso", perché `agent_active` è false (`voice.py:1284-1308`).

**Cosa cambia per rimetterlo su un numero diverso da Corte del Sole**

- **Codice: nessuna modifica necessaria.** Il routing è per `agent_phone`: Twilio manda `To`, e `match_restaurant_by_phone` (`app/services/base44_client.py:162-195`) confronta il numero normalizzato E.164, oppure le ultime 10 cifre.
- **Configurazione:**
  1. Su Twilio, acquistare o assegnare un numero dedicato e impostare i webhook:
     - Voice → `POST {PUBLIC_BASE_URL}/voice/incoming`
     - Status callback → `/voice/status`
     - Se si vogliono i comandi del titolare, SMS → `/sms/incoming`
     - Fare il tutto con la stessa firma `TWILIO_AUTH_TOKEN` dello stesso account.
  2. Su Base44, nel Restaurant PaP, impostare `agent_phone` = nuovo numero in formato E.164 e `agent_active = true`.
  3. Verificare `DEFAULT_RESTAURANT_ID` su Railway. Con due locali attivi viene ignorato (`conversation_service.py:1079-1092`), quindi diventa innocuo. Oggi invece, con un solo locale attivo, se è impostato su Corte del Sole manda a Corte del Sole qualunque numero non riconosciuto.
  4. Dopo l'aggiornamento non serve un deploy: la risoluzione legge Base44 a ogni chiamata.

**Rischio**

- Il match sulle ultime 10 cifre può confondere due numeri che hanno le stesse ultime 10 cifre ma prefissi internazionali diversi. Con un numero italiano e uno USA è improbabile, ma va controllato.
- Il menu PaP su Base44 deve contenere voci con `restaurant_id` di PaP e `available = true`, altrimenti si va sul fallback del file.

**Correzione proposta (non applicata)**

- Dopo la configurazione, fare una chiamata di prova e cercare nei log Railway la riga `[Restaurant] To=... → restaurant_id='6a22d781b615baedb412be35' (match=agent_phone)`.

---

## 4. Logica Pizza a Pezzi

**Le 5 domande obbligatorie**

Lo stato successivo è deciso da `determine_state` (`app/routes/chat.py:1639-1674`), in questo ordine:

1. **Gusti** (voci del carrello): si resta in `collecting_items` finché il carrello è vuoto o finché manca qualcosa (`chat.py:1649-1659`).
2. **Quantità, porzione piena/mezza e calda/fredda** per le voci al taglio (`sale_unit == "kg"`): slot `_KG_SLOTS` in `chat.py:2171-2264`, verificati in `chat.py:1661-1665`.
   - Domanda unica, per esempio "Quanti tranci, interi o mezzi? Caldi o freddi?": `_kg_slot_question` in `chat.py:2286-2318`.
   - Estrazione delle risposte: `_extract_temperature` (`chat.py:2085`) e `_extract_kg_size` (`chat.py:2100`).
   - Nessun valore predefinito.
3. **Nome**: `collecting_name` (`chat.py:1667-1668`).
4. **Ora di ritiro**: `collecting_pickup_time` (`chat.py:1670-1671`), validata sugli orari del giorno di preordine da `_check_pickup_time` (`chat.py:2640-2661`).
5. **Conferma finale**: `awaiting_confirmation`.

**Calcolo del giorno di preordine**

- `phone_orders_next_day_only` viene letto da `is_phone_orders_next_day_only` (`conversation_service.py:1030-1033`).
- Il giorno si fissa una volta per sessione e si salva in `ConversationSession.pickup_date` (`chat.py:2608-2624`).
- `get_next_open_day` (`conversation_service.py:1508-1526`) parte da domani e salta i giorni con `opening_hours` "closed". Quindi una chiamata di domenica porta a **martedì**, perché il lunedì è chiuso.
- Una richiesta "per stasera/oggi" viene respinta senza passare dall'LLM (`chat.py:3218-3250`).
- `pickup_date` viene salvato sull'Order Base44 (`chat.py:1458-1459`).

**Buchi trovati**

1. **La porzione piena/mezza è chiesta solo per gli ordini a tranci.** Per gli ordini a peso ("mezzo chilo di margherita") non viene chiesta: è una scelta voluta (`chat.py:2174-2175` e `chat.py:2194-2195`), ma non corrisponde al requisito "5 domande sempre". Da confermare con Fabio.
2. **Dopo 2 risposte non capite** (`KG_SLOT_MAX_ATTEMPTS = 2`, `chat.py:2184`) quantità, porzione o temperatura restano vuote e l'ordine viene **salvato comunque**, con `review_reasons` (`chat.py:2247-2275`). Il prezzo usa la tariffa fredda come stima (`chat.py:1146`). L'ordine non è bloccato: qualcuno deve leggere le `review_reasons` sulla dashboard.
3. **Se mancano gli `opening_hours`** (Base44 non risponde e il file non corrisponde), `get_next_open_day` restituisce domani senza controlli, anche se è lunedì (`conversation_service.py:1524-1526`).
4. **Chiusure straordinarie e festività** non sono gestite: esiste solo l'orario settimanale.
5. **RISOLTO.** **L'SMS di conferma al cliente non indicava il giorno di ritiro.** Il payload `whatsapp_confirmation` (`chat.py:1511-1517`) non contiene `pickup_date`, e il testo dice solo "Ritiro alle HH:MM" (`conversation_service.py:658-663`). Per un preordine il cliente può capire "stasera".
6. **RISOLTO.** **Nome e telefono del locale nell'SMS al cliente erano globali**: vengono dalle variabili d'ambiente `PIZZERIA_NAME` e `PIZZERIA_PHONE` (`conversation_service.py:650-651`), non dal Restaurant. Con due locali attivi, un cliente PaP riceve il nome configurato per tutti. L'SMS delle prenotazioni usa invece `Restaurant.name` (`conversation_service.py:2274`).

**Correzione proposta (non applicata)**

- Buco 1: decidere con Fabio se la porzione va chiesta anche per gli ordini a peso. In caso affermativo, togliere la condizione `unit == "tranci"` nello slot `portion`.
- Buco 2: **RISOLTO lato notifica.** La notifica a Fabio di un ordine con `review_reasons` inizia con "DA VERIFICARE" ed elenca i motivi. Resta da valutare se evidenziarlo anche in Cucina/OrdiniStaff.
- Buco 5: **RISOLTO.** Il job `whatsapp_confirmation` porta `pickup_date` (o, se manca, `order_date`) e `restaurant_id`. L'SMS dice "Ritiro martedì 3 novembre, ore 12:30" (`format_pickup_day` in `conversation_service.py`). Il fallback WhatsApp usa ancora il template Twilio con la sola ora.
- Buco 6: **RISOLTO.** `restaurant_contact(restaurant_id)` legge `Restaurant.name` e `Restaurant.phone` del tenant. `PIZZERIA_NAME` resta solo come ripiego quando il nome manca, con un warning nei log, e per i job accodati prima della modifica. `PIZZERIA_PHONE` non si usa più: senza `Restaurant.phone` la riga "Per modifiche chiama il..." viene omessa. Oggi `phone` è null per PaP. Anche l'SMS delle prenotazioni ora usa `Restaurant.phone`.
- Buchi 3 e 4: in mancanza di orari, chiedere conferma del giorno invece di assumere domani. Valutare un campo `closed_dates` sul Restaurant.

---

## 5. Notifiche a Fabio

**Stato attuale**

- **Per i nuovi ordini non parte nessuna notifica al titolare.** L'ordine arriva su Base44 e Fabio lo vede solo dalla dashboard (Cucina e OrdiniStaff, con `notification_sound` per locale, oggi null per PaP). Il campo `notification_email` è null per PaP e il backend non lo usa.
- L'unico messaggio al titolare è `send_owner_alert` (`conversation_service.py:830-858`), inviato **solo quando un ordine non viene salvato su Base44** dopo tutti i retry (`chat.py:1411-1416`, `chat.py:1311` e `chat.py:1327`).
  - **Destinazione**: variabile d'ambiente globale `OWNER_PHONE`.
  - **Canali**: prima WhatsApp da `TWILIO_WHATSAPP_FROM` (funziona solo entro 24 ore dall'ultimo messaggio del titolare), poi SMS da `TWILIO_NUMBER`.
  - Non dipende dal tenant: con due locali, gli avvisi di entrambi vanno allo stesso numero.
- Anche i comandi SMS del titolare (`app/routes/sms.py:247-253`) accettano un solo mittente, `OWNER_PHONE`, per tutti i locali. Il locale su cui agire si ricava dal numero chiamato (`To`).
- `handoff_phone` (trasferimento delle chiamate per gruppi numerosi) è null per PaP, ma le prenotazioni sono disattivate, quindi non serve.

**Rischio**

- **Alto**: se Fabio non tiene la dashboard aperta, non sa che è arrivato un preordine.
- Se `OWNER_PHONE` è il numero di chi sviluppa TakeIt o quello di Corte del Sole, gli avvisi "ordine non salvato" di PaP vanno alla persona sbagliata, e Fabio non può mandare comandi SMS.
- L'avviso "ordine non salvato" dipende dall'outbox su SQLite (punto 1): si perde con un deploy.

**Aggiornamento 2026-10-26: RISOLTO (punti 1 e 2 qui sotto)**

- **Nuovo ordine**: quando l'ordine è salvato su Base44 parte un job `owner_new_order`. Se è salvato alla conferma, il job viene accodato insieme alle conferme; se è salvato dal job di retry `base44_order`, viene accodato da quel job.
  - Il messaggio contiene numero d'ordine, cliente, giorno e ora di ritiro, voci con quantità e totale (`build_new_order_owner_message`).
  - Se l'ordine ha `review_reasons`, il messaggio inizia con "DA VERIFICARE".
- **Destinazione**: `Restaurant.owner_notify_phone` del locale dell'ordine (`owner_notify_phone()` in `conversation_service.py`). Se il campo è vuoto si usa `OWNER_PHONE`, con un warning `[OwnerAlert] WARNING owner_notify_phone vuoto ...` nei log. Lo stesso vale per l'avviso "ordine non salvato".
- **Canale**: lo stesso di prima, `send_owner_alert`: WhatsApp se `TWILIO_WHATSAPP_FROM` è configurato, altrimenti, o se WhatsApp fallisce, SMS da `TWILIO_NUMBER`.
- **Errori**: un invio fallito viene registrato nei log come `[OwnerNotify] invio fallito restaurant_id=... order_id=... order_number=...` e ritentato dall'outbox. Con la configurazione mancante non viene ritentato. Il salvataggio dell'ordine non dipende mai dalla notifica: se l'accodamento fallisce dentro `base44_order`, l'errore viene registrato e il job non viene ritentato, così non si crea un doppione.
- **Test**: `tests/test_owner_notifications.py`.
- **Da fare su Base44**: compilare `owner_notify_phone` (in formato E.164) per Pizza a Pezzi e per Corte del Sole.
- **Ancora aperto**: i comandi SMS del titolare accettano ancora un solo `OWNER_PHONE` per tutti i locali.
- **Aggiornamento 2026-10-29**: con `Restaurant.owner_order_sms_enabled = false` la notifica per ogni nuovo ordine non parte, tranne per gli ordini da verificare (vedi punto 6).

**Correzione proposta originale**

1. Aggiungere al Restaurant un campo `owner_phone` (o `notification_phone`) e usarlo al posto di `OWNER_PHONE` sia in `send_owner_alert` sia per autorizzare i comandi SMS, con `OWNER_PHONE` come ripiego.
2. Aggiungere un job `owner_new_order` nell'outbox: un SMS a Fabio per ogni preordine, con giorno, ora, voci e `review_reasons`.
3. In alternativa, o in aggiunta: account di Fabio su Base44 con OrdiniStaff filtrato sul suo locale (vedi la nota sulla dashboard già aperta: ID hardcoded e regole RLS sugli Order) e `notification_sound = true`.

---

## 6. Riepilogo giornaliero

**Aggiornamento 2026-10-29: codice pronto, da attivare su Base44.**

Fabio prende solo preordini per il giorno di apertura successivo, quindi la mattina gli serve un riepilogo unico per organizzare produzione e ritiri.

**Come funziona** (`app/services/daily_summary.py`)

- Un thread, avviato all'avvio accanto al DailyReset e con lo stesso meccanismo (thread daemon e `sleep`), rilegge i Restaurant da Base44 al massimo ogni 15 minuti, e all'orario di un locale attivo se arriva prima. Così un cambio di orario o un'attivazione su Base44 vale senza riavvio.
- Un locale riceve il riepilogo se:
  - `daily_summary_enabled = true` (null vale false);
  - oggi è un giorno di apertura secondo `opening_hours`. Senza orari il locale conta come aperto, come in `get_next_open_day`;
  - l'ora di Roma ha superato `daily_summary_time`. Se il campo è vuoto o non è "HH:MM", vale 11:00 e nei log compare `[DailySummary] WARNING daily_summary_time ...`;
  - `last_daily_summary_date` non è la data di oggi.
- **Destinazione**: `owner_notify_phone`, con ripiego su `OWNER_PHONE` e lo stesso warning delle notifiche (`owner_phone_for_restaurant`). **Canale**: solo SMS da `TWILIO_NUMBER`, niente WhatsApp.
- **Ordini inclusi**: gli Order del locale con `pickup_date` uguale a oggi, più quelli senza `pickup_date` con `order_date` di oggi. Sono esclusi gli stati annullato/cancellato e ritirato/completato/consegnato, e i record `is_sample`. Oggi su Base44 gli stati usati sono solo `nuovo` e `completato`: **`completato` conta come ritirato**.
- **Contenuto**, in quest'ordine:
  1. intestazione "Ordini di giovedì 29 ottobre — N ordini";
  2. "DA VERIFICARE" (solo se ci sono ordini con `needs_review`): ora, nome, telefono e `review_reason`;
  3. "TOTALI PER GUSTO": per ogni prodotto, kg (con il dettaglio caldo/freddo se le temperature sono più di una), tranci per porzione e temperatura, pezzi. Ad esempio "Bufala: 3,5 kg (calda 2 kg, fredda 1,5 kg) · 6 tranci interi freddi". Temperatura o porzione mancanti compaiono come "da definire";
  4. "RITIRI": un blocco per ordine, in ordine di ora di ritiro (quelli senza ora vanno in fondo), con le voci nello stesso formato dell'SMS al cliente. Gli ordini da verificare sono segnati "(DA VERIFICARE)";
  5. senza ordini: "Nessun ordine per oggi".
- **Lunghezza**: oltre 1500 caratteri il testo si divide in più SMS numerati "(1/3)", "(2/3)"..., senza spezzare un ordine.

**Idempotenza e recupero**

- Dopo l'invio si scrive `last_daily_summary_date` su Base44. Un riavvio nello stesso giorno non manda un secondo riepilogo.
- Se il servizio riparte dopo l'orario e il riepilogo di oggi non è partito, parte al primo controllo, cioè subito.
- Se Twilio fallisce, si ritenta al controllo successivo, fino a 3 tentativi al giorno.
- Se gli SMS partono ma la scrittura della data su Base44 fallisce, il processo ricorda l'invio e non lo ripete. **Rischio residuo**: un riavvio nello stesso giorno, prima che la data sia salvata, manda il riepilogo una seconda volta.

**`owner_order_sms_enabled`**

- Con `false`, la notifica per ogni nuovo ordine (`owner_new_order`) non parte e il job si chiude con il log `owner_order_sms_enabled=false: notifica saltata`.
- Gli ordini da verificare partono sempre: quelli con `review_reasons` e quelli con `ai_confidence` sotto 0,8, la stessa regola di `needs_review` su Base44.
- Con null o `true` il comportamento non cambia. L'avviso "ordine non salvato" non dipende da questo campo.

**Anteprima senza invio**

```
curl -H "X-Admin-Api-Key: $ADMIN_API_KEY" \
  "https://<servizio>/daily-summary/preview?restaurant_id=6a22d781b615baedb412be35&date=2026-10-30"
```

L'anteprima restituisce il testo degli SMS (`messages`), il numero di ordini, se il locale è aperto quel giorno, l'orario in uso e se il riepilogo è attivo. Scrive il testo anche nei log. Non invia nulla e non scrive su Base44. Senza `date` usa oggi. Prova del 2026-10-29 sui dati reali di PaP (1 ottobre, 3 ordini): totali "Bufala: 0,7 kg fredda · 2 tranci interi freddi" e tre ritiri alle 20:00.

**Attivazione** (`scripts/set_restaurant_field.py` aggiorna un solo campo e mostra il valore prima e dopo):

```
python scripts/set_restaurant_field.py 6a22d781b615baedb412be35 daily_summary_time 10:30
python scripts/set_restaurant_field.py 6a22d781b615baedb412be35 daily_summary_enabled true
python scripts/set_restaurant_field.py 6a22d781b615baedb412be35 owner_order_sms_enabled false   # facoltativo
```

**Test**: `tests/test_daily_summary.py` (contenuto, totali con unità e temperature miste, ordinamento, ordini da verificare in cima, zero ordini, split, giorno chiuso, idempotenza dopo riavvio, orario vuoto o non valido) e `OwnerOrderSmsEnabledTests` in `tests/test_owner_notifications.py`.

---

## Checklist prima della riattivazione

- [ ] Volume Railway su `/data` e `DB_PATH=/data/takeit.db` (punto 1: codice pronto, manca il Volume)
- [ ] Numero Twilio dedicato e webhook configurati (punto 3)
- [ ] Base44 PaP: `agent_phone` reale e `agent_active = true` (punto 3)
- [ ] Menu PaP su Base44 verificato e `scripts/export_menu.py` rieseguito (punto 2)
- [ ] Decisione con Fabio sulla porzione negli ordini a peso (punto 4)
- [x] Giorno di ritiro e nome del locale nell'SMS al cliente (punto 4)
- [x] Canale di notifica a Fabio per ogni ordine e numero del titolare per locale (punto 5)
- [ ] Base44: `owner_notify_phone` compilato per PaP e Corte del Sole, e `phone` per PaP (punto 5 e punto 4, buco 6)
- [ ] Chiamata di prova di domenica: il ritiro proposto deve essere martedì
- [ ] Riepilogo giornaliero: anteprima verificata con Fabio, poi `daily_summary_time` e `daily_summary_enabled` su Base44 (punto 6)
- [ ] Decidere con Fabio se spegnere l'SMS per ogni ordine (`owner_order_sms_enabled = false`) una volta attivo il riepilogo (punto 6)
