import os


def dev_force_to_self_enabled() -> bool:
    raw = os.getenv("DEV_FORCE_TO_SELF", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}
