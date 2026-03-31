# TelegramBot

Отдельный Telegram-бот, который деплоится на Railway как самостоятельный сервис и не требует изменений в коде сайта.

## Что умеет

- вход через Telegram в уже существующий аккаунт сайта
- регистрация нового пользователя сразу из Telegram
- привязка Telegram к пользователю через backend API
- просмотр профиля, персональных новостей и сводки
- общение с ИИ через backend route `POST /api/assistant/chat`
- healthcheck на `GET /health` для Railway

## Структура

- `bot.py` - основной код бота
- `setup.sh` - build/start/check для Railway
- `Dockerfile` - контейнер для Railway
- `railway.json` - healthcheck и deploy config
- `.env.example` - шаблон переменных
- `start_bot.bat` - локальный запуск на Windows

## Переменные окружения

- `TELEGRAM_BOT_TOKEN` - токен бота
- `BACKEND_API_URL` - URL сайта или backend API без `/api` в конце
- `REQUEST_TIMEOUT` - таймаут HTTP-запросов
- `LOG_LEVEL` - уровень логов
- `BOT_NAME` - имя в health/status ответах
- `HEALTH_HOST` - хост мини HTTP сервера
- `HEALTH_PATH` - путь healthcheck, по умолчанию `/health`
- `PORT` - порт, Railway подставляет автоматически

## Railway

Если будешь загружать именно эту папку отдельным проектом:

1. Создай новый сервис на Railway.
2. Загрузи содержимое папки `TelegramBot`.
3. Railway сам увидит `Dockerfile` и `railway.json`.
4. Проверь переменные окружения.
5. После деплоя бот поднимет polling и HTTP health endpoint.

Если Railway попросит команды вручную:

- Build Command: `sh ./setup.sh build`
- Start Command: `sh ./setup.sh start`

## API, которые использует бот

- `GET /api/health`
- `GET /api/users/telegram/<telegram_id>`
- `POST /api/auth/telegram/login`
- `POST /api/auth/telegram/register`
- `POST /api/news/fetch`
- `GET /api/users/telegram/<telegram_id>/digest`
- `GET /api/users/telegram/<telegram_id>/stats`
- `PUT /api/users/telegram/<telegram_id>/interests`
- `POST /api/assistant/chat`

## Локальный запуск

```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python bot.py
```
