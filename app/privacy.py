import re


def mask_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return "unknown"

    visible = digits[-4:]
    hidden_count = max(len(digits) - len(visible), 3)
    return f"{'*' * hidden_count}{visible}"
