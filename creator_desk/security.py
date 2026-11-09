import re

import keyring

SERVICE = "CreatorDesk"


def save_secret(name: str, value: str):
    # The Windows credential backend is mandatory; do not fall back to files.
    backend = keyring.get_keyring()
    module = type(backend).__module__
    if "Windows" not in module and "macOS" not in module and "SecretService" not in module:
        raise RuntimeError("Secure OS credential backend unavailable")
    keyring.set_password(SERVICE, name, value)


def read_secret(name: str) -> str | None:
    return keyring.get_password(SERVICE, name)


AGE_PATTERNS = [
    r"\b(?:i am|i'm|im|я|мне)\s*(?:only\s*)?(?:[1-9]|1[0-7])\s*(?:years?\s*old|лет|года?|yo|y/o)?\b",
    r"\b(?:under\s*18|minor|несовершеннолет\w*|школьни[кц]\w*)\b",
    r"\b(?:возраст|age)\s*[:=]\s*(?:[1-9]|1[0-7])\b",
]


def age_concern(text: str) -> bool:
    return any(re.search(p, text, flags=re.I) for p in AGE_PATTERNS)
