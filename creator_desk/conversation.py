"""Merge overlapping observed messages without flattening speakers or inventing history."""
import hashlib
from datetime import datetime, timedelta
import re


def parse_message_time(value, reference=None):
    """Read a timestamp from the messenger's Time field, never message content.

    Time-only labels are interpreted in the desktop's local zone, matching the
    messenger UI. A clock time ahead of now belongs to the preceding day.
    Unknown formats stay unknown instead of guessing from arbitrary text.
    """
    reference = reference or datetime.now().astimezone()
    value = (value or "").strip().casefold().replace("\u200e", "").replace("\u200f", "")
    match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?(?!\w)", value)
    if not match:
        return None
    hour, minute, second = int(match[1]), int(match[2]), int(match[3] or 0)
    if minute > 59 or second > 59 or hour > 23:
        return None
    if match[4]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if match[4] == "pm" else 0)
    prefix = value[:match.start()].strip()
    explicit_day = False
    day = reference.date()
    numeric = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", prefix)
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", prefix)
    try:
        if numeric:
            day = day.replace(year=int(numeric[3]), month=int(numeric[2]), day=int(numeric[1]))
            prefix = prefix[:numeric.start()] + prefix[numeric.end():]
            explicit_day = True
        elif iso:
            day = day.replace(year=int(iso[1]), month=int(iso[2]), day=int(iso[3]))
            prefix = prefix[:iso.start()] + prefix[iso.end():]
            explicit_day = True
        elif re.search(r"\b(?:вчера|yesterday)\b", prefix):
            day -= timedelta(days=1)
            explicit_day = True
        elif re.search(r"\b(?:сегодня|today)\b", prefix):
            explicit_day = True
        prefix = re.sub(r"\b(?:в|at|сегодня|today|вчера|yesterday|отправлено|получено|изменено|sent|received|edited)\b", "", prefix).strip(" ,")
        if prefix:
            return None
        stamp = reference.replace(year=day.year, month=day.month, day=day.day,
                                  hour=hour, minute=minute, second=second, microsecond=0)
    except ValueError:
        return None
    if stamp > reference + timedelta(minutes=1):
        if explicit_day:
            return None
        stamp -= timedelta(days=1)
    return min(stamp.timestamp(), reference.timestamp())


def message_key(turn):
    return turn.get("key") or hashlib.sha256((turn["role"] + ":" + turn["text"]).encode()).hexdigest()


def merge_turns(stored, observed, limit=80):
    if not observed:
        return list(stored)
    current = [{"key": message_key(t), "role": t["role"], "text": t["text"]} for t in observed]
    known = {t["key"] for t in stored}
    if not known.intersection(t["key"] for t in current):
        return current[-limit:]  # No overlap means continuity is not established.
    merged = [dict(t) for t in stored]
    previous = None
    for i, turn in enumerate(current):
        positions = {t["key"]: j for j, t in enumerate(merged)}
        if turn["key"] in positions:
            merged[positions[turn["key"]]] = turn
        else:
            following = next((t["key"] for t in current[i + 1:] if t["key"] in positions), None)
            index = positions[following] if following else positions[previous] + 1 if previous in positions else len(merged)
            merged.insert(index, turn)
        previous = turn["key"]
    # Keep context only through the latest observed message. Scrolling backwards
    # must not make messages below the viewport look like newly received replies.
    end = next(i for i, t in enumerate(merged) if t["key"] == current[-1]["key"])
    return merged[:end + 1][-limit:]


def incoming_anchor(turns):
    last = next((t for t in reversed(turns) if t["role"] == "incoming"), None)
    return hashlib.sha256((last["key"] + ":" + last["text"]).encode()).hexdigest() if last else "no-incoming"
