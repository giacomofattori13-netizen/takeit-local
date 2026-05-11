import hashlib
import re


def describe_text_for_log(text: str | None) -> str:
    value = text or ""
    digest = hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"chars={len(value)} sha256={digest}"


def mask_name(name: str | None) -> str:
    parts = [part for part in re.split(r"\s+", (name or "").strip()) if part]
    if not parts:
        return "unknown"
    return " ".join(f"{part[0]}***" for part in parts[:2])


def mask_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return "unknown"

    visible = digits[-4:]
    hidden_count = max(len(digits) - len(visible), 3)
    return f"{'*' * hidden_count}{visible}"
