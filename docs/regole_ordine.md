# Regole d'ordine

Tutte le regole d'ordine trovate nel codice e nel prompt, una riga per regola, con il test che la copre. Aggiornato il 2026-11-07.

- **Fonti**: `chat.py` è `app/routes/chat.py`, `service` è `app/services/conversation_service.py`, `prompt` è il prompt di estrazione in `service`.
- **Casi golden**: ``caso `x` `` è un caso di `CASES` in `tests/test_order_rules_golden.py`. Gira con l'estrazione registrata in `test_golden_with_recorded_extraction` e con l'LLM vero, una run per caso, in `test_golden_with_real_llm`.
- **Comandi**: `pytest tests` senza rete; `pytest tests/test_order_rules_golden.py --run-llm -k real_llm` con l'LLM vero (circa un minuto). L'LLM vero si usa solo se il commit modifica il prompt o l'estrazione dell'ordine.
- **Controllo**: `test_rules_document_only_cites_tests_that_exist` fallisce se questo documento cita un test che non esiste, e `test_rules_document_cites_every_golden_case` se un caso golden non è citato qui.
- **Chiamate di prova**: ogni chiamata di test fallita si aggiunge ai casi golden con le sue parole esatte. I casi `reale_*` vengono dalle chiamate del 4 e 5 novembre a Corte del Sole; i `reale_pap_*` dalle chiamate di prova del 6 novembre a Pizza a Pezzi (menu in `tests/fixtures/pap_menu.json`).
- **NON COPERTA**: nessun test verifica la regola. Sono elencate in fondo.

## Ordine o prenotazione

| Regola | Fonte | Test |
|---|---|---|
| Decide il contenuto, non la parola "prenotare": un prodotto nominato è un ordine, anche con "prenotarmi" o "mi tiene da parte" | service `classify_call_intent`, prompt | casi `reale_prenotare_una_margherita`, `prenotarmi_una_capricciosa`, `mi_tiene_da_parte`, `tests/test_call_intent_flow.py::test_c2dbefef_a_margherita_is_an_order_not_a_table`, `tests/test_call_intent_flow.py::test_classifier` |
| "tavolo", "posti", "persone", "siamo in N": prenotazione. "Tavolo" vince su un prodotto; un prodotto vince su "persone" | service `classify_call_intent` | casi `reale_tavolo_dentro_un_ordine`, `siamo_in_quattro`, `tests/test_call_intent_flow.py::test_377c8aa0_a_real_table_is_still_a_table`, `tests/test_call_intent_flow.py::test_classifier` |
| Né prodotti né tavolo: "Vuole ordinare da asporto o prenotare un tavolo?", una sola volta | chat.py `INTENT_QUESTION` | casi `vorrei_prenotare_e_basta`, `reale_volevo_prenotare_per_domani_alle_8`, `tests/test_call_intent_flow.py::test_no_product_and_no_table_asks`, `tests/test_call_intent_flow.py::test_unknown_first_sentence_asks_once` |
| Saluto, nome del locale o frase generica: la stessa domanda, ripetuta se il cliente saluta di nuovo. Prenotazioni spente o preordine fisso: "Cosa desidera ordinare?" | chat.py, service `is_only_greeting` | casi `reale_salve`, `reale_corte_del_sole`, `reale_forte_del_sole`, `tests/test_call_intent_flow.py::test_5770365c_a_greeting_asks_order_or_table`, `tests/test_call_intent_flow.py::test_8d7db458_a_generic_sentence_asks_order_or_table`, `tests/test_call_intent_flow.py::test_greeting_then_an_order_or_a_table`, `tests/test_call_intent_flow.py::test_greeting_then_a_table`, `tests/test_call_intent_flow.py::test_greeting_after_the_question_keeps_what_was_said_before`, `tests/test_call_intent_flow.py::test_greeting_after_choosing_an_order_asks_what_to_order`, `tests/test_call_intent_flow.py::test_greeting_with_reservations_off_asks_what_to_order`, `tests/test_call_intent_flow.py::test_greeting_still_asks_what_to_order` |
| La risposta conserva giorno e orario detti prima | chat.py | `tests/test_call_intent_flow.py::test_the_answer_keeps_what_was_said_for_a_table`, `tests/test_call_intent_flow.py::test_the_answer_keeps_what_was_said_for_an_order`, `tests/test_call_intent_flow.py::test_regression_a_table_still_keeps_the_day` |
| Vale anche se la risposta è un prodotto, un prodotto storpiato che riconosce solo l'LLM, o "lo sport" (STT di "l'asporto"); il prodotto non si perde | chat.py `_keep_earlier_day_and_time`, `_ORDER_ANSWER_RE` | caso `reale_una_capricciosa_lo_sport`, `tests/test_call_intent_flow.py::test_32da8401_tomorrow_survives_an_order_and_the_new_time_wins`, `tests/test_call_intent_flow.py::test_regression_prenotare_for_tomorrow_then_an_order_saves_tomorrow`, `tests/test_call_intent_flow.py::test_regression_product_with_asporto_is_not_lost`, `tests/test_call_intent_flow.py::test_regression_lo_sport_alone_is_an_order`, `tests/test_call_intent_flow.py::test_regression_product_recognised_only_by_the_llm` |
| Giorno od orario nuovi nella risposta vincono su quelli di prima, ma un orario nuovo non cambia il giorno | chat.py | `tests/test_call_intent_flow.py::test_regression_new_time_with_the_product_keeps_the_day`, `tests/test_call_intent_flow.py::test_regression_a_new_day_in_the_answer_wins`, `tests/test_call_intent_flow.py::test_regression_tonight_in_the_answer_wins_and_is_saved_with_the_date` |
| Il giorno di prima passa dai controlli: chiuso si propone, giorno e data discordi si chiede | chat.py | `tests/test_call_intent_flow.py::test_regression_earlier_closed_day_is_proposed_not_taken`, `tests/test_call_intent_flow.py::test_regression_earlier_weekday_and_date_that_disagree_are_asked` |
| A cucina chiusa un prodotto risponde alla domanda; giorno e orario detti prima restano | chat.py | `tests/test_call_intent_flow.py::test_a_product_answers_the_closed_kitchen_question`, `tests/test_call_intent_flow.py::test_earlier_day_and_time_survive_the_closed_kitchen_question`, `tests/test_call_intent_flow.py::test_yes_alone_still_does_not_choose` |
| Da ordine a tavolo a metà chiamata: giorno, orario e nome restano, gli articoli non confermati no. Con articoli nel carrello serve la parola "tavolo" | chat.py | `tests/test_call_intent_flow.py::test_fb627c3b_corte_del_sole_then_a_table`, `tests/test_call_intent_flow.py::test_order_to_table_keeps_day_time_name_and_drops_the_items`, `tests/test_call_intent_flow.py::test_a_cart_is_not_dropped_for_persone_alone` |
| Da tavolo a ordine a metà chiamata: giorno, orario e nome restano. Il prodotto deve seguire un numero o un articolo, perché "Monti" può essere un nome | chat.py, service `mentions_product` | `tests/test_call_intent_flow.py::test_table_to_order_keeps_day_time_and_name`, `tests/test_call_intent_flow.py::test_correction_to_an_order_from_a_reservation` |
| Prenotazioni spente: "prenotare" senza prodotti riceve "solo ordini da asporto", con un prodotto è un ordine | chat.py | `tests/test_call_intent_flow.py::test_reservations_off_turns_prenotare_away_but_not_a_product` |
| Cucina chiusa e intento non noto: "La cucina stasera è chiusa. Vuole ordinare per domani o prenotare un tavolo?". Il sì da solo non sceglie | chat.py `_closed_kitchen_intent_question` | caso `reale_corte_del_sole_cucina_chiusa`, `tests/test_call_intent_flow.py::test_answers_to_the_closed_kitchen_question`, `tests/test_call_intent_flow.py::test_prenotare_answers_the_closed_kitchen_question` |
| Tavolo, poi un prodotto ("Vorrei una capricciosa da sport."): diventa un ordine con giorno e orario della prenotazione; "Sono per le 8.45." al riepilogo cambia l'orario | chat.py | casi `reale_vorrei_una_capricciosa_da_sport`, `reale_sono_per_le_8_45`, `tests/test_call_intent_flow.py::test_2736d6cb_table_then_order_is_preserved` |
| Cucina chiusa con un prodotto nominato: resta "vuole ordinare per domani?" | chat.py | `tests/test_call_intent_flow.py::test_a_product_at_closed_kitchen_keeps_the_usual_question` |

