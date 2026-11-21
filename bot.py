# -*- coding: utf-8 -*-
"""
Telegram-бот «Системный отчёт» (версия 2).

Как это работает:
  1. Человек пишет боту /start и видит кнопку «Системный отчёт».
  2. Кнопка открывает мини-приложение (WebApp) — страницу index.html.
  3. Страница собирает данные об устройстве и после нажатия «Отправить» отдаёт их боту.
  4. Бот оформляет данные в отчёт (Markdown) и присылает в чат.

Бот работает через polling — вебхуки и SSL не нужны.
Секреты (токен, ссылка) берутся из переменных окружения Render — в коде их нет.
"""
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

# Меняйте это число при каждом обновлении index.html ("2" -> "3" -> "4"...).
# Бот добавит его к ссылке (?v=2), и Telegram не сможет взять старую страницу из кэша.
CACHE_BUSTER = "2"

NO_DATA = "Скрыто системой"
LINE = "━━━━━━━━━━━━━━━━━━"

# Что Telegram пишет в WebApp.platform -> как показать человеку
PLATFORMS = {
    "android": ("Телефон", "Android"),
    "android_x": ("Телефон", "Android"),
    "ios": ("Телефон", "iOS"),
    "tdesktop": ("ПК", "Telegram Desktop"),
    "macos": ("ПК", "macOS"),
    "weba": ("Браузер", "Telegram Web A"),
    "webk": ("Браузер", "Telegram Web K"),
    "web": ("Браузер", "Telegram Web"),
}


# ---------------------------------------------------------------------------
#  Мелкие помощники
# ---------------------------------------------------------------------------
def clean(value, limit=100):
    """Возвращает аккуратную строку или None, если значения нет."""
    try:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in ("null", "none", "undefined", "unknown", "nan"):
            return None
        return text[:limit]
    except Exception:
        return None


def to_float(value):
    """Число или None (NaN, бесконечность и мусор отбрасываются)."""
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


_MD_SPECIAL = "_*[]()~`>#+-=|{}.!\\"


def md(text):
    """Экранирует текст для MarkdownV2, чтобы Telegram не ругался на символы."""
    return "".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in str(text))


# ---------------------------------------------------------------------------
#  Форматирование каждого параметра. Каждая функция вернёт текст или None.
#  Если внутри что-то сломается — безопасная обёртка safe_field() покажет
#  «Скрыто системой», а остальные строки отчёта не пострадают.
# ---------------------------------------------------------------------------
def fmt_os(d):
    name = clean(d.get("os"))
    if not name:
        return None
    version = clean(d.get("osVersion"))
    return f"{name} {version}" if version else name


def fmt_arch(d):
    bits = to_float(d.get("archBits"))
    cpu = clean(d.get("archCpu"), 20)
    has_bits = bits in (32.0, 64.0)
    if has_bits and cpu:
        return f"{int(bits)}-bit ({cpu})"
    if has_bits:
        return f"{int(bits)}-bit"
    return cpu


def fmt_platform(d):
    raw = clean(d.get("tgPlatform"), 30)
    if not raw:
        return None
    known = PLATFORMS.get(raw.lower())
    return f"{known[0]} ({known[1]})" if known else raw


def fmt_browser(d):
    name = clean(d.get("browser"))
    if not name:
        return None
    version = clean(d.get("browserVersion"), 20)
    return f"{name} {version}" if version else name


def fmt_tg_version(d):
    return clean(d.get("tgVersion"), 20)


def fmt_timezone(d):
    zone = clean(d.get("timezone"), 60)
    offset = to_float(d.get("tzOffset"))
    offset_text = None
    if offset is not None and abs(offset) <= 14 * 60:
        total = int(offset)
        sign = "+" if total >= 0 else "-"
        hours, minutes = divmod(abs(total), 60)
        offset_text = f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")
    if zone and offset_text:
        return f"{zone} ({offset_text})"
    return zone or offset_text


def fmt_cores(d):
    cores = to_float(d.get("cores"))
    if cores and 0 < cores <= 1024:
        return str(int(cores))
    return None


def fmt_ram(d):
    ram = to_float(d.get("ram"))
    if not ram or ram <= 0:
        return None
    if ram >= 8:
        return "8 ГБ или больше (браузер не показывает точнее)"
    return f"≈ {ram:g} ГБ (округлено браузером)"


def fmt_screen(d):
    screen = clean(d.get("screen"))
    if not screen:
        return None
    extra = []
    ratio = to_float(d.get("pixelRatio"))
    physical = clean(d.get("physical"))
    if ratio and ratio > 0:
        extra.append(f"плотность ×{round(ratio, 2):g}")
    if physical:
        extra.append(f"≈ {physical} физ. пикселей")
    return screen + (f" ({', '.join(extra)})" if extra else "")


def fmt_refresh(d):
    hz = to_float(d.get("refreshRate"))
    if hz and 10 <= hz <= 500:
        return f"{int(round(hz))} Гц"
    return None


