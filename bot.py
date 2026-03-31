"""Standalone Telegram bot that works with the site backend API."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)


@dataclass(slots=True)
class BotConfig:
    token: str
    backend_api_url: str
    request_timeout: int
    log_level: str
    bot_name: str
    health_host: str
    health_path: str
    port: int
    retry_seconds: int


def load_config() -> BotConfig:
    backend_api_url = (
        os.getenv("BACKEND_API_URL")
        or os.getenv("BACKEND_URL")
        or "http://127.0.0.1:5000"
    ).strip()
    health_path = (os.getenv("HEALTH_PATH") or "/health").strip() or "/health"
    if not health_path.startswith("/"):
        health_path = f"/{health_path}"
    return BotConfig(
        token=(
            os.getenv("TELEGRAM_BOT_TOKEN")
            or os.getenv("BOT_TOKEN")
            or os.getenv("TG_BOT_TOKEN")
            or ""
        ).strip(),
        backend_api_url=backend_api_url,
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
        log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
        bot_name=(os.getenv("BOT_NAME") or "IDO SKILLS News Bot").strip(),
        health_host=(os.getenv("HEALTH_HOST") or "0.0.0.0").strip(),
        health_path=health_path,
        port=int(os.getenv("PORT", "8080")),
        retry_seconds=max(5, int(os.getenv("BOT_RETRY_SECONDS", "15"))),
    )


CONFIG = load_config()


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, CONFIG.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    return logging.getLogger("telegram_bot")


LOGGER = setup_logging()


RUNTIME_STATE: dict[str, Any] = {
    "configured": bool(CONFIG.token),
    "bot_started": False,
    "last_error": "",
}
RUNTIME_LOCK = threading.Lock()


def set_runtime_state(**updates: Any) -> None:
    with RUNTIME_LOCK:
        RUNTIME_STATE.update(updates)


def runtime_state_snapshot() -> dict[str, Any]:
    with RUNTIME_LOCK:
        return dict(RUNTIME_STATE)


class BackendAPIError(RuntimeError):
    """Raised when backend API responds with an error."""


class BackendAPIClient:
    def __init__(self, base_url: str, timeout: int):
        root = base_url.rstrip("/")
        self.base_url = root if root.endswith("/api") else f"{root}/api"
        self.timeout = timeout

    def _should_bypass_proxy(self) -> bool:
        host = (urlparse(self.base_url).hostname or "").lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    def _request(self, method: str, path: str, *, json_data: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            with requests.Session() as session:
                if self._should_bypass_proxy():
                    session.trust_env = False
                response = session.request(
                    method=method,
                    url=f"{self.base_url}{path}",
                    json=json_data,
                    timeout=self.timeout,
                )
        except requests.RequestException as exc:
            raise BackendAPIError(f"Backend is unavailable: {exc}") from exc

        try:
            data = response.json()
        except ValueError:
            data = {}

        if response.status_code >= 400:
            message = data.get("error") or data.get("message") or response.text or f"HTTP {response.status_code}"
            raise BackendAPIError(str(message).strip())
        return data

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def get_user_by_telegram(self, telegram_id: int) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/users/telegram/{telegram_id}")
        except BackendAPIError:
            return None

    def telegram_login(
        self,
        *,
        telegram_id: int,
        username: str,
        chat_id: int,
        identifier: str,
        password: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/auth/telegram/login",
            json_data={
                "telegram_id": telegram_id,
                "telegram_username": username,
                "telegram_chat_id": chat_id,
                "login": identifier,
                "password": password,
            },
        )

    def telegram_register(
        self,
        *,
        telegram_id: int,
        username: str,
        chat_id: int,
        login: str,
        email: str,
        password: str,
        interests: list[str],
        threshold: int,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/auth/telegram/register",
            json_data={
                "telegram_id": telegram_id,
                "telegram_username": username,
                "telegram_chat_id": chat_id,
                "login": login,
                "email": email,
                "password": password,
                "interests": interests,
                "threshold": threshold,
            },
        )

    def get_personal_news(self, telegram_id: int) -> dict[str, Any]:
        return self._request("POST", "/news/fetch", json_data={"telegram_id": telegram_id})

    def get_digest(self, telegram_id: int) -> dict[str, Any]:
        return self._request("GET", f"/users/telegram/{telegram_id}/digest")

    def get_stats(self, telegram_id: int) -> dict[str, Any]:
        return self._request("GET", f"/users/telegram/{telegram_id}/stats")

    def update_interests(self, telegram_id: int, interests: list[str], threshold: int) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/users/telegram/{telegram_id}/interests",
            json_data={"interests": interests, "threshold": threshold},
        )

    def assistant_chat(self, telegram_id: int, message: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/assistant/chat",
            json_data={"telegram_id": telegram_id, "message": message},
        )


CLIENT = BackendAPIClient(CONFIG.backend_api_url, CONFIG.request_timeout)


BTN_AI = "ИИ чат"
BTN_NEWS = "Мои новости"
BTN_DIGEST = "Сводка"
BTN_PROFILE = "Профиль"
BTN_INTERESTS = "Интересы"
BTN_LOGIN = "Войти"
BTN_REGISTER = "Регистрация"
BTN_HELP = "Помощь"
BTN_MENU = "Меню"


def parse_interests(value: str) -> list[str]:
    parts = [item.strip() for item in value.replace("\n", ",").split(",")]
    unique: list[str] = []
    seen: set[str] = set()
    for item in parts:
        key = item.lower()
        if item and key not in seen:
            unique.append(item)
            seen.add(key)
    return unique


def main_keyboard(authorized: bool) -> ReplyKeyboardMarkup:
    if authorized:
        rows = [
            [BTN_AI, BTN_NEWS],
            [BTN_DIGEST, BTN_PROFILE],
            [BTN_INTERESTS, BTN_HELP],
        ]
    else:
        rows = [
            [BTN_LOGIN, BTN_REGISTER],
            [BTN_HELP],
        ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def escape(value: Any) -> str:
    return html.escape(str(value or ""))


async def call_backend(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def send_long_message(update: Update, text: str, *, reply_markup=None):
    message = update.effective_message
    if not message:
        return

    chunk_size = 3900
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)] or [text]
    for index, chunk in enumerate(chunks):
        await message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )


def store_session(context: ContextTypes.DEFAULT_TYPE, payload: dict[str, Any] | None):
    if not payload:
        context.user_data.pop("session", None)
        return
    context.user_data["session"] = payload


def get_session(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return dict(context.user_data.get("session") or {})


def current_user(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    return get_session(context).get("user")


def clear_flow(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("flow", None)
    context.user_data.pop("draft", None)


def current_threshold(context: ContextTypes.DEFAULT_TYPE) -> int:
    user = current_user(context) or {}
    try:
        return max(1, min(10, int(user.get("news_threshold", 6))))
    except (TypeError, ValueError):
        return 6


async def ensure_linked_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    telegram_id = update.effective_user.id
    payload = await call_backend(CLIENT.get_user_by_telegram, telegram_id)
    if payload:
        store_session(context, {"user": payload})
        return payload

    store_session(context, None)
    await send_long_message(
        update,
        "Аккаунт пока не привязан к Telegram.\n\n"
        "Нажмите <b>Войти</b>, если аккаунт уже есть на сайте, или <b>Регистрация</b>, "
        "чтобы создать новый и сразу привязать его к Telegram.",
        reply_markup=main_keyboard(False),
    )
    return None


def profile_text(user: dict[str, Any], stats: dict[str, Any] | None = None) -> str:
    interests = ", ".join(user.get("interests") or []) or "не заданы"
    lines = [
        f"<b>{escape(CONFIG.bot_name)}</b>",
        "",
        "<b>Ваш профиль</b>",
        f"Логин: <code>{escape(user.get('login'))}</code>",
        f"Email: <code>{escape(user.get('email') or 'не указан')}</code>",
        f"Интересы: {escape(interests)}",
        f"Порог важности: {escape(user.get('news_threshold', 6))}",
    ]
    if user.get("telegram_username"):
        lines.append(f"Telegram: @{escape(user.get('telegram_username'))}")
    if stats:
        lines.extend(
            [
                "",
                "<b>Статистика</b>",
                f"Прочитано: {escape(stats.get('read_count', 0))}",
                f"Закладок: {escape(stats.get('bookmarks_count', 0))}",
                f"Дней активности: {escape(stats.get('streak_days', 1))}",
            ]
        )
    return "\n".join(lines)


def news_text(payload: dict[str, Any]) -> str:
    items = payload.get("news") or []
    if not items:
        return "По вашим интересам пока нет подходящих новостей."

    lines = ["<b>Персональная лента</b>"]
    for index, item in enumerate(items[:6], start=1):
        lines.append("")
        lines.append(f"{index}. <b>{escape(item.get('title'))}</b>")
        lines.append(f"Источник: {escape(item.get('source') or 'не указан')}")
        lines.append(f"Категория: {escape(item.get('category') or 'без категории')}")
        lines.append(f"Важность: {escape(item.get('importance_score') or '—')}/10")
        if item.get("summary"):
            lines.append(f"Кратко: {escape(item.get('summary'))}")
        if item.get("url"):
            lines.append(f"<a href=\"{escape(item.get('url'))}\">Открыть источник</a>")
    return "\n".join(lines)


def digest_text(payload: dict[str, Any]) -> str:
    return (
        "<b>Персональная сводка</b>\n"
        f"Новостей в подборке: {escape(payload.get('news_count', 0))}\n\n"
        f"{escape(payload.get('digest') or 'Сводка пока пустая.')}"
    )


def assistant_text(payload: dict[str, Any]) -> str:
    reply = escape(payload.get("reply") or "Ответ пока пустой.")
    sources = payload.get("sources") or []
    if not sources:
        return reply

    lines = [reply, "", "<b>Источники</b>"]
    for source in sources:
        title = escape(source.get("title") or source.get("url") or "Источник")
        url = escape(source.get("url") or "")
        if url:
            lines.append(f"• <a href=\"{url}\">{title}</a>")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_flow(context)
    user = await ensure_linked_user(update, context)
    if user:
        await send_long_message(
            update,
            "Бот подключен к вашему аккаунту.\n\n"
            "Можно открывать профиль, смотреть свои новости и просто писать сообщения ИИ.",
            reply_markup=main_keyboard(True),
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    authorized = bool(current_user(context))
    await send_long_message(
        update,
        "Что умеет бот:\n"
        "/start - показать меню\n"
        "/help - краткая справка\n"
        "/menu - открыть меню\n\n"
        "Если аккаунт уже привязан, можно просто писать сообщения обычным текстом, "
        "и бот ответит как ИИ-ассистент.\n\n"
        "Примеры:\n"
        "• Что у меня сейчас самое важное в новостях\n"
        "• Кратко перескажи мою сводку\n"
        "• Какие темы у меня в интересах\n"
        "• Что стоит прочитать первым",
        reply_markup=main_keyboard(authorized),
    )


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_linked_user(update, context)
    if not user:
        return
    stats = await call_backend(CLIENT.get_stats, update.effective_user.id)
    await send_long_message(update, profile_text(user, stats), reply_markup=main_keyboard(True))


async def show_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_linked_user(update, context)
    if not user:
        return
    payload = await call_backend(CLIENT.get_personal_news, update.effective_user.id)
    await send_long_message(update, news_text(payload), reply_markup=main_keyboard(True))


async def show_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_linked_user(update, context)
    if not user:
        return
    payload = await call_backend(CLIENT.get_digest, update.effective_user.id)
    await send_long_message(update, digest_text(payload), reply_markup=main_keyboard(True))


async def begin_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["flow"] = "login_identifier"
    context.user_data["draft"] = {}
    await send_long_message(
        update,
        "Введите ваш логин или email от сайта.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def begin_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["flow"] = "register_login"
    context.user_data["draft"] = {}
    await send_long_message(
        update,
        "Регистрация через Telegram.\n\nШаг 1 из 5: введите желаемый логин.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def begin_interests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_linked_user(update, context)
    if not user:
        return
    context.user_data["flow"] = "update_interests"
    await send_long_message(
        update,
        "Отправьте интересы через запятую.\nПример: <code>AI, стартапы, Python</code>\n\n"
        f"Текущий порог важности сохранится: <b>{current_threshold(context)}</b>.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def explain_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_linked_user(update, context)
    if not user:
        return
    await send_long_message(
        update,
        "Режим ИИ уже активен. Просто напишите вопрос обычным сообщением.\n\n"
        "Например:\n"
        "• Что у меня самое важное сегодня\n"
        "• Объясни мою сводку простыми словами\n"
        "• Что почитать первым\n"
        "• Какие темы у меня сейчас в интересах",
        reply_markup=main_keyboard(True),
    )


async def handle_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    flow = context.user_data.get("flow")
    if not flow:
        return False

    draft = context.user_data.setdefault("draft", {})
    telegram_user = update.effective_user
    telegram_username = telegram_user.username or telegram_user.first_name or "telegram_user"
    telegram_id = telegram_user.id
    chat_id = update.effective_chat.id

    if flow == "login_identifier":
        draft["identifier"] = text
        context.user_data["flow"] = "login_password"
        await send_long_message(update, "Теперь введите пароль.")
        return True

    if flow == "login_password":
        try:
            payload = await call_backend(
                CLIENT.telegram_login,
                telegram_id=telegram_id,
                username=telegram_username,
                chat_id=chat_id,
                identifier=draft.get("identifier", ""),
                password=text,
            )
            store_session(context, payload)
            clear_flow(context)
            await send_long_message(
                update,
                "Вход выполнен. Telegram привязан к аккаунту, можно сразу общаться с ИИ.",
                reply_markup=main_keyboard(True),
            )
        except BackendAPIError as exc:
            clear_flow(context)
            await send_long_message(
                update,
                f"Не удалось войти: {escape(exc)}",
                reply_markup=main_keyboard(False),
            )
        return True

    if flow == "register_login":
        draft["login"] = text
        context.user_data["flow"] = "register_email"
        await send_long_message(
            update,
            "Шаг 2 из 5: введите email или отправьте <code>-</code>, если хотите пропустить.",
        )
        return True

    if flow == "register_email":
        draft["email"] = "" if text == "-" else text
        context.user_data["flow"] = "register_password"
        await send_long_message(
            update,
            "Шаг 3 из 5: введите пароль не короче 6 символов.",
        )
        return True

    if flow == "register_password":
        draft["password"] = text
        context.user_data["flow"] = "register_interests"
        await send_long_message(
            update,
            "Шаг 4 из 5: введите интересы через запятую.",
        )
        return True

    if flow == "register_interests":
        draft["interests"] = parse_interests(text)
        context.user_data["flow"] = "register_threshold"
        await send_long_message(
            update,
            "Шаг 5 из 5: введите порог важности от 1 до 10. Если отправите пустое сообщение, будет 6.",
        )
        return True

    if flow == "register_threshold":
        try:
            threshold = int(text.strip() or "6")
        except ValueError:
            threshold = 6
        threshold = max(1, min(10, threshold))
        try:
            payload = await call_backend(
                CLIENT.telegram_register,
                telegram_id=telegram_id,
                username=telegram_username,
                chat_id=chat_id,
                login=draft.get("login", ""),
                email=draft.get("email", ""),
                password=draft.get("password", ""),
                interests=draft.get("interests", []),
                threshold=threshold,
            )
            store_session(context, payload)
            clear_flow(context)
            await send_long_message(
                update,
                "Регистрация завершена. Аккаунт создан и сразу привязан к Telegram.\n\n"
                "Теперь можно писать ИИ обычными сообщениями.",
                reply_markup=main_keyboard(True),
            )
        except BackendAPIError as exc:
            clear_flow(context)
            await send_long_message(
                update,
                f"Не удалось зарегистрироваться: {escape(exc)}",
                reply_markup=main_keyboard(False),
            )
        return True

    if flow == "update_interests":
        interests = parse_interests(text)
        if not interests:
            clear_flow(context)
            await send_long_message(
                update,
                "Не удалось распознать интересы. Откройте пункт Интересы и попробуйте еще раз.",
                reply_markup=main_keyboard(True),
            )
            return True

        try:
            payload = await call_backend(
                CLIENT.update_interests,
                telegram_id,
                interests,
                current_threshold(context),
            )
            store_session(context, {"user": payload.get("user")})
            clear_flow(context)
            await send_long_message(
                update,
                f"Интересы обновлены: {escape(', '.join(payload.get('user', {}).get('interests') or []))}",
                reply_markup=main_keyboard(True),
            )
        except BackendAPIError as exc:
            clear_flow(context)
            await send_long_message(
                update,
                f"Не удалось обновить интересы: {escape(exc)}",
                reply_markup=main_keyboard(True),
            )
        return True

    clear_flow(context)
    return False


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_user:
        return

    text = (update.effective_message.text or "").strip()
    if not text:
        return

    if await handle_flow(update, context, text):
        return

    if text == BTN_LOGIN:
        await begin_login(update, context)
        return
    if text == BTN_REGISTER:
        await begin_register(update, context)
        return
    if text == BTN_NEWS:
        await show_news(update, context)
        return
    if text == BTN_DIGEST:
        await show_digest(update, context)
        return
    if text == BTN_PROFILE:
        await show_profile(update, context)
        return
    if text == BTN_INTERESTS:
        await begin_interests(update, context)
        return
    if text == BTN_AI:
        await explain_ai_chat(update, context)
        return
    if text in {BTN_HELP, BTN_MENU}:
        await help_command(update, context)
        return

    user = await ensure_linked_user(update, context)
    if not user:
        return

    try:
        payload = await call_backend(CLIENT.assistant_chat, update.effective_user.id, text)
        await send_long_message(update, assistant_text(payload), reply_markup=main_keyboard(True))
    except BackendAPIError as exc:
        await send_long_message(
            update,
            f"Ошибка ответа ИИ: {escape(exc)}",
            reply_markup=main_keyboard(True),
        )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/", CONFIG.health_path}:
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"status":"not_found"}')
            return

        state = runtime_state_snapshot()
        payload = {
            "status": "ok",
            "service": "telegram-bot",
            "bot_name": CONFIG.bot_name,
            "backend_api_url": CLIENT.base_url,
            "configured": state.get("configured", False),
            "bot_started": state.get("bot_started", False),
            "last_error": state.get("last_error", ""),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args):
        LOGGER.debug("Health server | " + fmt, *args)


def start_health_server() -> ThreadingHTTPServer | None:
    try:
        server = ThreadingHTTPServer((CONFIG.health_host, CONFIG.port), HealthHandler)
    except OSError as exc:
        LOGGER.warning("Health server did not start on %s:%s: %s", CONFIG.health_host, CONFIG.port, exc)
        return None

    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    LOGGER.info("Health server listening on http://%s:%s%s", CONFIG.health_host, CONFIG.port, CONFIG.health_path)
    return server


async def on_startup(application: Application):
    del application
    try:
        await call_backend(CLIENT.health)
        LOGGER.info("Backend healthcheck passed for %s", CONFIG.backend_api_url)
    except Exception as exc:
        LOGGER.warning("Backend healthcheck failed: %s", exc)
    set_runtime_state(bot_started=True, last_error="")


def idle_forever() -> None:
    while True:
        time.sleep(3600)


def main():
    start_health_server()
    if not CONFIG.token:
        message = "Telegram bot token is missing. Set TELEGRAM_BOT_TOKEN in Railway Variables."
        set_runtime_state(configured=False, bot_started=False, last_error=message)
        LOGGER.error(message)
        idle_forever()

    set_runtime_state(configured=True, bot_started=False, last_error="")

    while True:
        try:
            application = Application.builder().token(CONFIG.token).post_init(on_startup).build()
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("help", help_command))
            application.add_handler(CommandHandler("menu", start))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

            LOGGER.info("Starting Telegram bot")
            application.run_polling(allowed_updates=Update.ALL_TYPES)
            set_runtime_state(bot_started=False, last_error="Bot polling stopped.")
            LOGGER.warning("Bot polling stopped. Waiting before restart.")
        except Exception as exc:
            set_runtime_state(bot_started=False, last_error=str(exc))
            LOGGER.exception("Telegram bot crashed during startup or polling")

        time.sleep(CONFIG.retry_seconds)


if __name__ == "__main__":
    main()
