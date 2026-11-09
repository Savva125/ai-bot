"""Persist operator guidance and examples; this does not change model weights."""
import json
from pathlib import Path
import re

from sqlalchemy import select
from .models import AssistantSetting, ReplyExample, Chat, Contact, Draft, Message, Feedback

DEFAULT_STYLE = (
    "Пиши тепло, естественно и разговорно на языке собеседника, подстраиваясь под его тон. "
    "Подхватывай конкретные детали сообщения. Без канцелярита и шаблонных реакций. "
    "Не начинай каждый ответ с приветствия и не заканчивай каждый ответ вопросом. "
    "Эмодзи используй умеренно, если они уместны. Обычно достаточно 1–3 предложений. "
    "Не придумывай личные факты, события или обещания."
)


class PersonalizationError(ValueError):
    pass


def checked(value, maximum, label, required=False):
    value = (value or "").strip()
    if (required and not value) or len(value) > maximum:
        raise PersonalizationError(f"{label}: нужно {'1' if required else '0'}–{maximum} символов.")
    return value


class Personalization:
    def __init__(self, sessions, export_directory=None):
        self.sessions = sessions
        self.export_directory = export_directory or Path(__file__).resolve().parent.parent / "training-data"

    def load(self, chat_id=None):
        with self.sessions() as s:
            setting = s.get(AssistantSetting, "reply_style")
            result = {"style": setting.value if setting else DEFAULT_STYLE, "chat_id": chat_id,
                      "contact": "", "memory": "", "examples": [], "latest_draft": None}
            chat = s.get(Chat, chat_id) if chat_id else None
            if not chat:
                result["chat_id"] = None
                return result
            contact = s.get(Contact, chat.contact_id)
            result.update(contact=contact.name, memory=contact.memory)
            result["examples"] = [{"id": e.id, "context": e.context, "reply": e.reply,
                "proposed": e.proposed, "note": e.note} for e in s.scalars(select(ReplyExample)
                    .where(ReplyExample.account_id == chat.account_id).order_by(ReplyExample.created_at.desc()))]
            draft = s.scalar(select(Draft).where(Draft.chat_id == chat.id, Draft.text != "")
                             .order_by(Draft.created_at.desc()).limit(1))
            if draft:
                capture = s.scalar(select(Message).where(Message.chat_id == chat.id,
                                                        Message.external_id == "capture:" + draft.revision))
                result["latest_draft"] = {"id": draft.id, "reply": draft.original or draft.text,
                                           "context": capture.text[-2000:] if capture else ""}
            return result

    def save(self, data):
        style = checked(data.get("style"), 2000, "Стиль", True)
        example = data.get("example")
        if example:
            example = dict(example)
            example["context"] = checked(example.get("context"), 2000, "Контекст примера", True)
            example["reply"] = checked(example.get("reply"), 2000, "Хороший ответ", True)
            example["note"] = checked(example.get("note"), 500, "Пояснение")
        memory = checked(data.get("memory"), 2000, "Память")
        with self.sessions.begin() as s:
            chat = s.get(Chat, data.get("chat_id")) if data.get("chat_id") else None
            if not chat and (example or memory or data.get("delete_ids")):
                raise PersonalizationError("Сначала откройте и распознайте диалог в выбранном окне.")
            setting = s.get(AssistantSetting, "reply_style")
            if setting:
                setting.value = style
            else:
                s.add(AssistantSetting(key="reply_style", value=style))
            if chat:
                s.get(Contact, chat.contact_id).memory = memory
                for identity in data.get("delete_ids", []):
                    row = s.get(ReplyExample, identity)
                    if not row or row.account_id != chat.account_id:
                        raise PersonalizationError("Пример не относится к этому аккаунту.")
                    s.delete(row)
            if example:
                row = s.get(ReplyExample, example["id"]) if example.get("id") else None
                if example.get("id") and (not row or row.account_id != chat.account_id):
                    raise PersonalizationError("Пример не относится к этому аккаунту.")
                draft = s.get(Draft, example["draft_id"]) if example.get("draft_id") else None
                if example.get("draft_id") and (not draft or draft.chat_id != chat.id):
                    raise PersonalizationError("Ответ относится к другому диалогу.")
                if row is None and draft:
                    row = s.scalar(select(ReplyExample).where(ReplyExample.account_id == chat.account_id,
                                                              ReplyExample.draft_id == draft.id))
                if row is None:
                    row = ReplyExample(account_id=chat.account_id, draft_id=draft.id if draft else None,
                                       proposed=(draft.original or draft.text) if draft else "")
                    s.add(row)
                    if draft:
                        s.add(Feedback(draft_id=draft.id, rating="corrected", proposed=row.proposed,
                                       corrected=example["reply"], explanation=example["note"]))
                row.context, row.reply, row.note = example["context"], example["reply"], example["note"]
        return "Стиль, примеры и память сохранены."

    def generation_context(self, chat_id, current_text):
        with self.sessions() as s:
            chat = s.get(Chat, chat_id)
            contact = s.get(Contact, chat.contact_id)
            setting = s.get(AssistantSetting, "reply_style")
            candidates = list(s.scalars(select(ReplyExample).where(ReplyExample.account_id == chat.account_id)
                                       .order_by(ReplyExample.created_at.desc()).limit(100)))
            words = set(re.findall(r"\w{3,}", current_text.casefold()))
            candidates.sort(key=lambda e: len(words & set(re.findall(r"\w{3,}", e.context.casefold()))), reverse=True)
            rejected = list(s.scalars(select(Feedback.proposed).join(Draft, Feedback.draft_id == Draft.id)
                .where(Draft.chat_id == chat_id, Feedback.rating.in_(["rejected", "source_removed"]))
                .order_by(Feedback.created_at.desc()).limit(2)))
            return {"style": setting.value if setting else DEFAULT_STYLE, "memory": contact.memory,
                    "rejected_replies": [text[:500] for text in rejected],
                    "examples": [{"context": e.context[-400:], "reply": e.reply[:500], "note": e.note[:160]}
                                 for e in candidates[:3]]}

    def export(self, chat_id):
        state = self.load(chat_id)
        if not state["chat_id"]:
            raise PersonalizationError("Для экспорта сначала выберите диалог.")
        with self.sessions() as s:
            account_id = s.get(Chat, chat_id).account_id
        folder = Path(self.export_directory)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"style-examples-{account_id}.jsonl"
        lines = [json.dumps({"messages": [{"role": "user", "content": e["context"]},
                    {"role": "assistant", "content": e["reply"]}]}, ensure_ascii=False) for e in state["examples"]]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return {"path": str(path), "count": len(lines)}
