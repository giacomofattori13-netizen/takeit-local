"""Centralized Base44 REST API client.

All requests use BASE44_API_KEY passed as an X-Api-Key header (not a query param).
App ID: 69c54bc5c44250d7da397903
"""
import os
import re

import httpx

_APP_ID = "69c54bc5c44250d7da397903"
_BASE = f"https://app.base44.com/api/apps/{_APP_ID}/entities"


def _auth_headers() -> dict:
    api_key = os.getenv("BASE44_API_KEY", "")
    h: dict = {"Content-Type": "application/json"}
    if api_key:
        h["X-Api-Key"] = api_key
    return h


def _parse_entities(data) -> list[dict]:
    entities = data.get("entities", data) if isinstance(data, dict) else data
    return entities if isinstance(entities, list) else []


def get_menu_items(restaurant_id: str | None = None, timeout: float = 10.0) -> list[dict]:
    """Fetch MenuItem entities from Base44, optionally filtered by restaurant_id."""
    if not os.getenv("BASE44_API_KEY"):
        print("[Base44] get_menu_items: BASE44_API_KEY mancante")
        return []
    try:
        resp = httpx.get(f"{_BASE}/MenuItem", headers=_auth_headers(), timeout=timeout)
        resp.raise_for_status()
        items = _parse_entities(resp.json())
        if restaurant_id:
            items = [item for item in items if item.get("restaurant_id") == restaurant_id]
            print(f"[Base44] get_menu_items: {len(items)} voci (restaurant_id={restaurant_id!r})")
        else:
            print(f"[Base44] get_menu_items: {len(items)} voci")
        return items
    except Exception as e:
        print(f"[Base44] get_menu_items error: {type(e).__name__}: {e}")
        return []


def get_all_restaurants(timeout: float = 10.0) -> list[dict]:
    """Fetch all Restaurant entities from Base44."""
    if not os.getenv("BASE44_API_KEY"):
        print("[Base44] get_all_restaurants: BASE44_API_KEY mancante")
        return []
    try:
        resp = httpx.get(f"{_BASE}/Restaurant", headers=_auth_headers(), timeout=timeout)
        resp.raise_for_status()
        restaurants = _parse_entities(resp.json())
        print(f"[Base44] get_all_restaurants: {len(restaurants)} ristoranti")
        return restaurants
    except Exception as e:
        print(f"[Base44] get_all_restaurants error: {type(e).__name__}: {e}")
        return []


def _normalize_e164(s: str) -> str:
    """Normalize a phone string to a comparable E.164-like form.

    Keeps only digits and a single leading '+'; converts a leading '00'
    international prefix to '+' so that 0016067334996 == +16067334996.
    """
    cleaned = re.sub(r"[^\d+]", "", s or "")
    if cleaned.startswith("+"):
        # Keep the leading '+', strip any spurious '+' elsewhere
        cleaned = "+" + cleaned[1:].replace("+", "")
    else:
        cleaned = cleaned.replace("+", "")
        if cleaned.startswith("00"):
            cleaned = "+" + cleaned[2:]
    return cleaned


def get_restaurant_by_phone(phone: str, timeout: float = 10.0) -> dict | None:
    """Find a Restaurant whose agent_phone matches the given phone number.

    Normalizes both sides to E.164 (digits + optional leading '+') and tries:
    1. Exact match on the normalized string.
    2. 10-digit suffix match to handle country-code prefix differences.
    Logs every candidate so mismatches are visible in Railway logs.
    """
    if not os.getenv("BASE44_API_KEY"):
        print("[Base44] get_restaurant_by_phone: BASE44_API_KEY mancante")
        return None
    try:
        restaurants = get_all_restaurants(timeout=timeout)
        needle = _normalize_e164(phone)
        needle_digits = re.sub(r"\D", "", needle)
        needle_suffix = needle_digits[-10:] if len(needle_digits) >= 10 else needle_digits
        print(
            f"[Base44] Phone lookup: To={phone!r} → norm={needle!r} suffix10={needle_suffix!r}"
            f", candidati={len(restaurants)}"
        )
        for r in restaurants:
            raw = r.get("agent_phone") or ""
            r_norm = _normalize_e164(raw)
            r_digits = re.sub(r"\D", "", r_norm)
            r_suffix = r_digits[-10:] if len(r_digits) >= 10 else r_digits
            exact = needle == r_norm
            sfx = bool(needle_suffix and r_suffix and needle_suffix == r_suffix)
            print(
                f"[Base44]   id={r.get('id')!r} agent_phone={raw!r}"
                f" norm={r_norm!r} exact={exact} suffix10={sfx}"
            )
            if exact or sfx:
                print(f"[Base44] Match trovato id={r.get('id')!r} per To={phone!r}")
                return r
        print(f"[Base44] Nessun match per To={phone!r}")
        return None
    except Exception as e:
        print(f"[Base44] get_restaurant_by_phone error: {type(e).__name__}: {e}")
        return None


