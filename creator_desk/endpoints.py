from urllib.parse import urlsplit
from .errors import AiError


def validate_server_url(value):
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError()
    except ValueError:
        raise AiError("Некорректный адрес или порт сервера.") from None
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise AiError("Укажите адрес сервера: https://адрес без пути и пароля.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise AiError("Для удалённого сервера требуется HTTPS.")
    return value
