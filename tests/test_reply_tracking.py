"""Source observations, removal coverage and scroll ambiguity; no UI input."""
from sqlalchemy import select
import pytest

from creator_desk.db import connect
from creator_desk.models import Base, Account, Contact, Chat, Draft, Feedback
from creator_desk.reply_tracking import reconcile_replies, source_receipt, short_key


def turn(key, text, role="outgoing"):
    return {"key": key, "role": role, "text": text}


@pytest.fixture
def store(tmp_path):
    engine, sessions = connect("sqlite:///" + str(tmp_path / "tracking.db"))
    Base.metadata.create_all(engine)
    with sessions.begin() as s:
        a = Account(external_id="a", name="a"); s.add(a); s.flush()
        c = Contact(account_id=a.id, external_id="c", name="c"); s.add(c); s.flush()
        chat = Chat(account_id=a.id, contact_id=c.id, external_id="chat", revision="trigger")
        s.add(chat); s.flush()
        draft = Draft(chat_id=chat.id, revision="trigger", config_hash="test", text="Bad reply",
                      original="Bad reply", provider="test", status="sent_observed")
        stored = [turn("before", "Question", "incoming"), turn("reply", "Bad reply")]
        draft.receipt = source_receipt(stored, 1)
        s.add(draft); s.flush(); ids = chat.id, draft.id
    yield sessions, ids, stored
    engine.dispose()


def test_tail_removal_is_confirmed_twice_and_receipt_survives_reopening(store):
    sessions, (chat_id, draft_id), stored = store
    visible = stored[:1]
    with sessions.begin() as s:
        removed, anchors, pending = reconcile_replies(s, s.get(Chat, chat_id), stored, visible, True)
        assert pending and not removed and not anchors
    with sessions.begin() as s:
        removed, anchors, pending = reconcile_replies(s, s.get(Chat, chat_id), visible, visible, True)
        assert not pending and short_key("before") in anchors
        assert s.get(Draft, draft_id).status == "discarded"
        assert s.scalar(select(Feedback)).rating == "source_removed"


def test_scrolling_does_not_mean_deleted(store):
    sessions, (chat_id, draft_id), stored = store
    with sessions.begin() as s:
        for _ in range(3):
            removed, anchors, pending = reconcile_replies(s, s.get(Chat, chat_id), stored, stored[:1], False)
            assert not removed and not anchors and not pending
        assert s.get(Draft, draft_id).status == "sent_observed"


def test_interior_removal_requires_both_surrounding_messages(store):
    sessions, (chat_id, draft_id), stored = store
    after = turn("after", "New question", "incoming")
    stored = stored + [after]
    with sessions.begin() as s:
        s.get(Draft, draft_id).receipt = source_receipt(stored, 1)
    visible = [stored[0], after]
    with sessions.begin() as s:
        assert reconcile_replies(s, s.get(Chat, chat_id), stored, visible, False)[2]
    with sessions.begin() as s:
        removed, _, _ = reconcile_replies(s, s.get(Chat, chat_id), stored, visible, False)
        assert "reply" in removed
        assert s.get(Draft, draft_id).status == "discarded"


def test_late_source_appearance_confirms_uncertain_invoke(store):
    sessions, (chat_id, draft_id), stored = store
    with sessions.begin() as s:
        d = s.get(Draft, draft_id)
        d.status, d.reason, d.receipt = "needs_review", "verify_delivery_in_source", "uia::before:"
        reconcile_replies(s, s.get(Chat, chat_id), stored[:1], stored, True)
        assert d.status == "sent_observed" and d.receipt.startswith("uia:reply:")


def test_edited_message_is_not_claimed_to_equal_original_reply(store):
    sessions, (chat_id, draft_id), stored = store
    edited = [stored[0], turn("reply", "Operator's improved response")]
    with sessions.begin() as s:
        reconcile_replies(s, s.get(Chat, chat_id), stored, edited, True)
        assert s.get(Draft, draft_id).status == "edited"


def test_old_same_text_before_trigger_is_not_confirmation(store):
    sessions, (chat_id, draft_id), stored = store
    with sessions.begin() as s:
        d = s.get(Draft, draft_id)
        d.status, d.receipt = "needs_review", "uia::before:"
        reconcile_replies(s, s.get(Chat, chat_id), stored[:1], [stored[1], stored[0]], True)
        assert d.status == "needs_review"
