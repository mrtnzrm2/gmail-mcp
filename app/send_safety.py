from .config import DEV_FORCE_TO_SELF_ALLOWLIST, DEV_FORCE_TO_SELF_REASON
from .safety import dev_force_to_self_enabled


def parse_allowlist(raw: str) -> set[str]:
    text = (raw or "").strip()
    if not text:
        return set()
    return {item.strip().lower() for item in text.split(",") if item.strip()}


def apply_force_to_self(to_email: str, me_email: str) -> tuple[str, bool, str | None]:
    original = (to_email or "").strip().lower()
    me = (me_email or "").strip().lower()
    if not dev_force_to_self_enabled():
        return original, False, None
    if not me:
        return original, False, None

    allowlist = parse_allowlist(DEV_FORCE_TO_SELF_ALLOWLIST)
    if allowlist:
        final_to = original if original in allowlist else me
    else:
        final_to = me
    applied = final_to != original
    reason = DEV_FORCE_TO_SELF_REASON if applied else None
    return final_to, applied, reason