def safe_field(func, data):
    """Запускает форматирование поля так, чтобы ошибка не ломала весь отчёт."""
    try:
        value = func(data)
        return value if value else NO_DATA
    except Exception:
        log.warning("Не удалось обработать поле %s", getattr(func, "__name__", "?"), exc_info=True)
        return NO_DATA


# ---------------------------------------------------------------------------
#  Сборка отчёта
# ---------------------------------------------------------------------------
def build_report(data):
    """Возвращает (разделы, подвал). Раздел = (заголовок, [(эмодзи, название, значение), ...])."""
    if not isinstance(data, dict):
        data = {}

    system = [
        ("🖥", "ОС", safe_field(fmt_os, data)),
        ("🧬", "Архитектура", safe_field(fmt_arch, data)),
        ("🧭", "Платформа", safe_field(fmt_platform, data)),
        ("🌐", "Браузер", safe_field(fmt_browser, data)),
        ("✈️", "Версия Telegram", safe_field(fmt_tg_version, data)),
        ("🕒", "Часовой пояс", safe_field(fmt_timezone, data)),
    ]
    hardware = [
        ("⚙️", "Ядра процессора", safe_field(fmt_cores, data)),
        ("🧠", "ОЗУ", safe_field(fmt_ram, data)),
        ("📐", "Экран", safe_field(fmt_screen, data)),
        ("🎞", "Частота экрана", safe_field(fmt_refresh, data)),
    ]
    sections = [("💻 Система", system), ("🔧 Железо", hardware)]

    version = clean(data.get("v"), 20)
    if version:
        footer = f"🧩 Версия WebApp: {version}"
    else:
        footer = "⚠️ Открыта старая версия страницы (кэш Telegram) — сбросьте кэш по инструкции"
    return sections, footer


def render_markdown(sections, footer):
    lines = ["*📊 СИСТЕМНЫЙ ОТЧЁТ*", LINE]
    for title, rows in sections:
        lines.append("")
        lines.append(f"*{md(title)}*")
        for emoji, label, value in rows:
            lines.append(f"{emoji} *{md(label)}:* {md(value)}")
    lines += ["", LINE, md(footer)]
    return "\n".join(lines)


def render_plain(sections, footer):
    lines = ["📊 СИСТЕМНЫЙ ОТЧЁТ", LINE]
    for title, rows in sections:
        lines.append("")
        lines.append(title)
        for emoji, label, value in rows:
            lines.append(f"{emoji} {label}: {value}")
    lines += ["", LINE, footer]
    return "\n".join(lines)


def send_safe(bot, chat_id, text, **kwargs):
    """Отправка сообщения, которая не роняет бота при ошибке."""
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception:
        log.exception("Не удалось отправить сообщение в чат %s", chat_id)
        return None


def send_report(bot, chat_id, sections, footer):
    """Сначала красивый Markdown; если Telegram его не принял — обычный текст."""
    try:
        bot.send_message(chat_id, render_markdown(sections, footer), parse_mode="MarkdownV2")
        return
    except Exception:
        log.exception("MarkdownV2 не прошёл, отправляю обычным текстом")
    send_safe(bot, chat_id, render_plain(sections, footer))


def webapp_url():
    """Ссылка на страницу + номер версии (обход кэша Telegram)."""
    separator = "&" if "?" in WEBAPP_URL else "?"
    return f"{WEBAPP_URL}{separator}v={CACHE_BUSTER}"


# ---------------------------------------------------------------------------
#  Сам бот
# ---------------------------------------------------------------------------
def create_bot():
    bot = telebot.TeleBot(BOT_TOKEN)

    @bot.message_handler(commands=["start", "help"])
    def cmd_start(message):
        keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add(
            types.KeyboardButton(
                "📊 Системный отчёт",
                web_app=types.WebAppInfo(url=webapp_url()),
            )
        )
        send_safe(
            bot,
            message.chat.id,
            "👋 Привет! Я составлю системный отчёт о вашем устройстве.\n\n"
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
            sections, footer = build_report(data)
        except Exception:
            log.exception("Ошибка при сборке отчёта")
            sections, footer = build_report({})

        send_report(bot, message.chat.id, sections, footer)

    @bot.message_handler(func=lambda message: True)
    def fallback(message):
        send_safe(bot, message.chat.id, "Нажмите /start, чтобы получить кнопку 👇")

    return bot


# ---------------------------------------------------------------------------
#  Мини-сервер «я жив» — нужен бесплатному Render и сайту-«будильнику»
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
        sys.exit("❌ Не задана переменная BOT_TOKEN (Render -> Environment).")
    if not WEBAPP_URL.startswith("https://"):
        sys.exit("❌ WEBAPP_URL должна начинаться с https:// (Render -> Environment).")


def main():
    check_settings()
    start_health_server()
    bot = create_bot()
    try:
        bot.remove_webhook()  # на случай, если раньше был включён вебхук
    except Exception:
        log.warning("Не удалось сбросить вебхук (это не страшно)", exc_info=True)
    log.info("✅ Бот запущен (polling), версия WebApp: %s", CACHE_BUSTER)
    bot.infinity_polling(timeout=20, long_polling_timeout=20)


if __name__ == "__main__":
    main()
