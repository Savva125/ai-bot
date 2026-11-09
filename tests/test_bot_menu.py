import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Update, Message, Chat, User, CallbackQuery
import httpx

from creator_desk_server.bot import build_dispatcher
from creator_desk_server.bot_menu import home


def test_menu_permissions_creation_and_double_click(monkeypatch):
    real = httpx.AsyncClient
    requests, replies = [], []
    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={'id': str(uuid4()), 'key': 'test-key', 'days': 30, 'daily_limit': 500})
    monkeypatch.setattr('creator_desk_server.bot_menu.httpx.AsyncClient',
        lambda **kw: real(transport=httpx.MockTransport(transport), **kw))
    class Session(BaseSession):
        async def close(self): pass
        async def make_request(self, bot, method, timeout=None):
            replies.append(method)
            return True
        async def stream_content(self, *args, **kwargs):
            yield b''
    async def run():
        bot = Bot('123456:' + 'x'*35, session=Session())
        dp = build_dispatcher({42}, 'http://127.0.0.1:8000', 'a'*48)
        cases = [(7, 'private', 'menu:create:30'), (42, 'group', 'menu:create:30'),
                 (42, 'private', 'menu:new'), (42, 'private', 'menu:create-confirm:30'),
                 (42, 'private', 'menu:create:30'), (42, 'private', 'menu:create:30')]
        for i, (uid, kind, action) in enumerate(cases):
            user = User(id=uid, is_bot=False, first_name='Test')
            msg = Message(message_id=100, date=datetime.now(timezone.utc), chat=Chat(id=uid, type=kind), text='menu')
            q = CallbackQuery(id=str(i), from_user=user, chat_instance='test', message=msg, data=action)
            await dp.feed_update(bot, Update(update_id=i+1, callback_query=q))
        await bot.session.close()
    asyncio.run(run())
    assert len(requests) == 1
    secrets = [m for m in replies if isinstance(m, SendMessage) and m.protect_content]
    assert len(secrets) == 1 and 'test-key' in secrets[0].text
    assert any(isinstance(m, SendMessage) and 'уже выполнено' in m.text for m in replies)
    for reply in replies:
        markup = getattr(reply, 'reply_markup', None)
        if markup:
            assert all(len(b.callback_data.encode()) <= 64 for row in markup.inline_keyboard for b in row)


def test_local_buttons_are_opt_in(monkeypatch):
    monkeypatch.delenv('CREATOR_DESK_LOCAL_CONTROL', raising=False)
    assert 'menu:launch' not in str(home())
    monkeypatch.setenv('CREATOR_DESK_LOCAL_CONTROL', '1')
    assert 'menu:launch' in str(home())
