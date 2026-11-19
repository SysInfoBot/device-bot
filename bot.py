# -*- coding: utf-8 -*-
"""
Telegram-бот «Данные устройства».

Как это работает:
  1. Человек пишет боту /start и видит кнопку «Узнать данные устройства».
  2. Кнопка открывает мини-приложение (WebApp) — страницу index.html.
  3. Страница собирает данные об устройстве и после нажатия «Отправить» отдаёт их боту.
  4. Бот оформляет данные и присылает их в чат.

Бот работает через polling (сам опрашивает Telegram) — вебхуки и SSL не нужны.
Секреты (токен, ссылка) берутся из переменных окружения — в коде их нет.
"""
import html
import json
import logging
import math
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import telebot
from telebot import types

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("device-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()

HIDDEN = "Скрыто системой"


# ---------------------------------------------------------------------------
#  Разбор данных от WebApp (всё обёрнуто так, чтобы бот НИКОГДА не падал)
# ---------------------------------------------------------------------------
def clean(value, limit=120):
    """Делает значение безопасным для Telegram. Если значения нет — вернёт None."""
    try:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in ("null", "none", "undefined", "unknown", "nan"):
            return None
        return html.escape(text[:limit])
    except Exception:
        return None


def to_number(value):
    """Превращает значение в положительное число. Если не вышло — None."""
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return None
        return number
    except Exception:
        return None


def build_report(data):
    """Собирает красивое сообщение. Если какого-то поля нет — пишет «Скрыто системой»."""
    if not isinstance(data, dict):
        data = {}

    # --- Операционная система ---
    os_name = clean(data.get("os"))
    os_ver = clean(data.get("osVersion"))
    if not os_name:
        os_line = HIDDEN
    else:
        os_line = f"{os_name} {os_ver}" if os_ver else os_name

    # --- Экран ---
    screen = clean(data.get("screen"))
    if not screen:
        screen_line = HIDDEN
    else:
        extra = []
        ratio = to_number(data.get("pixelRatio"))
        physical = clean(data.get("physical"))
        if ratio:
            extra.append(f"плотность ×{round(ratio, 2):g}")
        if physical:
            extra.append(f"≈ {physical} физ. пикселей")
        screen_line = screen + (f" ({', '.join(extra)})" if extra else "")

    # --- Ядра процессора ---
    cores = to_number(data.get("cores"))
    cores_line = str(int(cores)) if cores else HIDDEN

    # --- Оперативная память (iOS и Safari её не отдают) ---
    ram = to_number(data.get("ram"))
    if not ram:
        ram_line = HIDDEN
    elif ram >= 8:
        ram_line = "8 ГБ или больше (браузер не показывает точнее)"
    else:
        ram_line = f"≈ {ram:g} ГБ (округлено браузером)"

    # --- Приложение Telegram ---
    tg_platform = clean(data.get("tgPlatform"))
    tg_version = clean(data.get("tgVersion"))
    if not tg_platform:
        tg_line = HIDDEN
    else:
        tg_line = f"{tg_platform} {tg_version}" if tg_version else tg_platform

    return (
        "📊 <b>Данные вашего устройства</b>\n\n"
        f"🖥 <b>ОС:</b> {os_line}\n"
        f"📐 <b>Экран:</b> {screen_line}\n"
        f"⚙️ <b>Ядра процессора:</b> {cores_line}\n"
        f"🧠 <b>ОЗУ:</b> {ram_line}\n"
        f"✈️ <b>Telegram:</b> {tg_line}"
    )


def send_safe(bot, chat_id, text, **kwargs):
    """Отправка сообщения, которая не роняет бота при ошибке."""
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception:
        log.exception("Не удалось отправить сообщение в чат %s", chat_id)
        return None


# ---------------------------------------------------------------------------
#  Сам бот
# ---------------------------------------------------------------------------
def create_bot():
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

    @bot.message_handler(commands=["start", "help"])
    def cmd_start(message):
        keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add(
            types.KeyboardButton(
                "📱 Узнать данные устройства",
                web_app=types.WebAppInfo(url=WEBAPP_URL),
            )
        )
        send_safe(
            bot,
            message.chat.id,
            "👋 Привет! Я покажу, какие данные об устройстве видит Telegram: "
            "ОС, экран, ядра процессора и ОЗУ.\n\n"
            "Нажмите кнопку внизу 👇, посмотрите данные на экране "
            "и нажмите «Отправить в чат».",
            reply_markup=keyboard,
        )

    @bot.message_handler(content_types=["web_app_data"])
    def on_web_app_data(message):
        try:
            data = json.loads(message.web_app_data.data)
        except Exception:
            log.warning("Не удалось разобрать данные из WebApp", exc_info=True)
            data = {}

        try:
            text = build_report(data)
        except Exception:
            log.exception("Ошибка при сборке отчёта")
            text = build_report({})

        send_safe(bot, message.chat.id, text)

    @bot.message_handler(func=lambda message: True)
    def fallback(message):
        send_safe(bot, message.chat.id, "Нажмите /start, чтобы получить кнопку 👇")

    return bot


# ---------------------------------------------------------------------------
#  Мини-сервер «я жив» — нужен бесплатному хостингу Render и сайту-«будильнику»
# ---------------------------------------------------------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def _ok(self, with_body):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if with_body:
            self.wfile.write(b"OK")

    def do_GET(self):
        self._ok(True)

    def do_HEAD(self):
        self._ok(False)

    def log_message(self, *args):  # не засоряем логи
        pass


def start_health_server():
    port = os.environ.get("PORT")  # Render сам задаёт PORT для Web Service
    if not port:
        return
    try:
        server = HTTPServer(("0.0.0.0", int(port)), HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("Сервер «я жив» запущен на порту %s", port)
    except Exception:
        log.exception("Не получилось запустить сервер «я жив» (бот всё равно работает)")


def check_settings():
    if not BOT_TOKEN:
        sys.exit("❌ Не задана переменная BOT_TOKEN. Вставьте токен от @BotFather в настройках Render.")
    if not WEBAPP_URL.startswith("https://"):
        sys.exit("❌ WEBAPP_URL должна начинаться с https:// (ссылка на вашу страницу GitHub Pages).")


def main():
    check_settings()
    start_health_server()
    bot = create_bot()
    try:
        bot.remove_webhook()  # на случай, если раньше был включён вебхук
    except Exception:
        log.warning("Не удалось сбросить вебхук (это не страшно)", exc_info=True)
    log.info("✅ Бот запущен (polling)")
    bot.infinity_polling(timeout=20, long_polling_timeout=20)


if __name__ == "__main__":
    main()