## "No" a una domanda

| Regola | Fonte | Test |
|---|---|---|
| Un "no", anche ripetuto, a una domanda aperta: "Va bene, cosa devo correggere?" | chat.py `_is_bare_no`, `CORRECTION_QUESTION` | caso `reale_no_alla_domanda_sulle_persone`, `tests/test_call_intent_flow.py::test_no_at_the_party_question_is_not_a_failure` |
| Mai la stessa frase dopo un "no": la seconda volta si elencano i dati, la terza si passa al locale | chat.py `_ask_correction` | `tests/test_call_intent_flow.py::test_repeated_no_never_repeats_and_goes_to_the_restaurant` |
| Terzo "no", o "no" a "Per quale giorno le serve?": richiesta salvata con `needs_review` "Cliente non capito dopo 3 correzioni", poi trasferimento a `handoff_phone` | chat.py `_callback_fallback`, `save_callback_request` | `tests/test_correction_callback.py::test_third_no_saves_what_was_said_and_dials_the_restaurant`, `tests/test_correction_callback.py::test_no_to_the_day_question_saves_the_order_details_and_dials` |
| Il locale non risponde: "La faccio richiamare dal locale appena possibile. A presto!" e fine chiamata, senza altre domande | voice.py `_dial_result_update`, chat.py `finish_callback_request` | `tests/test_correction_callback.py::test_no_answer_says_goodbye_and_keeps_one_request`, `tests/test_correction_callback.py::test_answer_without_pressing_one_counts_as_no_answer`, `tests/test_correction_callback.py::test_dial_result_hangs_up_with_the_goodbye` |
| Senza `handoff_phone` si salva e si saluta subito | chat.py `_callback_fallback` | `tests/test_correction_callback.py::test_without_a_handoff_phone_it_saves_and_says_goodbye` |
| Il locale preme 1: la richiesta resta, "passata al locale" | voice.py | `tests/test_correction_callback.py::test_restaurant_accepting_marks_the_request` |
| La richiamata si promette solo se è registrata e c'è un numero | chat.py `finish_callback_request` | `tests/test_correction_callback.py::test_failed_save_does_not_promise_a_callback`, `tests/test_correction_callback.py::test_no_caller_number_does_not_promise_a_callback` |
| Giorno, ora o persone non detti: segnaposto oggi, 00:00 e 0, dichiarati nelle note | chat.py `save_callback_request` | `tests/test_correction_callback.py::test_nothing_said_uses_placeholders_stated_in_the_notes` |
| CallLog: richiesta per un tavolo è `prenotazione`, per un ordine `nessun_ordine`, sempre col riferimento | voice.py `_session_saved_outcome_sync` | `tests/test_correction_callback.py::test_call_log_reflects_the_request` |
| La correzione vale su intento, prodotti, giorno, ora, persone e nome | chat.py | `tests/test_call_intent_flow.py::test_correction_of_each_field_in_a_reservation`, `tests/test_call_intent_flow.py::test_correction_with_the_value_goes_on`, `tests/test_call_intent_flow.py::test_correction_at_the_recap_rechecks_and_recaps`, `tests/test_call_intent_flow.py::test_order_correction_by_field`, `tests/test_call_intent_flow.py::test_order_correction_of_the_day_by_field` |
| "No" al riepilogo dell'ordine non annulla più: "annulla" e "no grazie" sì | chat.py | `tests/test_call_intent_flow.py::test_no_at_the_order_recap_does_not_cancel_the_order`, `tests/test_call_intent_flow.py::test_explicit_cancel_at_the_recap_still_cancels` |
| "No, per le 9" è una correzione; "nome" non contiene un "no" | chat.py | `tests/test_call_intent_flow.py::test_no_with_a_value_at_the_order_recap_is_a_correction`, `tests/test_call_intent_flow.py::test_no_with_a_value_in_a_reservation`, `tests/test_call_intent_flow.py::test_nome_at_the_recap_does_not_read_no_inside_the_word` |
| Il "no" conserva il suo significato dove è una risposta: proposta di orario o nome, giorno proposto, "Va bene interi?" | chat.py | `tests/test_call_intent_flow.py::test_no_keeps_its_meaning_on_a_proposal`, `tests/test_pickup_time_escalation.py::test_no_to_the_proposal_then_a_valid_time_is_used`, `tests/test_order_rules_golden.py::test_no_to_the_closed_day_proposal_asks_for_the_day_and_keeps_the_order` |

