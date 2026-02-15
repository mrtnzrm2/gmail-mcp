LAST_ERROR: dict | None = None


def set_last_error(exc: Exception, where: str) -> None:
    global LAST_ERROR
    LAST_ERROR = {
        "where": where,
        "type": exc.__class__.__name__,
        "message": str(exc),
    }


def get_last_error() -> dict | None:
    return LAST_ERROR
