"""Button-driven private administration and opt-in local desktop launcher."""
import asyncio
import os
from pathlib import Path
import subprocess
from uuid import UUID

from aiogram import F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import httpx


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows])


def home():
    rows = [[('🔑 Создать ключ', 'menu:new'), ('🗂 Мои ключи', 'menu:list:0')],
            [('📊 Состояние', 'menu:status'), ('❓ Как пользоваться', 'menu:help')]]
    if os.getenv('CREATOR_DESK_LOCAL_CONTROL') == '1':
        rows.insert(0, [('▶️ Открыть приложение', 'menu:launch')])
        rows.insert(1, [('⏹ Закрыть приложение', 'menu:stop-confirm')])
    return keyboard(rows)


WELCOME = ('🤖 AI-помощник · Панель управления\n\n'
           'Откройте приложение на ПК, создайте ключ или управляйте доступом кнопками ниже.\n'
           'Панель доступна только администраторам.')


def desktop_processes():
    import psutil
    root = Path(__file__).resolve().parents[1]
    expected = str(root / '.venv' / 'Scripts').lower()
    found = []
    for p in psutil.process_iter(['exe', 'cmdline']):
        try:
            args = p.info['cmdline'] or []
            if expected in (p.info['exe'] or '').lower() and any(
                args[i:i+2] == ['-m', 'creator_desk'] for i in range(len(args)-1)):
                found.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def local_action(action):
    if os.name != 'nt' or os.getenv('CREATOR_DESK_LOCAL_CONTROL') != '1':
        return 'Управление приложением доступно только при локальном запуске на Windows.'
    processes = desktop_processes()
    if action == 'stop':
        for p in processes:
            try:
                for child in p.children(recursive=True):
                    child.terminate()
                p.terminate()
            except Exception:
                return 'Не удалось закрыть приложение. Проверьте его окно на ПК.'
        return '⏹ Приложение закрыто. Сервер и бот продолжают работать.'
    if processes:
        return '✅ Приложение уже открыто на ПК. Переключитесь в его окно.'
    root = Path(__file__).resolve().parents[1]
    subprocess.Popen([str(root / '.venv/Scripts/pythonw.exe'), '-m', 'creator_desk'], cwd=root,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    return ('▶️ Приложение запускается на ПК.\n'
            'В его окне нажмите «Начать работу» и выберите переписку.\n'
            'Если ПК заблокирован, сначала разблокируйте его.')


def register_menu(dp, admins, api_url, token):
    lock = asyncio.Lock()
    consumed = set()

    async def request(method, path, actor, body=None):
        async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
            r = await client.request(method, api_url + path, json=body, headers={
                'Authorization': 'Bearer ' + token, 'X-Admin-Actor': 'telegram:' + str(actor)})
            r.raise_for_status()
            return r.json()

    @dp.callback_query(F.data.startswith('menu:'))
    async def clicked(query):
        from .bot import describe
        if (query.from_user.id not in admins or query.message is None
                or query.message.chat.type != 'private'
                or query.message.chat.id != query.from_user.id):
            await query.answer('Нет доступа', show_alert=True)
            return
        await query.answer()
        message = query.message
        parts = query.data.split(':')
        action = parts[1]
        async def answer(text, markup=None, secret=False):
            await message.answer(text, reply_markup=markup or home(), protect_content=secret)
        try:
            if action == 'home':
                await answer(WELCOME)
            elif action == 'help':
                await answer('📖 Быстрый старт\n\n1. Нажмите «Открыть приложение».\n'
                    '2. Создайте ключ и сохраните его.\n'
                    '3. При первой активации введите ключ и адрес своего API.\n'
                    '4. На ПК нажмите «Начать работу» и выберите окно переписки.\n\n'
                    'Бот управляет этим ПК. Компьютер и локальный сервер должны работать.\n'
                    'Срок ключа начинается при первой активации.\n'
                    'Обычные команды /new, /list, /info также работают.')
            elif action == 'status':
                async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                    api = (await client.get(api_url + '/health')).is_success
                    try:
                        model = (await client.get('http://127.0.0.1:8081/health')).is_success if os.getenv('CREATOR_DESK_LOCAL_CONTROL') == '1' else None
                    except httpx.HTTPError:
                        model = False
                text = '📊 Состояние\n\nAPI: ' + ('✅ работает' if api else '🔴 недоступен')
                if model is not None:
                    text += '\nМодель: ' + ('✅ готова' if model else '⏳ недоступна')
                    text += '\nПриложение: ' + ('🟢 открыто' if await asyncio.to_thread(desktop_processes) else '⚪ закрыто')
                await answer(text)
            elif action in {'launch', 'stop'}:
                async with lock:
                    await answer(await asyncio.to_thread(local_action, action))
            elif action == 'stop-confirm':
                await answer('Закрыть приложение на ПК? Текущая работа помощника остановится.', keyboard([
                    [('⏹ Да, закрыть', 'menu:stop')], [('↩️ Назад', 'menu:home')]]))
            elif action == 'new':
                await answer('🔑 Новый ключ\n\nВыберите срок. Лимит: 500 запросов в сутки, одно устройство.\n'
                    'Для своих параметров: /new дни лимит устройства метка', keyboard([
                    [('7 дней', 'menu:create-confirm:7'), ('30 дней', 'menu:create-confirm:30'), ('90 дней', 'menu:create-confirm:90')],
                    [('↩️ Меню', 'menu:home')]]))
            elif action in {'create-confirm', 'create'}:
                days = int(parts[2])
                if days not in {7, 30, 90}: raise ValueError()
                if action == 'create-confirm':
                    await answer(f'Создать ключ на {days} дней, 500 запросов/сутки и одно устройство?', keyboard([
                        [('✅ Создать', f'menu:create:{days}')], [('↩️ Назад', 'menu:new')]]))
                else:
                    async with lock:
                        marker = (message.chat.id, message.message_id)
                        if marker in consumed:
                            await answer('Это действие уже выполнено. Проверьте список ключей.')
                            return
                        consumed.add(marker)
                        # Remove the action before the mutation to prevent accidental double clicks.
                        await query.message.edit_reply_markup(reply_markup=keyboard([[('↩️ Меню', 'menu:home')]]))
                        data = await request('POST', '/admin/licenses', query.from_user.id,
                            {'days': days, 'daily_limit': 500, 'max_devices': 1, 'label': f'Telegram {query.from_user.id}'})
                        await answer(f"✅ Ключ создан\n\n{data['key']}\n\nСрок: {days} дней с активации\n"
                            'Лимит: 500 в сутки · 1 устройство\n\nСохраните ключ: повторно его посмотреть нельзя.', secret=True)
            elif action == 'list':
                offset = int(parts[2])
                if not 0 <= offset <= 1000000 or offset % 20: raise ValueError()
                data = await request('GET', f'/admin/licenses?offset={offset}', query.from_user.id)
                if not data:
                    await answer('🗂 На этой странице ключей нет.')
                else:
                    from .bot import describe
                    for item in data:
                        await answer('🔑 ' + describe(item), keyboard([
                            [('⚙️ Управлять', 'menu:info:' + item['id'])], [('🏠 Меню', 'menu:home')]]))
                    await answer('🗂 Навигация', keyboard([
                        [('⬅️ Назад', f'menu:list:{max(0, offset-20)}'), ('Далее ➡️', f'menu:list:{offset+20}')],
                        [('🏠 Меню', 'menu:home')]]))
            elif action in {'info', 'extend', 'block', 'unblock', 'reset', 'confirm'}:
                license_id = str(UUID(parts[2]))
                if action == 'confirm':
                    operation = parts[3]
                    titles = {'block': 'Заблокировать ключ', 'unblock': 'Разблокировать ключ',
                              'reset': 'Сбросить устройства и отозвать сеансы', 'extend': 'Продлить на 30 дней'}
                    await answer(titles[operation] + '?', keyboard([
                        [('✅ Подтвердить', f'menu:{operation}:{license_id}')],
                        [('↩️ Назад', f'menu:info:{license_id}')]]))
                    return
                path = '/admin/licenses/' + license_id
                if action != 'info':
                    async with lock:
                        marker = (message.chat.id, message.message_id)
                        if marker in consumed:
                            await answer('Это действие уже выполнено. /start')
                            return
                        consumed.add(marker)
                        await query.message.edit_reply_markup(reply_markup=keyboard([[('🏠 Меню', 'menu:home')]]))
                        data = await request('POST', path + '/' + action, query.from_user.id,
                            {'days': 30} if action == 'extend' else None)
                else:
                    data = await request('GET', path, query.from_user.id)
                op = 'unblock' if data['blocked'] else 'block'
                await answer('🔑 ' + describe(data), keyboard([
                    [('➕ 30 дней', f'menu:confirm:{license_id}:extend')],
                    [('🔓 Разблокировать' if data['blocked'] else '🔒 Заблокировать', f'menu:confirm:{license_id}:{op}')],
                    [('🔄 Сбросить устройства', f'menu:confirm:{license_id}:reset')],
                    [('🗂 Ключи', 'menu:list:0'), ('🏠 Меню', 'menu:home')]]))
        except httpx.HTTPError:
            await answer('⚠️ Нет связи с API или запрос отклонён. Если меняли ключ, проверьте список перед повтором.')
        except (ValueError, KeyError, IndexError):
            await answer('Эта кнопка устарела. Откройте меню заново: /start.')
        except OSError:
            await answer('Не удалось выполнить действие на ПК. Проверьте локальный сервер.')