def get_restaurant_by_id(restaurant_id: str, timeout: float = 10.0) -> dict | None:
    """Fetch a specific Restaurant entity by ID from Base44."""
    if not os.getenv("BASE44_API_KEY"):
        print("[Base44] get_restaurant_by_id: BASE44_API_KEY mancante")
        return None
    try:
        resp = httpx.get(
            f"{_BASE}/Restaurant/{restaurant_id}",
            headers=_auth_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # Base44 may return the entity directly or wrapped
        if isinstance(data, dict) and "entities" in data:
            entities = data["entities"]
            restaurant = entities[0] if isinstance(entities, list) and entities else None
        elif isinstance(data, dict):
            restaurant = data
        else:
            restaurant = None
        if restaurant:
            print(f"[Base44] get_restaurant_by_id ok id={restaurant.get('id')!r}")
        return restaurant
    except Exception as e:
        print(f"[Base44] get_restaurant_by_id id={restaurant_id!r} error: {type(e).__name__}: {e}")
        return None


def get_restaurant(timeout: float = 10.0) -> dict | None:
    """Fetch the first Restaurant entity from Base44."""
    if not os.getenv("BASE44_API_KEY"):
        print("[Base44] get_restaurant: BASE44_API_KEY mancante")
        return None
    try:
        resp = httpx.get(f"{_BASE}/Restaurant", headers=_auth_headers(), timeout=timeout)
        resp.raise_for_status()
        entities = _parse_entities(resp.json())
        restaurant = entities[0] if entities else None
        if restaurant:
            print(f"[Base44] get_restaurant ok id={restaurant.get('id')}")
        return restaurant
    except Exception as e:
        print(f"[Base44] get_restaurant error: {type(e).__name__}: {e}")
        return None


def update_restaurant(patch: dict, restaurant_id: str | None = None, timeout: float = 10.0) -> dict | None:
    """PUT (full update) the Restaurant entity on Base44."""
    if not os.getenv("BASE44_API_KEY"):
        print("[Base44] update_restaurant: BASE44_API_KEY mancante")
        return None
    if not restaurant_id:
        current = get_restaurant(timeout=timeout)
        if not current:
            print("[Base44] update_restaurant: ristorante non trovato su Base44")
            return None
        restaurant_id = current.get("id")
    if not restaurant_id:
        print("[Base44] update_restaurant: id mancante")
        return None
    try:
        resp = httpx.put(
            f"{_BASE}/Restaurant/{restaurant_id}",
            json=patch,
            headers=_auth_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        print(f"[Base44] update_restaurant id={restaurant_id} ok")
        return resp.json()
    except Exception as e:
        print(f"[Base44] update_restaurant id={restaurant_id} error: {type(e).__name__}: {e}")
        return None


def update_menu_item(item_id: str, patch: dict, timeout: float = 10.0) -> dict | None:
    """PUT a MenuItem entity on Base44."""
    if not os.getenv("BASE44_API_KEY"):
        print("[Base44] update_menu_item: BASE44_API_KEY mancante")
        return None
    try:
        resp = httpx.put(
            f"{_BASE}/MenuItem/{item_id}",
            json=patch,
            headers=_auth_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        print(f"[Base44] update_menu_item id={item_id} ok")
        return resp.json()
    except Exception as e:
        print(f"[Base44] update_menu_item id={item_id} error: {type(e).__name__}: {e}")
        return None


def create_call_log(data: dict, timeout: float = 8.0) -> dict | None:
    """Create a CallLog entity on Base44."""
    if not os.getenv("BASE44_API_KEY"):
        return None
    try:
        resp = httpx.post(
            f"{_BASE}/CallLog",
            json=data,
            headers=_auth_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        result = resp.json()
        print(f"[Base44] create_call_log ok id={result.get('id')!r}")
        return result
    except Exception as e:
        print(f"[Base44] create_call_log error: {type(e).__name__}: {e}")
        return None


def update_call_log(log_id: str, patch: dict, timeout: float = 8.0) -> dict | None:
    """Update a CallLog entity on Base44."""
    if not os.getenv("BASE44_API_KEY"):
        return None
    try:
        resp = httpx.put(
            f"{_BASE}/CallLog/{log_id}",
            json=patch,
            headers=_auth_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        print(f"[Base44] update_call_log id={log_id!r} outcome={patch.get('outcome')!r}")
        return resp.json()
    except Exception as e:
        print(f"[Base44] update_call_log id={log_id!r} error: {type(e).__name__}: {e}")
        return None


def create_owner_command(data: dict, timeout: float = 10.0) -> dict | None:
    """Create an OwnerCommand entity on Base44."""
    if not os.getenv("BASE44_API_KEY"):
        print("[Base44] create_owner_command: BASE44_API_KEY mancante")
        return None
    try:
        resp = httpx.post(
            f"{_BASE}/OwnerCommand",
            json=data,
            headers=_auth_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        result = resp.json()
        print(f"[Base44] create_owner_command ok id={result.get('id')}")
        return result
    except Exception as e:
        print(f"[Base44] create_owner_command error: {type(e).__name__}: {e}")
        return None


def update_owner_command(command_id: str, patch: dict, timeout: float = 10.0) -> dict | None:
    """Update an OwnerCommand entity on Base44."""
    if not os.getenv("BASE44_API_KEY"):
        print("[Base44] update_owner_command: BASE44_API_KEY mancante")
        return None
    try:
        resp = httpx.put(
            f"{_BASE}/OwnerCommand/{command_id}",
            json=patch,
            headers=_auth_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        print(f"[Base44] update_owner_command id={command_id} ok")
        return resp.json()
    except Exception as e:
        print(f"[Base44] update_owner_command id={command_id} error: {type(e).__name__}: {e}")
        return None
