import json
import httpx

from creator_desk.providers import SYSTEM
from .schemas import Draft
from .schemas import InspectedChat
from creator_desk.inspection import INSPECT_SYSTEM


class ModelGateway:
    def __init__(self, settings):
        self.settings = settings

    async def inspect_screen(self, screen):
        rows = json.loads(screen)["elements"]
        ids = [row[0] for row in rows]
        schema = InspectedChat.model_json_schema()
        for key in ("account", "contact", "composer", "send"):
            schema["properties"][key] = {"enum": [None, *ids]}
        schema["properties"]["history"]["items"] = {"enum": [row[0] for row in rows if row[1] in {"Text", "Message", "Hyperlink", "ListItem"}] or [None]}
        headers = {"Authorization": "Bearer " + self.settings.model_api_key} if self.settings.model_api_key else {}
        async with httpx.AsyncClient(timeout=self.settings.model_timeout, trust_env=False) as client:
            response = await client.post(self.settings.model_url + "/chat/completions", headers=headers, json={
                "model": self.settings.model_name, "temperature": 0, "max_tokens": 300,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_schema", "json_schema": {"name": "visible_chat", "strict": True, "schema": schema}},
                "messages": [{"role": "system", "content": INSPECT_SYSTEM},
                             {"role": "user", "content": '{"elements":[[10,"Text","Анна",false],[11,"Text","Анна: Привет! Как дела?",false],[12,"Edit","Сообщение…",true],[13,"Button","Отправить",false]]}'},
                             {"role": "assistant", "content": '{"is_chat":true,"account":null,"contact":10,"history":[11],"incoming":true,"composer":12,"send":13}'},
                             {"role": "user", "content": '{"elements":[[20,"Message","Просмотрено Меня Привет! Отправлено в 10:33",false],[21,"Edit","Сообщение...",true],[22,"Text","Анна",false]]}'},
                             {"role": "assistant", "content": '{"is_chat":true,"account":null,"contact":22,"history":[20],"incoming":false,"composer":21,"send":null}'},
                             {"role": "user", "content": screen}],
            })
            response.raise_for_status()
            if len(response.content) > 100000:
                raise ValueError("model_response_too_large")
            result = InspectedChat.model_validate_json(response.json()["choices"][0]["message"]["content"])
            if result.is_chat:
                refs = [result.account, result.contact, result.composer, result.send, *result.history]
                if any(v is not None and v not in ids for v in refs):
                    raise ValueError("unknown_element_id")
            return result.model_dump()

    async def generate(self, context):
        context = dict(context)
        context["history"] = [{"role": m["role"], "text": m["text"][:900]}
                              for m in context["history"][-16:]]
        context["style_examples"] = [{"context": e.get("context", "")[-400:],
            "reply": e["reply"][:500], "note": e.get("note", "")[:160]}
            for e in context.get("style_examples", [])[:3]]
        context["rejected_replies"] = [v[:500] for v in context.get("rejected_replies", [])[:2]]
        context["knowledge"] = context.get("knowledge", "")[:3000]
        context["contact_memory"] = context.get("contact_memory", "")[:2000]
        while len(json.dumps(context, ensure_ascii=False)) > 12000 and len(context["history"]) > 1:
            context["history"].pop(0)
        headers = {"Authorization": "Bearer " + self.settings.model_api_key} if self.settings.model_api_key else {}
        async with httpx.AsyncClient(timeout=self.settings.model_timeout, trust_env=False) as client:
            response = await client.post(self.settings.model_url + "/chat/completions", headers=headers, json={
                "model": self.settings.model_name,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                "temperature": 0.45, "max_tokens": 256, "cache_prompt": True,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "draft", "strict": True, "schema": Draft.model_json_schema()}},
            })
            response.raise_for_status()
            if len(response.content) > 100000:
                raise ValueError("model_response_too_large")
            result = Draft.model_validate_json(response.json()["choices"][0]["message"]["content"])
            return result.model_dump()
