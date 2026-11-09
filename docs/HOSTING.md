# Запуск хоста и Telegram-бота

## Что подготовить

Linux-хост с NVIDIA GPU, драйвером, Docker Engine/Compose 2.30+ и поддержкой GPU
контейнеров. Объём VRAM зависит от модели, контекста и параллелизма; начните
с одной генерации и измерьте нагрузку. Публичный DNS-адрес API должен указывать
на хост; откройте TCP 80/443. Сайта нет, адрес используется только приложением.
Нужны токен собственного бота от BotFather и ваш числовой Telegram user ID.

## Docker Compose

1. Скопируйте проект на хост без пользовательских БД, `.venv`, кэшей и секретов.
2. Поместите совместимую GGUF-модель в `/srv/creator-desk/models/model.gguf`.
   Существующая Qwen-модель находится в `models/model.gguf` в исходном проекте;
   перенесите её на хост отдельно. В релизные ZIP веса не включаются.
3. Скопируйте `deploy/.env.example` в `deploy/.env`, заполните значения.
4. Для пароля PostgreSQL и отдельного админского токена сгенерируйте разные
   значения: `python -c "import secrets; print(secrets.token_hex(32))"`.
   Используйте hex-пароль: он подставляется в URL PostgreSQL без экранирования.
5. Укажите API_DOMAIN, MODEL_DIRECTORY, TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_IDS.
   Несколько разрешённых Telegram ID разделяются запятой.

Из корня проекта:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
curl --fail https://ai.example.com/health
```

Caddy автоматически получает TLS-сертификат. Наружу опубликован только прокси;
PostgreSQL, модель и административный API находятся во внутренней Docker-сети.
/admin/* снаружи возвращает 404. Бот обращается к API внутри сети.
`migrate` должен завершиться с кодом 0, после него стартует API.
/health проверяет API и БД, но не готовность модели: она может загружаться дольше.

После проверки на вашей GPU закрепите LLAMA_SERVER_IMAGE по digest.
Для выпуска также закрепите образы PostgreSQL/Caddy. Конфигурация рассчитана
на один экземпляр API с одним worker: увеличение реплик требует общей очереди.

## Ключи

В личном чате с ботом с разрешённого аккаунта:

```text
/help
/new 30 500 1 Первый пользователь
```

Бот выдаст ID и ключ. Сохраните и передайте ключ пользователю: исходник в БД
не хранится и повторно не показывается. Срок начинается с первой активации.
/info ID показывает расход, срок и установки. /extend ID 30 продлевает доступ:
до активации добавляет дни, после истечения отсчитывает новый срок от продления.
/unblock снимает блокировку, но не продлевает срок.

/reset ID отзывает токены и освобождает установки. Прежний ключ остаётся рабочим.
При утечке ключа используйте /block и создайте замену. Идентификатор установки
не является аппаратной защитой: его можно скопировать вместе с секретами.

## Клиент для выдачи

На Windows:

```powershell
.venv\Scripts\python.exe -m pip install ".[desktop,build]"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-client.ps1 -ServerUrl https://ai.example.com
```

Архивируйте всю папку dist\CreatorDesk, включая _internal, и передайте её с ключом.
Не передавайте deploy/.env, серверные файлы, модель или пользовательские БД.
При первом запуске адрес будет заполнен; пользователь вводит только ключ.
Без ServerUrl адрес можно задать в окне активации.

Токен действует максимум 30 дней, потом автоматически обновляется повторной
проверкой ключа. Заблокированный/просроченный ключ не обновляется.
Хранилище доступа привязано к адресу API: изменение адреса потребует активации.

## Обслуживание

Не включайте логирование HTTP-тел и Authorization на прокси.
Настройте резервные копии PostgreSQL через pg_dump вне хоста и проверьте
восстановление в отдельную БД. Сохраните конфигурацию, секреты и версии образов.
Не используйте `docker compose down -v` для обновления: команда удалит том базы.

Перед обновлением сделайте резервную копию БД. Миграции можно выполнить отдельно:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm migrate
```

Запускайте новую API-версию только после успешного завершения миграций.
Лимит обновляется в 00:00 UTC / 03:00 МСК. Принятый сервером запрос может
расходовать лимит, даже если пользователь остановил ожидание в приложении.
При занятой GPU клиент показывает ошибку и останавливается; повторный старт ручной.
Ответы кэшируются до часа с очисткой раз в минуту; входные переписки в БД не сохраняются.

## Разработка без Docker

Установите сервер в отдельное окружение Python 3.12: `pip install ".[server]"`.
Задайте SERVER_DATABASE_URL=postgresql+psycopg://..., ADMIN_API_TOKEN,
MODEL_URL=http://127.0.0.1:8081/v1 и MODEL_NAME=creator-desk.
Запустите llama-server с этой моделью и alias, затем:

```sh
python -m creator_desk_server.manage migrate
uvicorn creator_desk_server.api:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --no-proxy-headers
```

Боту дополнительно нужны TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_IDS,
ADMIN_API_URL=http://127.0.0.1:8000. Запуск: `python -m creator_desk_server.bot`.
Удалённый доступ требует HTTPS-прокси, закрывающего /admin/*.
Продукционная конфигурация требует PostgreSQL; SQLite допускается только
явным тестовым Settings, не через переменные окружения.

Первоисточники: [FastAPI deployment](https://fastapi.tiangolo.com/deployment/docker/),
[aiogram polling](https://docs.aiogram.dev/en/latest/dispatcher/long_polling.html),
[llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).

В Docker API доверяет forwarded-заголовкам только благодаря приватной сети:
не публикуйте порт 8000. Caddy подставляет адрес клиента для ограничения активаций.
Распознавание неструктурированных окон тоже расходует дневной лимит.

Поле gpus требует [Docker Compose 2.30+](https://docs.docker.com/reference/compose-file/services/#gpus).