## Pizze fuori menu

| Regola | Fonte | Test |
|---|---|---|
| Una pizza fuori menu è una Margherita base con il nome detto come aggiunta, solo se le parole sono ingredienti del menu del locale o alias | prompt, service `is_ingredient`, chat.py | caso `melanzane_gorgonzola` |
| Il resto non diventa un'aggiunta: "Cosa desidera ordinare?" | service `is_ingredient`, chat.py | casi `non_ingrediente_kebab`, `non_ingrediente_porchetta`, `nome_di_persona_non_e_aggiunta` |
| Saluti e nome del locale non sono un ordine, neppure su una pizza del menu. "Corte del sole" non è la pizza Corte | service `is_only_greeting`, `strip_restaurant_name` | casi `reale_corte_del_sole`, `nome_del_locale_su_una_pizza_del_menu` |
| Anche se non sembra una pizza, con quantità, giorno e orario | prompt | caso `salamino_domani_alle_8` |
| Più parole dette sono più aggiunte, nell'ordine | prompt | caso `melanzane_salamino_gorgonzola` |
| "senza X" va nelle rimozioni e non toglie il nome dalle aggiunte | prompt | casi `wurstel_senza_pomodoro`, `salamino_senza_mozzarella`, `due_tonno_senza_cipolla`, `salamino_e_rucola_senza_pomodoro`, `speck_senza_origano` |
| Nome perso dall'LLM con una rimozione: si recupera dalla frase | service `_restore_off_menu_names` | `tests/test_order_rules_golden.py::test_off_menu_name_lost_by_the_llm_is_recovered_from_the_sentence`, caso `salsiccia_senza_rucola` |
| Le aggiunte dette dall'LLM non si sovrascrivono | service | `tests/test_order_rules_golden.py::test_additions_said_by_the_llm_are_never_overwritten` |
| Nome fuori menu restituito dall'LLM: diventa Margherita + nome | chat.py | `tests/test_order_rules_golden.py::test_off_menu_name_returned_by_the_llm_becomes_margherita_with_the_addition` |
| Nome venduto solo da un altro locale: per questo locale è fuori menu | chat.py | `tests/test_order_rules_golden.py::test_item_sold_only_by_another_restaurant_is_off_menu_here` |
| Nome simile a una voce del menu (refuso, soglia 0,82): "Forse intendevi…" | chat.py | `tests/test_order_rules_golden.py::test_typo_of_a_menu_item_keeps_the_suggestion` |
| Voce esaurita: si rifiuta, non diventa una Margherita | chat.py | `tests/test_order_rules_golden.py::test_sold_out_menu_item_is_refused_and_not_turned_into_a_margherita` |
| A voce "margherita con X", mai "pizza personalizzata" | chat.py `format_single_item_for_customer` | `tests/test_order_rules_golden.py::test_off_menu_pizza_is_spoken_as_margherita`, `tests/test_order_rules_golden.py::test_recap_of_the_real_call_never_says_personalizzata` |
| Margherita senza pomodoro: a voce "bianca" | chat.py | `tests/test_order_rules_golden.py::test_off_menu_pizza_is_spoken_as_margherita`, caso `bianca_con_prosciutto` |
| Nell'SMS al cliente: "Margherita" con le aggiunte a parte | service `_build_pizza_lines` | `tests/test_order_rules_golden.py::test_customer_sms_shows_margherita_plus_the_addition` |
| Su Base44: `pizza_name` "Margherita" con le aggiunte in `add_ingredients` | `order_sync.py` `build_order_items` | `tests/test_order_rules_golden.py::test_off_menu_is_saved_on_base44_as_margherita_with_the_additions`, `tests/test_order_rules_golden.py::test_menu_names_are_saved_on_base44_unchanged` |
| Riepilogo giornaliero: somma per "Margherita +<aggiunte>", anche con gli ordini salvati prima come "Personalizzata" | `daily_summary.py` `_flavour_name` | `tests/test_order_rules_golden.py::test_daily_summary_sums_margherite_by_additions`, `tests/test_order_rules_golden.py::test_daily_summary_sums_orders_saved_before_the_rename_with_the_new_ones` |
| Una pizza fuori menu non entra nelle "solite" | chat.py | `tests/test_order_rules_golden.py::test_off_menu_pizza_is_not_stored_as_a_favourite`, `tests/test_order_rules_golden.py::test_favourite_saved_as_personalizzata_is_never_proposed`, `tests/test_order_rules_golden.py::test_other_favourites_are_still_proposed` |
| `custom_pizza_enabled` null o assente vale true | service `is_custom_pizza_enabled` | `tests/test_order_rules_golden.py::test_custom_pizza_flag_defaults_to_true`, `tests/test_order_rules_golden.py::test_custom_pizza_null_still_becomes_margherita` |
| `custom_pizza_enabled` false: "<nome> non è nel menu, abbiamo <3 gusti>. Quale vuole?", niente Margherita. "Oggi non c'è" solo per un prodotto del menu non disponibile | chat.py `_off_menu_question` | caso `fuori_menu_rifiutata`, `tests/test_order_rules_golden.py::test_96bfebc7_the_same_unresolved_answer_twice_changes_strategy_then_hands_over`, `tests/test_order_rules_golden.py::test_off_menu_refused_names_three_similar_flavours_and_saves_nothing`, `tests/test_order_rules_golden.py::test_off_menu_refused_when_the_llm_returns_the_bare_name` |
| Nome contenuto in un solo prodotto del menu: "Intende Pepe Rosa?"; sì → il prodotto, no → i gusti simili | chat.py `_only_menu_name_containing` | caso `reale_pap_rosa`, `tests/test_order_rules_golden.py::test_ca96b70e_rosa_yes_adds_pepe_rosa_and_asks_what_is_missing`, `tests/test_order_rules_golden.py::test_ca96b70e_no_to_pepe_rosa_offers_similar_flavours` |
| Mai la stessa domanda all'infinito: domanda, ripetuta una volta, poi "Intende <gusto più simile>?" con sì/no; "no" o ancora non risolta → al locale | chat.py `off_menu_asked`, `off_menu_proposal` | `tests/test_order_rules_golden.py::test_96bfebc7_the_same_unresolved_answer_twice_changes_strategy_then_hands_over`, `tests/test_order_rules_golden.py::test_96bfebc7_no_to_the_changed_strategy_hands_over_to_the_restaurant` |
| `custom_pizza_enabled` false: un nome che sembra un prodotto ma non è nel menu né tra gli ingredienti ("una capricciosa") non si scarta con "Cosa desidera ordinare?": riceve la domanda con i gusti più vicini e lo stesso anti-loop. Saluti, nome del locale e nome del cliente restano esclusi. Un turno senza prodotti non azzera il conteggio | chat.py `_looks_like_product_name`, `off_menu_asked` | caso `reale_pap_capricciosa`, caso `reale_pap_una_diavola`, `tests/test_order_rules_golden.py::test_2715b8a1_capricciosa_never_loops_on_what_to_order`, `tests/test_order_rules_golden.py::test_a_turn_without_products_does_not_restart_the_off_menu_count` |
| I 3 gusti sono i più simili per nome e ingredienti; a pari somiglianza il più semplice | chat.py `_similar_menu_flavours` | `tests/test_order_rules_golden.py::test_similar_flavours_look_at_names_and_ingredients`, `tests/test_order_rules_golden.py::test_simplest_flavour_wins_a_tie` |
| Il rifiuto lascia altre pizze, giorno e orario | chat.py | caso `fuori_menu_rifiutata_giorno_e_orario_restano`, `tests/test_order_rules_golden.py::test_off_menu_refused_keeps_the_other_pizzas_day_and_time` |
| La quantità detta vale per il gusto scelto, se non ne dice un'altra | chat.py | `tests/test_order_rules_golden.py::test_chosen_flavour_gets_the_quantity_said_before`, `tests/test_order_rules_golden.py::test_chosen_flavour_with_its_own_quantity_keeps_it` |
| A cucina chiusa si concorda prima il giorno, poi la pizza che non c'è | chat.py | `tests/test_order_rules_golden.py::test_off_menu_refused_at_closed_kitchen_agrees_the_day_first` |
| Il flag non tocca le pizze del menu con aggiunte né i refusi | chat.py | caso `flag_spento_pizza_del_menu_con_aggiunta`, `tests/test_order_rules_golden.py::test_sold_out_and_typos_are_not_affected_by_the_flag` |

