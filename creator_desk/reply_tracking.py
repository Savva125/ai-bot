"""Reconcile generated replies with positively observed source messages."""
import hashlib
from sqlalchemy import select
from .models import Draft, Feedback


def short_key(key):
    return (key or "")[:32]


def source_receipt(turns, index, fallback_before=""):
    return ":".join(("uia", short_key(turns[index]["key"]),
                     short_key(turns[index - 1]["key"]) if index else short_key(fallback_before),
                     short_key(turns[index + 1]["key"]) if index + 1 < len(turns) else ""))


def reconcile_replies(session, chat, stored, observed, tail_visible):
    """Missing is not deletion unless the surrounding source area is covered twice.

    Receipts fit the existing Draft.receipt field and survive restart. No missing
    text is restored to the source application, and no send is retried here.
    """
    current = {short_key(t["key"]): i for i, t in enumerate(observed)}
    old_keys = {t["key"] for t in stored}
    removed, blocked_anchors, pending = set(), set(), False
    signature = hashlib.sha256("|".join(current).encode()).hexdigest()[:32]
    drafts = list(session.scalars(select(Draft).where(Draft.chat_id == chat.id)
                                  .order_by(Draft.created_at.desc()).limit(100)))
    for draft in drafts:
        # An asynchronous Invoke may be observed on the following poll. Associate
        # only a NEW outgoing message following the previously captured context.
        pending_parts = draft.receipt.split(":") if draft.receipt and draft.receipt.startswith("uia:") else []
        anchored = len(pending_parts) == 4 and not pending_parts[1]
        pending_key = pending_parts[2] if len(pending_parts) == 4 and not pending_parts[1] else short_key(stored[-1]["key"]) if stored else ""
        unlinked = not draft.receipt or (len(pending_parts) == 4 and not pending_parts[1])
        if (unlinked and draft.text and (draft.revision == chat.revision or pending_key in current)
                and draft.status in {"needs_review", "screen_inserted", "screen_sending", "superseded", "cancelled"}):
            matches = [i for i, t in enumerate(observed) if t["role"] == "outgoing"
                       and t["text"].strip() == draft.text.strip() and (anchored or t["key"] not in old_keys)
                       and pending_key in current and i > current[pending_key]]
            if len(matches) == 1:
                draft.receipt = source_receipt(observed, matches[0], stored[-1]["key"] if stored else "")
                draft.status, draft.reason = "sent_observed", "source_message_observed"
        if not draft.receipt or not draft.receipt.startswith("uia:"):
            continue
        parts = draft.receipt.split(":")
        if len(parts) != 4:
            continue
        _, key, before, after = parts
        if key and key in current:
            turn = observed[current[key]]
            if turn["role"] == "outgoing" and turn["text"].strip() == draft.text.strip():
                draft.receipt = source_receipt(observed, current[key], before)
                draft.status, draft.reason = "sent_observed", "source_message_observed"
                for feedback in session.scalars(select(Feedback).where(Feedback.draft_id == draft.id, Feedback.rating == "source_removed")):
                    session.delete(feedback)
            elif turn["role"] == "outgoing":
                draft.status, draft.reason = "edited", "source_message_edited"
            else:
                draft.status, draft.reason = "needs_review", "source_identity_changed"
            continue
        if draft.status in {"discarded", "cancelled"}:
            blocked_anchors.add(before)
            removed.update(t["key"] for t in stored if short_key(t["key"]) == key)
            continue
        covered = before in current and (after in current or tail_visible is True)
        if not key or not covered:
            continue  # Scrolled out of view, unknown viewport, or no source proof.
        marker = "missing_source:" + signature
        if draft.status == "source_missing" and draft.reason == marker:
            draft.status, draft.reason = "discarded", "source_message_removed"
            blocked_anchors.add(before)
            removed.update(t["key"] for t in stored if short_key(t["key"]) == key)
            if not session.scalar(select(Feedback.id).where(Feedback.draft_id == draft.id, Feedback.rating == "source_removed")):
                session.add(Feedback(draft_id=draft.id, rating="source_removed", proposed=draft.original or draft.text,
                                     corrected="", explanation="Ответ исчез из подтверждённой области переписки."))
        else:
            draft.status, draft.reason = "source_missing", marker
            pending = True
    return removed, blocked_anchors, pending
