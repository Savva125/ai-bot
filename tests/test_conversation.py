from creator_desk.conversation import merge_turns, incoming_anchor


def turn(key, role="incoming", text=None):
    return {"key": key, "role": role, "text": text or key}


def test_overlapping_viewport_retains_earlier_speakers_and_new_messages():
    stored = [turn("a"), turn("b", "outgoing"), turn("c")]
    observed = [turn("b", "outgoing"), turn("c"), turn("d"), turn("e", "outgoing")]
    result = merge_turns(stored, observed)
    assert [t["key"] for t in result] == list("abcde")
    assert [t["role"] for t in result] == ["incoming", "outgoing", "incoming", "incoming", "outgoing"]


def test_repeated_capture_has_no_duplicate_turns_and_edits_replace_text():
    stored = [turn("a"), turn("b")]
    assert merge_turns(stored, stored) == stored
    result = merge_turns(stored, [turn("a"), turn("b", text="Исправленная реплика")])
    assert len(result) == 2 and result[-1]["text"] == "Исправленная реплика"


def test_older_scroll_does_not_make_cached_future_messages_current():
    result = merge_turns([turn("a"), turn("b"), turn("c")], [turn("a"), turn("b")])
    assert result[-1]["key"] == "b"


def test_no_overlap_does_not_invent_continuity():
    assert merge_turns([turn("old")], [turn("new")]) == [turn("new")]


def test_same_text_new_message_identity_reopens_incoming_turn():
    assert incoming_anchor([turn("a", text="Привет")]) != incoming_anchor([turn("b", text="Привет")])
    assert incoming_anchor([turn("a"), turn("b", "outgoing")]) == incoming_anchor([turn("a")])


def test_context_is_bounded():
    turns = [turn(str(i)) for i in range(100)]
    assert len(merge_turns([], turns)) == 80