## Prezzi

| Regola | Fonte | Test |
|---|---|---|
| Fuori menu: prezzo Margherita + €2,00 per ogni aggiunta, per ogni pizza | chat.py `enrich_items_with_pricing` | `tests/test_order_rules_golden.py::test_off_menu_price_is_margherita_plus_two_euro_per_addition` |
| L'ordine salvato porta base, extra e totale | chat.py | `tests/test_order_rules_golden.py::test_off_menu_order_is_saved_with_the_addition_and_the_price` |
| Aggiunta su una pizza del menu: +€2,00; supplemento impasto; regole condivise | chat.py, service | `tests/test_chat_logic.py::test_enrich_items_with_pricing_uses_shared_rules` |
| Prezzi sempre del menu del locale della chiamata | chat.py | `tests/test_restaurant_pricing.py::test_cds_items_use_cds_prices_even_when_name_exists_at_pap`, `tests/test_restaurant_pricing.py::test_item_of_other_restaurant_is_not_priced` |
| Al taglio (sale_unit kg): tariffa al kg calda o fredda come `base_price`, nessun `total_price` né `total_amount`, anche a peso: si pesa al ritiro | chat.py `enrich_items_with_pricing` | `tests/test_restaurant_pricing.py::test_full_pap_order_by_weight_cold_and_hot_for_tomorrow`, `tests/test_restaurant_pricing.py::test_pap_prices_unchanged`, `tests/test_order_rules_golden.py::test_96bfebc7_kg_order_is_saved_without_any_total`, `tests/test_next_day_preorder_flow.py::test_call_in_slices_asks_only_what_is_missing_and_recap_matches_saved_order` |
| Preordine fisso, "quanto costa?" al riepilogo: al taglio prezzo al chilo (caldo/freddo dell'ordine) e "si paga a peso al ritiro"; a pezzo il totale. Poi "Confermo l'ordine?" | chat.py `_recap_price_answer` | casi `reale_pap_quanto_costa_al_riepilogo`, `reale_pap_quanto_costa`, `tests/test_order_rules_golden.py::test_3b55138c_price_and_same_time_at_the_recap_are_answered_then_confirmed`, `tests/test_order_rules_golden.py::test_price_at_the_recap_of_pieces_gives_the_total` |
| Domanda sul prezzo al taglio: tariffe al kg e ripete la domanda aperta | chat.py, prompt | `tests/test_next_day_preorder_flow.py::test_price_question_gives_kg_prices_and_repeats_the_open_question` |

## Prodotti, aggiunte e rimozioni

| Regola | Fonte | Test |
|---|---|---|
| Quantità detta con la pizza; quantità intere senza decimali | prompt, service | caso `impasto_per_tutte`, `tests/test_next_day_known_info.py::test_whole_quantity_becomes_int_and_weight_stays_decimal` |
| "con X" è un'aggiunta, mai parte del nome | prompt | caso `capricciosa_con_rucola` |
| "senza X" è una rimozione | prompt | caso `capricciosa_senza_olive` |
| Aggiunte e rimozioni nella stessa frase | prompt | caso `aggiunta_e_rimozione` |
| Alias fonetici ("wurstel" → "würstel", "verde" → "gorgonzola"). Mai dentro un nome del menu detto per intero ("verde e noci" resta Verde e Noci) | service `PIZZA_ALIASES`, `_apply_aliases` | caso `alias_verde_gorgonzola`, caso `reale_pap_verde_e_noci_e_mezzo_salmone_fritto`, `tests/test_order_rules_golden.py::test_96bfebc7_an_exact_menu_name_is_never_changed_by_an_alias`, caso `wurstel_senza_pomodoro`, `tests/test_extraction_validation.py::test_extract_order_masks_alias_and_item_logs`, `tests/test_order_rules_golden.py::test_alias_applies_where_the_word_is_not_on_the_menu` |
| Un alias non vale se la parola è una voce del menu del locale ("bondola" a CdS è la pizza, €11,00) | service `_apply_aliases` | casi `bondola_e_una_pizza_del_menu`, `due_bondola_con_orario`, `tests/test_order_rules_golden.py::test_alias_is_skipped_when_the_word_is_on_the_menu_of_the_restaurant`, `tests/test_order_rules_golden.py::test_bondola_is_the_pizza_at_its_menu_price` |
| Stessa pizza e stesso impasto: si sommano le quantità | chat.py | `tests/test_chat_logic.py::test_merge_same_item_accumulates_quantity` |
| Misura diversa: voci separate | chat.py | `tests/test_chat_logic.py::test_merge_different_size_keeps_separate_items` |
| Si estrae solo dall'ultimo messaggio | prompt | caso `per_le_8` (il carrello resta invariato) |
| Quantità dichiarata ("2 pizze"): si raccoglie fino a quel numero | chat.py `determine_state` | `tests/test_chat_logic.py::test_determine_state_waits_for_declared_quantity` |
| Cliente abituale e "2 pizze": "Le preparo le solite?", giorno e orario restano | chat.py | `tests/test_next_day_known_info.py::test_day_and_time_survive_the_usual_question`, `tests/test_order_rules_golden.py::test_usual_order_recap_names_the_day` |

## Impasto e misura

| Regola | Fonte | Test |
|---|---|---|
| Impasto detto con la pizza; senza, "classica" | prompt | caso `margherita_integrale` |
| L'impasto vale solo per la sua pizza | prompt | caso `impasto_solo_alla_sua_pizza` |
| "Tutte napoletane" vale per tutte le pizze della frase | prompt | caso `impasto_per_tutte` |
| Impasto non valido: "classica" | service | `tests/test_extraction_validation.py::test_invalid_dough_falls_back_to_classica` |
| "Mini": −€1,50 sulla base | prompt, chat.py | caso `margherita_mini`, `tests/test_chat_logic.py::test_enrich_items_with_pricing_uses_shared_rules` |
| "Doppio impasto": +€2,00 | prompt, chat.py | caso `capricciosa_doppio_impasto`, caso `doppia_pasta_e_impasto` |

## Carrello

| Regola | Fonte | Test |
|---|---|---|
| "Togli la X": rimuove quella pizza | prompt, chat.py | caso `togli_una_pizza` |
| "Togli l'ultima": rimuove l'ultima | prompt, chat.py | caso `togli_l_ultima` |
| Rimozione con quantità residua: restano le modifiche | chat.py | `tests/test_chat_logic.py::test_remove_items_preserves_modifiers_when_quantity_remains` |
| "Fai X invece": sostituisce | prompt, chat.py | caso `sostituisci` |
| "Cancella tutto": svuota il carrello, nome e orario restano | prompt, chat.py | caso `cancella_tutto` |
| "Annulla l'ordine": azzera anche nome, orario e giorno | prompt, chat.py | caso `annulla_ordine`, `tests/test_order_rules_golden.py::test_cancel_forgets_the_day` |

## Giorno di ritiro

| Regola | Fonte | Test |
|---|---|---|
| Un giorno detto vale anche a cucina aperta | chat.py `_apply_spoken_day` | caso `per_domani_a_cucina_aperta`, caso `margherita_domani_alle_8`, `tests/test_order_rules_golden.py::test_tomorrow_with_open_kitchen_is_saved_for_tomorrow_and_the_recap_says_so` |
| Vale per domani, dopodomani, giorno della settimana, data | chat.py `_spoken_pickup_day` | caso `giorno_della_settimana`, caso `data`, `tests/test_next_day_known_info.py::test_day_after_tomorrow_is_not_tomorrow` |
| "Stasera" vale oggi; annulla un giorno detto prima | chat.py | caso `stasera_alle_8`, `tests/test_order_rules_golden.py::test_tonight_at_the_recap_cancels_a_day_said_earlier` |
| Data già passata: l'anno prossimo; "8 2" non è una data | chat.py | `tests/test_order_rules_golden.py::test_past_date_means_next_year` |
| Giorno della settimana di oggi: oggi finché restano orari | chat.py | `tests/test_order_rules_golden.py::test_today_weekday_means_today_while_slots_are_left` |
| Giorno della settimana di oggi a cucina chiusa: settimana prossima | chat.py | `tests/test_order_rules_golden.py::test_today_weekday_means_next_week_when_the_kitchen_is_closed` |
| Giorno detto alla domanda sull'orario: vale, e non conta come risposta mancata | chat.py | `tests/test_order_rules_golden.py::test_day_at_the_pickup_time_question_is_respected`, `tests/test_order_rules_golden.py::test_day_alone_at_the_pickup_time_question_is_not_a_failure` |
| Giorno detto alla domanda sul nome: vale, e non è un nome | chat.py | `tests/test_order_rules_golden.py::test_day_at_the_name_question_is_not_a_name` |
| "Domenica" come nome non è un giorno | chat.py | `tests/test_order_rules_golden.py::test_weekday_used_as_a_name_is_not_a_day`, `tests/test_order_rules_golden.py::test_weekday_as_a_name_inside_an_order_is_not_a_day` |
| Correzione al riepilogo ("no, per domani"): aggiorna, non conferma né annulla | chat.py | `tests/test_order_rules_golden.py::test_day_change_at_the_recap_updates_without_confirming_or_cancelling` |
| Giorno chiuso: "<giorno> siamo chiusi, il primo giorno disponibile è <giorno>, va bene?" | chat.py `_closed_day_question` | caso `domani_chiuso_propone_martedi`, `tests/test_order_rules_golden.py::test_closed_named_weekday_proposes_the_next_open_day` |
| Giorno chiuso: si attende sì/no, poi vale il giorno proposto | chat.py | `tests/test_order_rules_golden.py::test_closed_tomorrow_on_sunday_proposes_tuesday_and_waits` |
| "No" al giorno proposto: "Per quale giorno le serve?", l'ordine resta | chat.py | `tests/test_order_rules_golden.py::test_no_to_the_closed_day_proposal_asks_for_the_day_and_keeps_the_order`, `tests/test_order_rules_golden.py::test_no_to_the_next_day_question_with_the_kitchen_closed_asks_for_the_day`, `tests/test_next_day_known_info.py::test_no_asks_for_the_day_and_keeps_order_and_time` |
| La risposta riapplica le regole del locale (giorno chiuso, stasera) | chat.py | `tests/test_order_rules_golden.py::test_no_then_a_closed_day_reapplies_the_rules`, `tests/test_order_rules_golden.py::test_no_then_tonight_with_the_kitchen_open_is_for_today` |
| "Sì" a "Per quale giorno le serve?" non è una risposta | chat.py | `tests/test_order_rules_golden.py::test_yes_to_the_day_question_is_not_an_answer` |
| "No grazie" o "lasci stare" a "Per quale giorno le serve?": il cliente rinuncia, chiusura senza salvare. Il "no" da solo va al locale | chat.py | `tests/test_order_rules_golden.py::test_explicit_decline_of_the_day_question_closes_the_call_without_saving`, `tests/test_opening_hours_flow.py::test_customer_says_no_twice_and_the_call_closes`, `tests/test_correction_callback.py::test_no_to_the_day_question_saves_the_order_details_and_dials` |
| Risposta alla proposta con un altro giorno aperto: vale quello | chat.py | `tests/test_order_rules_golden.py::test_answer_with_another_day_wins_over_the_proposal` |
| Risposta con un altro giorno chiuso: nuova proposta | chat.py | `tests/test_order_rules_golden.py::test_answer_with_another_closed_day_gets_a_new_proposal` |
| "Stasera" come risposta alla proposta: resta oggi | chat.py | `tests/test_order_rules_golden.py::test_tonight_as_answer_to_the_proposal_keeps_today` |
| Le prenotazioni non toccano il giorno di ritiro | chat.py | `tests/test_order_rules_golden.py::test_reservation_for_tomorrow_does_not_touch_the_pickup_day` |
| Stasera chiesto a cucina chiusa, alla domanda sul giorno: "Stasera purtroppo la cucina è chiusa. Va bene per domani, venerdì?". Il giorno cambia solo dopo il sì, mai con "Perfetto" prima | chat.py `_nd_today_refused` | caso `reale_stasera_a_cucina_chiusa`, `tests/test_call_intent_flow.py::test_00a50cc0_tonight_is_refused_confirmed_and_the_time_is_kept`, `tests/test_call_intent_flow.py::test_tonight_with_a_product_keeps_the_product_and_waits_for_yes`, `tests/test_call_intent_flow.py::test_ordinare_per_stasera_is_refused_too`, `tests/test_call_intent_flow.py::test_no_to_the_proposal_asks_the_day_and_moves_nothing` |
| Un prodotto al posto del sì alla proposta del giorno: vale come sì, e il prodotto entra | chat.py | `tests/test_call_intent_flow.py::test_known_defect_a_product_instead_of_yes_is_yes_and_the_product` |
| "Per stasera alle 8" senza prodotto alla domanda ordine/tavolo a cucina chiusa: "Stasera purtroppo la cucina è chiusa. Vuole ordinare per domani o prenotare un tavolo?", l'orario resta | chat.py | `tests/test_call_intent_flow.py::test_known_defect_b_tonight_at_8_without_a_product_is_refused_and_the_choice_asked` |
| L'orario detto con "stasera" resta per il giorno confermato, anche se l'LLM non lo estrae | chat.py `_propose_day` | `tests/test_call_intent_flow.py::test_00a50cc0_tonight_is_refused_confirmed_and_the_time_is_kept` |
| Orario detto che non vale nel giorno nuovo: "Alle 21:30 domani venerdì non è possibile, siamo aperti …. Va bene alle 19? Mi dica sì o no." | chat.py `_refused_time_question` | `tests/test_call_intent_flow.py::test_a_time_not_valid_on_the_new_day_is_said_and_a_valid_one_proposed`, `tests/test_call_intent_flow.py::test_new_day_where_the_time_is_not_valid_says_it_and_proposes_one` |
| Cucina chiusa e giorno detto: niente domanda, "segno per domani <giorno>" | chat.py | caso `domani_a_cucina_chiusa`, `tests/test_next_day_known_info.py::test_replay_of_the_real_call` |
| Cucina chiusa senza giorno: "vuole ordinare per domani?", i dati restano | chat.py | `tests/test_next_day_known_info.py::test_time_and_products_survive_the_question`, `tests/test_next_day_known_info.py::test_name_survives_the_question` |
| Risposta alla domanda con prodotti e orario: si estrae anche il resto | chat.py | `tests/test_next_day_known_info.py::test_answer_with_products_and_time_is_extracted` |
| Preordine fisso (PaP): sempre il prossimo giorno di apertura | chat.py `phone_orders_next_day_only` | `tests/test_next_day_preorder_flow.py::test_call_in_slices_asks_only_what_is_missing_and_recap_matches_saved_order`, `tests/test_chat_logic.py::test_persist_order_sets_pickup_date_tomorrow` |
| Preordine fisso: "per stasera" → "Stasera purtroppo al telefono prendiamo solo ordini per il giorno dopo, ma può passare direttamente in negozio. Va bene per <giorno>?". I prodotti della frase restano; dopo il sì la domanda che manca | chat.py `_today_with_products` | casi `pap_due_tranci_per_stasera`, `reale_pap_zucca_e_pistacchi_per_stasera`, `reale_pap_zucca_e_pistaxi`, `tests/test_next_day_preorder_flow.py::test_request_for_tonight_is_redirected_to_the_shop_and_keeps_the_product`, `tests/test_order_rules_golden.py::test_67b4aaf1_tonight_keeps_the_product_and_yes_goes_on_with_the_missing_question`, `tests/test_call_intent_flow.py::test_tonight_is_still_redirected_with_the_fixed_day` |
| Preordine fisso, altro giorno: "Prendiamo ordini solo per <giorno>, va bene?", ordine e orario restano | chat.py `_propose_only_order_day` | caso `preordine_altro_giorno`, `tests/test_order_rules_golden.py::test_fixed_day_other_day_is_asked_and_yes_keeps_order_and_time`, `tests/test_order_rules_golden.py::test_fixed_day_with_tomorrow_closed_names_the_next_open_day` |
| Preordine fisso, stesso giorno: nessuna domanda | chat.py | caso `preordine_stesso_giorno` |
| Preordine fisso: il saluto e un orario diverso al riepilogo restano come prima. Lo stesso orario ("Per le 7?") → "Sì, alle 19. Confermo l'ordine?" | chat.py | caso `reale_pap_per_le_7_al_riepilogo`, `tests/test_call_intent_flow.py::test_time_at_the_recap_still_repeats_the_recap`, `tests/test_call_intent_flow.py::test_order_is_saved_for_the_fixed_day` |
| Preordine fisso: "no" chiede il giorno e riapplica la regola | chat.py | `tests/test_order_rules_golden.py::test_fixed_day_no_asks_for_the_day_and_reapplies_the_rule` |
| Preordine fisso: vale anche al riepilogo e alla domanda sull'orario | chat.py | `tests/test_order_rules_golden.py::test_fixed_day_other_day_at_the_recap_is_asked_and_not_confirmed`, `tests/test_order_rules_golden.py::test_fixed_day_other_day_at_the_pickup_time_question_is_asked` |
| Prossimo giorno di apertura: salta i giorni chiusi | service `get_next_open_day` | `tests/test_chat_logic.py::test_get_next_open_day_skips_closed_days` |
| "Il ritiro è per <giorno>" una volta per chiamata | chat.py `_day_said_for` | `tests/test_next_day_preorder_flow.py::test_pickup_day_is_said_once_per_call`, `tests/test_next_day_known_info.py::test_bare_yes_with_empty_cart_asks_for_products` |

## Orario di ritiro

| Regola | Fonte | Test |
|---|---|---|
| "Alle 7" → 19:00, "per le 8" → 20:00, "alle 9" → 21:00 | prompt, chat.py | casi `alle_7`, `per_le_8`, `alle_9`, `alle_7_alla_domanda_sull_orario`, `per_le_8_alla_domanda_sull_orario` |
| Orario con i minuti, anche scritto "8.45" dall'STT | prompt, chat.py | casi `orario_con_minuti`, `reale_per_8_45` |
| Orario diverso al riepilogo: vince, si riepiloga di nuovo. Fuori orario si rifiuta e resta il precedente. Lo stesso orario conferma. Preordine fisso escluso | chat.py `_is_new_time_at_recap` | `tests/test_call_intent_flow.py::test_32da8401_tomorrow_survives_an_order_and_the_new_time_wins`, `tests/test_call_intent_flow.py::test_time_at_the_recap_outside_the_hours_is_refused_and_the_old_one_stays`, `tests/test_call_intent_flow.py::test_same_time_at_the_recap_confirms`, `tests/test_call_intent_flow.py::test_a_confirmation_with_no_time_is_not_a_time` |
| "Prima possibile": primo orario di oggi | prompt, service | caso `prima_possibile` |
| "Domani il prima possibile": apertura di quel giorno | chat.py | `tests/test_next_day_known_info.py::test_as_soon_as_possible_tomorrow_is_the_opening_time` |
| Un giorno da solo non è un orario | prompt, chat.py | caso `per_domani_a_cucina_aperta`, `tests/test_next_day_known_info.py::test_only_tomorrow_skips_the_question` |
| Ore 1-11: vale l'ora negli orari del giorno, altrimenti +12 | chat.py `_choose_pickup_hour` | `tests/test_pickup_time_escalation.py::test_evening_opening_alle_otto_is_20`, `tests/test_pickup_time_escalation.py::test_lunch_opening_alle_undici_is_11` |
| Vanno bene mattina e sera: si chiede | chat.py | `tests/test_pickup_time_escalation.py::test_all_day_opening_asks_morning_or_evening` |
| "Di mattina"/"di sera" detto: non si chiede | chat.py | `tests/test_pickup_time_escalation.py::test_explicit_day_part_is_not_asked` |
| Parsing tollerante e storpiature di "otto" | chat.py `_extract_local_pickup_time` | `tests/test_pickup_time_escalation.py::test_spoken_times`, `tests/test_pickup_time_escalation.py::test_stt_distortions_of_otto` |
| Orario prima dell'apertura o troppo vicino: rifiutato col primo disponibile | service `validate_pickup_time` | `tests/test_opening_hours_flow.py::test_time_before_opening_is_refused_with_first_pickup`, `tests/test_opening_hours_flow.py::test_pickup_closer_than_prep_time_is_refused` |
| Orario fuori apertura per quel giorno: rifiutato, il giorno resta | chat.py | `tests/test_next_day_known_info.py::test_time_outside_the_hours_of_that_day_is_refused_but_the_day_stays`, `tests/test_next_day_preorder_flow.py::test_pickup_time_after_closing_is_refused_with_that_days_hours` |
| Orario non capito: frase d'esempio, proposta, poi primo orario con needs_review | chat.py `_pickup_time_miss` | `tests/test_pickup_time_escalation.py::test_hotpoint_gets_the_first_escalation_message`, `tests/test_pickup_time_escalation.py::test_three_failures_save_the_order_at_the_first_slot_with_needs_review` |
| "No" alla proposta di orario: mai l'orario rifiutato | chat.py | `tests/test_pickup_time_escalation.py::test_no_to_the_proposal_then_a_valid_time_is_used` |
| Mai la stessa risposta due volte di fila | chat.py | `tests/test_pickup_time_escalation.py::test_never_the_same_answer_twice_in_a_row` |

## Nome

| Regola | Fonte | Test |
|---|---|---|
| Nome detto da solo o nella frase | prompt | caso `solo_il_nome`, caso `nome_nella_frase` |
| Risposta locale: accetta solo un nome puro | chat.py `_extract_local_customer_name` | `tests/test_chat_logic.py::test_local_customer_name_accepts_plain_name_only`, `tests/test_chat_logic.py::test_local_customer_name_rejects_order_fragments` |
| Parole d'ordine non sono un nome | service | `tests/test_chat_logic.py::test_normalize_extracted_payload_rejects_order_vocab_customer_name` |
| Nome non capito: nuova richiesta, proposta, poi ordine da verificare | chat.py `_name_miss` | `tests/test_pickup_time_escalation.py::test_second_failure_proposes_what_was_heard`, `tests/test_pickup_time_escalation.py::test_three_failures_continue_without_name_and_need_review` |

## Pizza al taglio (voci al kg)

| Regola | Fonte | Test |
|---|---|---|
| A peso: la porzione non si chiede | prompt, chat.py | `tests/test_next_day_preorder_flow.py::test_weight_order_skips_the_portion_question` |
| A tranci: si chiedono quantità, porzione e caldo/freddo | prompt, chat.py | `tests/test_next_day_preorder_flow.py::test_slices_without_portion_ask_portion_and_temperature_together` |
| Più campi in una risposta: si applicano tutti | chat.py | `tests/test_next_day_preorder_flow.py::test_quantity_portion_and_temperature_in_one_answer` |
| Sinonimi di porzione e di temperatura | chat.py | `tests/test_next_day_preorder_flow.py::test_portion_synonyms`, `tests/test_next_day_preorder_flow.py::test_temperature_synonyms` |
| Temperatura e porzione mai predefinite | chat.py | `tests/test_next_day_preorder_flow.py::test_skipped_temperature_is_asked_again_never_defaulted`, `tests/test_next_day_preorder_flow.py::test_unrecognised_portion_three_times_saves_without_default_and_needs_review` |
| Quantità non capita due volte: ordine da verificare | chat.py | `tests/test_next_day_preorder_flow.py::test_unrecognised_quantity_twice_saves_without_default_and_needs_review` |
| Caldo/freddo detto nella stessa parte di frase del prodotto vince sempre, anche su quello dell'LLM o già nel carrello; "fritto" (STT) vale freddo | chat.py `_apply_kg_clause_values` | casi `reale_pap_verde_e_noci_e_mezzo_salmone_fritto`, `reale_pap_salmone_e_limone_fredda` |
| "Mezzo" senza chilo, etti o grammi è mezzo trancio (tranci, porzione mezza), mai mezzo chilo | chat.py `_apply_kg_clause_values` | caso `reale_pap_verde_e_noci_e_mezzo_salmone_fritto` |
| Il prodotto si nomina solo con più voci al taglio | chat.py | `tests/test_next_day_preorder_flow.py::test_product_is_named_only_with_more_than_one_slice_item` |
| Un prodotto già nel carrello ripetuto con la stessa quantità (es. alla domanda intero/mezzo o caldo/freddo) non si somma: valgono solo i dettagli nuovi. Si aggiunge solo con "altri", "aggiungi", "anche", "ancora", "in più" | chat.py `_repeated_cart_item`, `_EXPLICIT_ADD_RE` | caso `pap_ripete_il_prodotto_alla_domanda_intero_mezzo`, caso `pap_ripete_il_prodotto_alla_domanda_caldo_freddo`, caso `pap_altri_due_tranci_si_aggiungono`, `tests/test_order_rules_golden.py::test_repeating_the_product_at_the_portion_question_does_not_add_slices` |

## Riepilogo e conferma

| Regola | Fonte | Test |
|---|---|---|
| Il riepilogo nomina sempre il giorno; "stasera" solo se il ritiro è oggi | chat.py `build_confirmation_recap` | `tests/test_order_rules_golden.py::test_tonight_recap_says_tonight_and_nothing_else`, `tests/test_order_rules_golden.py::test_tomorrow_with_open_kitchen_is_saved_for_tomorrow_and_the_recap_says_so` |
| Oltre la settimana il riepilogo dice anche la data | chat.py `pickup_day_phrase` | `tests/test_order_rules_golden.py::test_far_date_recap_names_the_date` |
| L'ordine salvato ha sempre `pickup_date`, anche per "stasera" | chat.py `_persist_order_once` | `tests/test_chat_logic.py::test_corte_del_sole_same_day_order_has_todays_pickup_date`, `tests/test_order_rules_golden.py::test_tonight_recap_says_tonight_and_nothing_else`, `tests/test_opening_hours_flow.py::test_takeaway_for_tonight_and_recap_says_the_day`, `tests/test_restaurant_pricing.py::test_full_cds_order_by_piece_with_todays_pickup_date_and_nonzero_price` |
| Correzione al riepilogo (gusto, quantità, intero/mezzo, caldo/freddo, ora): il carrello si aggiorna e segue un riepilogo nuovo, mai quello identico; "Sì, però caldi" non conferma. Le parole precedute da "non" non contano ("mezzi, non interi"). Un altro gusto passa dall'LLM e prende quantità, porzione e caldo/freddo di quello che sostituisce. Correzione non capita: "Va bene, cosa cambio?" | chat.py `_apply_recap_item_changes`, `_names_product_outside_cart`, `_recap_correction_before` | caso `pap_riepilogo_mezzi_non_interi`, caso `pap_riepilogo_si_pero_caldi`, caso `pap_riepilogo_facciamone_tre`, caso `pap_riepilogo_alle_8`, caso `pap_riepilogo_cambio_gusto`, caso `pap_riepilogo_correzione_non_chiara`, `tests/test_order_rules_golden.py::test_a_correction_at_the_recap_is_applied_and_never_confirmed`, `tests/test_order_rules_golden.py::test_an_unclear_correction_at_the_recap_asks_what_to_change_then_applies_it` |
| Si conferma solo dopo il riepilogo | chat.py | `tests/test_next_day_preorder_flow.py::test_call_in_slices_asks_only_what_is_missing_and_recap_matches_saved_order` |
| Conferma al cellulare: "Le arriva un SMS di conferma" | chat.py | `tests/test_next_day_known_info.py::test_mobile_is_told_about_the_sms` |
| Conferma al fisso: nessun messaggio promesso | chat.py | `tests/test_next_day_known_info.py::test_landline_is_not_promised_a_message` |
| WhatsApp spento con il numero sandbox | service `whatsapp_sender` | `tests/test_next_day_known_info.py::test_customer_confirmation_never_tries_the_sandbox`, `tests/test_next_day_known_info.py::test_owner_alert_goes_by_sms_with_the_sandbox` |
| Un ordine si salva una sola volta per conversazione | chat.py | `tests/test_chat_logic.py::test_persist_order_once_is_idempotent_per_conversation` |

## NON COPERTE

Regole presenti nel prompt o nel codice che nessun test verifica. L'elenco serve a decidere se aggiungere i test.

| Regola | Fonte | Motivo |
|---|---|---|
| "Senza glutine" usa la voce "(SG)" del menu | prompt | Il menu di CdS non ha voci "(SG)": serve un menu di prova che le abbia |
| Mai proporre una voce "(SG)" come alternativa a un impasto | prompt | Come sopra |
| "Rossa": nessuna aggiunta automatica | prompt | NON COPERTA |
| Temperatura: "da asporto" significa "fredda" per le voci al kg | prompt | NON COPERTA. Da rivedere: nelle frasi di CdS "da asporto" è solo il modo di ritiro |
| Domande sugli impasti e i loro prezzi: risposta dalla lista | prompt | NON COPERTA. `extract_order_from_text` restituisce solo JSON, quindi la regola potrebbe non avere effetto |
| Segnale "ho finito" (`is_done_signal`) | chat.py | La funzione non è chiamata da nessuna parte |
