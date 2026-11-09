"""Private Telegram control plane. No polling or messages until explicitly started."""
import asyncio
from datetime import datetime, timezone
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
import httpx

from creator_desk.endpoints import validate_server_url


HELP = """Управление доступом
/new [дни] [лимит_в_сутки] [устройства] [метка]
/list [страница]
/info ID
/extend ID дни
/block ID
/unblock ID
/reset ID

Пример: /new 30 500 1 Иван
Срок начинается с первой активации. Лимит обновляется в 03:00 МСК.
После /reset старые сеансы недействительны; ключ можно активировать заново.
Ключ показывается только при создании. Для замены заблокируйте старый и создайте новый.
"""


def allowed(message, admins):
    return (message.chat.type == "private" and message.from_user is not None
            and message.from_user.id in admins)


def describe(item):
    expires = (datetime.fromtimestamp(item["expires_at"], timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
               if item["expires_at"] else "после первой активации")
    return (f"ID: {item['id']}\nМетка: {item['label']}\n"
            f"Статус: {'заблокирован' if item['blocked'] else 'доступен'}\nДо: {expires}\n"
            f"Запросы сегодня: {item['used_today']}/{item['daily_limit']}\n"
            f"Устройства: {item['devices']}/{item['max_devices']}")


def build_dispatcher(admins, api_url, token):
    dp = Dispatcher()

    @dp.message(Command("start", "help", "new", "list", "info", "extend", "block", "unblock", "reset"))
    async def handle(message: Message):
        if not allowed(message, admins):
            return
        parts = (message.text or "").split()
        command = parts[0].split("@")[0][1:]
        if command in {"start", "help"}:
            await message.answer(HELP)
            return
        try:
            method, body = "GET", None
            if command == "new":
                if len(parts) < 4:
                    raise ValueError()
                days, limit, devices = map(int, parts[1:4])
                body = {"days": days, "daily_limit": limit, "max_devices": devices,
                        "label": " ".join(parts[4:])}
                method, path = "POST", "/admin/licenses"
            elif command == "list":
                page = int(parts[1]) if len(parts) == 2 else 1
                if page < 1 or page > 50001:
                    raise ValueError()
                path = f"/admin/licenses?offset={(page - 1) * 20}"
            else:
                from uuid import UUID
                license_id = str(UUID(parts[1]))
                path = "/admin/licenses/" + license_id
                if command != "info":
                    method, path = "POST", path + "/" + command
                if command == "extend":
                    body = {"days": int(parts[2])}
            async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
                response = await client.request(method, api_url + path, json=body,
                    headers={"Authorization": "Bearer " + token,
                             "X-Admin-Actor": "telegram:" + str(message.from_user.id)})
            if not response.is_success:
                errors = {401: "Сеанс администратора недействителен.", 403: "Админский токен отклонён.",
                          404: "Ключ с таким ID не найден.", 422: "Проверьте параметры команды."}
                await message.answer(errors.get(response.status_code, "API временно недоступен."))
                return
            data = response.json()
            if command == "new":
                reply = (f"Создан доступ\nID: {data['id']}\nКлюч: {data['key']}\n"
                         f"Дни: {data['days']}\nЛимит: {data['daily_limit']}\n"
                         "Сохраните ключ: повторно получить его нельзя.")
                await message.answer(reply, protect_content=True)
            elif command == "list":
                if not data:
                    await message.answer("На этой странице ключей нет.")
                else:
                    # Each result fits Telegram's message limit even for long labels.
                    for index in range(0, len(data), 5):
                        await message.answer("\n\n".join(describe(i) for i in data[index:index + 5]))
            else:
                await message.answer(describe(data))
        except (ValueError, IndexError, KeyError):
            await message.answer("Неверные параметры. Формат команд: /help")
        except httpx.HTTPError:
            await message.answer("Нет связи с API. Результат команды неизвестен; проверьте /list перед повтором.")

    return dp


def main():
    admins = {int(v.strip()) for v in os.environ["TELEGRAM_ADMIN_IDS"].split(",") if v.strip()}
    if not admins or any(v <= 0 for v in admins):
        raise ValueError("TELEGRAM_ADMIN_IDS must contain explicit numeric user IDs")
    raw_url = os.getenv("ADMIN_API_URL", "http://127.0.0.1:8000").rstrip("/")
    # The compose service name is trusted only inside the private Docker network.
    url = raw_url if raw_url == "http://api:8000" else validate_server_url(raw_url)
    token = os.environ["ADMIN_API_TOKEN"]
    if len(token) < 32:
        raise ValueError("ADMIN_API_TOKEN is too short")

    async def run():
        bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"])
        dp = build_dispatcher(admins, url, token)
        # Do not execute old queued administrative commands after a restart.
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message"], tasks_concurrency_limit=4)
    asyncio.run(run())


if __name__ == "__main__":
    main()
