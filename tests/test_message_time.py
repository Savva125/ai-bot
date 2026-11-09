from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS

import pytest
from creator_desk.conversation import parse_message_time
from creator_desk.screen_agent import message_metadata

MOSCOW = timezone(timedelta(hours=3))


@pytest.mark.parametrize("label,expected", [
    ("в 23:04", datetime(2026, 11, 4, 23, 4, tzinfo=MOSCOW)),
    ("04.11.2026 в 23:04", datetime(2026, 11, 4, 23, 4, tzinfo=MOSCOW)),
    ("2026-11-04 at 23:04", datetime(2026, 11, 4, 23, 4, tzinfo=MOSCOW)),
    ("вчера в 23:04", datetime(2026, 11, 4, 23, 4, tzinfo=MOSCOW)),
    ("today at 12:03 AM", datetime(2026, 11, 5, 0, 3, tzinfo=MOSCOW)),
])
def test_message_time_handles_dates_and_midnight(label, expected):
    now = datetime(2026, 11, 5, 0, 10, tzinfo=MOSCOW)
    assert parse_message_time(label, now) == expected.timestamp()


@pytest.mark.parametrize("label", ["", "в 25:99", "31.02.2026 в 12:00", "в понедельник 12:00", "06.11.2026 в 12:00"])
def test_invalid_or_ambiguous_time_remains_unknown(label):
    assert parse_message_time(label, datetime(2026, 11, 5, 0, 10, tzinfo=MOSCOW)) is None


def test_timing_is_taken_from_native_time_field_not_message_body():
    def field(name, value):
        return NS(element_info=NS(name=name), iface_value=NS(CurrentValue=value))
    node = NS(children=lambda: [field("Сообщение", "Напиши в 12:00"), field("Время", "в 23:04")])
    metadata = message_metadata(node)
    assert metadata["message_time"] == "в 23:04"
    assert metadata["message_text"] == "Напиши в 12:00"
