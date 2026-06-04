"""Centralized Base44 REST API client.

Uses BASE44_API_KEY (env) for write operations and BASE44_TOKEN for reads.
App ID: 69c54bc5c44250d7da397903
"""
import os

import httpx

_APP_ID = "69c54bc5c44250d7da397903"
_BASE = f"https://app.base44.com/api/apps/{_APP_ID}/entities"


def _rw_params() -> dict:
    return {"api_key": os.getenv("BASE44_API_KEY", "")}


def _ro_headers() -> dict:
    token = os.getenv("BASE44_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _parse_entities(data) -> list[dict]:
    entities = data.get("entities", data) if isinstance(data, dict) else data
    return entities if isinstance(entities, list) else []


def get_menu_items(timeout: float = 10.0) -> list[dict]:
    """Fetch all MenuItem entities from Base44."""
    if not os.getenv("BASE44_TOKEN"):
        print("[Base44] get_menu_items: BASE44_TOKEN mancante")
        return []
    try:
        resp = httpx.get(f"{_BASE}/MenuItem", headers=_ro_headers(), timeout=timeout)
        resp.raise_for_status()
        items = _parse_entities(resp.json())
        print(f"[Base44] get_menu_items: {len(items)} voci")
        return items
    except Exception as e:
        print(f"[Base44] get_menu_items error: {type(e).__name__}: {e}")
        return []


def get_restaurant(timeout: float = 10.0) -> dict | None:
    """Fetch the first Restaurant entity from Base44."""
    if not os.getenv("BASE44_TOKEN"):
        print("[Base44] get_restaurant: BASE44_TOKEN mancante")
        return None
    try:
        resp = httpx.get(f"{_BASE}/Restaurant", headers=_ro_headers(), timeout=timeout)
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
    api_key = os.getenv("BASE44_API_KEY")
    if not api_key:
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
            params=_rw_params(),
            json=patch,
            headers={"Content-Type": "application/json"},
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
    api_key = os.getenv("BASE44_API_KEY")
    if not api_key:
        print("[Base44] update_menu_item: BASE44_API_KEY mancante")
        return None
    try:
        resp = httpx.put(
            f"{_BASE}/MenuItem/{item_id}",
            params=_rw_params(),
            json=patch,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        print(f"[Base44] update_menu_item id={item_id} ok")
        return resp.json()
    except Exception as e:
        print(f"[Base44] update_menu_item id={item_id} error: {type(e).__name__}: {e}")
        return None


def create_owner_command(data: dict, timeout: float = 10.0) -> dict | None:
    """Create an OwnerCommand entity on Base44."""
    api_key = os.getenv("BASE44_API_KEY")
    if not api_key:
        print("[Base44] create_owner_command: BASE44_API_KEY mancante")
        return None
    try:
        resp = httpx.post(
            f"{_BASE}/OwnerCommand",
            params=_rw_params(),
            json=data,
            headers={"Content-Type": "application/json"},
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
    api_key = os.getenv("BASE44_API_KEY")
    if not api_key:
        print("[Base44] update_owner_command: BASE44_API_KEY mancante")
        return None
    try:
        resp = httpx.put(
            f"{_BASE}/OwnerCommand/{command_id}",
            params=_rw_params(),
            json=patch,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        print(f"[Base44] update_owner_command id={command_id} ok")
        return resp.json()
    except Exception as e:
        print(f"[Base44] update_owner_command id={command_id} error: {type(e).__name__}: {e}")
        return None
