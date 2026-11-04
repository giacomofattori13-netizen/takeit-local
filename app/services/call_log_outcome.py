"""Testi dell'esito sul CallLog, gli stessi per la chiamata in corso e per il ricalcolo."""


def reservation_summary(party_size, date, time, *, pending: bool = False, reference: str | None = None) -> str:
    """'Prenotazione per 4 persone, 2026-11-07 alle 20:00 (rif. abc)'. Il CallLog non ha un
    campo per la prenotazione: il riferimento sta nel riassunto."""
    if isinstance(party_size, float) and party_size.is_integer():
        party_size = int(party_size)  # Base44 restituisce i numeri come 4.0
    when = f"{date or ''} alle {time or ''}".strip()
    summary = f"Prenotazione per {party_size} persone, {when}"
    if pending:
        summary += " (da confermare: richiamare il cliente)"
    if reference:
        summary += f" (rif. {reference})"
    return summary


def order_summary(order_number) -> str:
    if isinstance(order_number, float) and order_number.is_integer():
        order_number = int(order_number)
    if order_number:
        return f"Ordine #{order_number} confermato"
    return "Ordine confermato (numero in assegnazione: Base44 non disponibile)"
