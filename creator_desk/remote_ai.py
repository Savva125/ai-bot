"""Hosted provider. No model binaries, admin secrets or local inference required."""
import asyncio
import hashlib
import json
from threading import Event
from uuid import uuid4

import httpx

from .errors import AiError
from .providers import Result
from .security import read_secret, save_secret
from .endpoints import validate_server_url


ERRORS = {
    "invalid_key": "Ключ доступа не найден.",
    "license_blocked": "Доступ заблокирован. Обратитесь к администратору.",
    "license_expired": "Срок доступа истёк. Обратитесь к администратору для продления.",
    "device_limit": "Ключ уже привязан к другому устройству. Попросите администратора сбросить привязку.",
    "daily_limit": "Дневной лимит исчерпан. Он обновится в 03:00 по Москве.",
    "server_busy": "Сервер занят. Работа остановлена; попробуйте начать позже.",
    "generation_in_progress": "Предыдущий запрос ещё обрабатывается. Попробуйте позже.",
    "activation_rate_limit": "Слишком много попыток активации. Повторите через минуту.",
    "request_rate_limit": "Слишком частые запросы. Повторите через минуту.",
    "model_unavailable": "Модель временно недоступна. Попробуйте позже.",
    "session_expired": "Требуется повторная активация ключа.",
}


def check_response(response):
    if not response.is_success:
        try:
            code = response.json().get("detail")
        except (ValueError, AttributeError):
            code = None
        raise AiError(ERRORS.get(code if isinstance(code, str) else "", "Сервер отклонил запрос. Проверьте доступ или обратитесь к администратору."))
    if len(response.content) > 100000:
        raise AiError("Сервер вернул слишком большой ответ.")
    return response.json()


class RemoteProvider:
    name = "AI на сервере"

    def __init__(self, server_url, token="", license_key="", device_id="", stop_event=None):
        self.server_url = validate_server_url(server_url)
        self.token = token
        self.license_key = license_key
        self.device_id = device_id
        self.stop_event = stop_event or Event()
        self.vault_scope = hashlib.sha256(self.server_url.encode()).hexdigest()[:20]

    async def activate(self, client):
        data = check_response(await client.post(self.server_url + "/v1/activate",
            json={"key": self.license_key, "device_id": self.device_id}, timeout=15))
        token = data.get("token")
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise AiError("Некорректный ответ сервера активации.")
        self.token = token
        return data

    def persist(self):
        save_secret("remote_access_" + self.vault_scope, json.dumps({"token": self.token,
            "key": self.license_key, "device_id": self.device_id}))

    @classmethod
    def load(cls, url):
        provider = cls(url)
        value = read_secret("remote_access_" + provider.vault_scope)
        if value:
            data = json.loads(value)
            provider.token, provider.license_key, provider.device_id = data["token"], data["key"], data["device_id"]
        return provider

    def activate_sync(self, key):
        self.license_key = key.strip()
        if not self.device_id:
            self.device_id = uuid4().hex

        async def work():
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                return await self.activate(client)
        try:
            result = asyncio.run(work())
            self.persist()
            return result
        except httpx.HTTPError:
            raise AiError("Не удалось подключиться к серверу. Проверьте адрес и интернет.") from None

    def generate(self, context):
        # Match the existing local model's context budget before crossing the network.
        context = dict(context)
        context["history"] = [{"role": m["role"], "text": m["text"][:900]}
                              for m in context["history"][-16:]]
        value = self._call({"request_id": str(uuid4()), "context": context}, "/v1/generate")
        if (not isinstance(value, dict) or not isinstance(value.get("text"), str)
                or type(value.get("handoff")) is not bool
                or value.get("action") not in {"reply", "wait"}
                or not isinstance(value.get("reason", ""), str)
                or len(value["text"]) > 4000):
            raise AiError("Некорректный ответ сервера.")
        if value["action"] == "reply" and not value["handoff"] and not value["text"].strip():
            raise AiError("Сервер вернул пустой ответ.")
        return Result("" if value["handoff"] or value["action"] == "wait" else value["text"],
                      value["handoff"], value.get("reason", ""), value["action"])

    def inspect_screen(self, compact):
        value = self._call({"request_id": str(uuid4()), "screen": compact}, "/v1/inspect")
        if not isinstance(value, dict) or type(value.get("is_chat")) is not bool:
            raise AiError("Некорректный ответ распознавания.")
        return value

    def _call(self, payload, path):
        if self.stop_event.is_set():
            raise AiError("Генерация остановлена.")

        async def request():
            async with httpx.AsyncClient(timeout=httpx.Timeout(210, connect=10), trust_env=False,
                                         follow_redirects=False) as client:
                if not self.token:
                    await self.activate(client)
                    self.persist()
                response = await client.post(self.server_url + path, json=payload,
                    headers={"Authorization": "Bearer " + self.token})
                if response.status_code == 401:
                    # Authentication rejection happens before any inference: safe to reauthenticate.
                    await self.activate(client)
                    self.persist()
                    response = await client.post(self.server_url + path, json=payload,
                        headers={"Authorization": "Bearer " + self.token})
                return check_response(response)

        async def cancellable():
            task = asyncio.create_task(request())
            try:
                while not task.done():
                    if self.stop_event.is_set():
                        task.cancel()
                        raise AiError("Генерация остановлена.")
                    await asyncio.wait({task}, timeout=0.1)
                if self.stop_event.is_set():
                    raise AiError("Генерация остановлена.")
                return await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        try:
            return asyncio.run(cancellable())
        except httpx.HTTPError:
            raise AiError("Связь с сервером прервана. Ответ не отправлен; работа остановлена.") from None
        except (ValueError, KeyError, TypeError):
            raise AiError("Некорректный ответ сервера.") from None
