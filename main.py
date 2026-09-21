import os
import asyncio
import random
import logging
from io import BytesIO
from datetime import datetime, timedelta

import cv2
import numpy as np
import pytesseract
from PIL import Image
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, AuthKeyUnregisteredError
from telethon.sessions import StringSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import zoneinfo

# =========================================================
#                     НАСТРОЙКИ
# =========================================================
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TARGET_BOT = os.environ["TARGET_BOT"]
BONUS_TEXT = os.environ.get("BONUS_TEXT", "🎁Бонус")

# Самара = UTC+4
BONUS_TZ = zoneinfo.ZoneInfo("Europe/Samara")

# 🧪 ТЕСТ: 1 = отправить бонус один раз через 2 минуты после старта
TEST_MODE = os.environ.get("TEST_MODE", "0") == "1"

# =========================================================
#                        ЛОГИ
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("captcha-bot")

# =========================================================
#                         OCR
# =========================================================
WHITELIST = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
TESS_CONFIG = f"--psm 7 -c tessedit_char_whitelist={WHITELIST}"


def solve_captcha(image_bytes: bytes) -> str:
    """Распознаёт капчу, убирая фон и линии."""
    img = np.array(Image.open(BytesIO(image_bytes)).convert("RGB"))
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 180]), np.array([180, 60, 255]))
    inv = cv2.bitwise_not(mask)

    blur = cv2.GaussianBlur(inv, (3, 3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8), iterations=1)

    text = pytesseract.image_to_string(cleaned, config=TESS_CONFIG)
    return text.strip().replace(" ", "").replace("\n", "")


# =========================================================
#                  ЗАГРУЗКА СЕССИЙ (1..15)
# =========================================================
SESSIONS = []
for i in range(1, 16):
    val = os.environ.get(f"SESSION_STR_{i}")
    if val and val.strip():
        SESSIONS.append((i, val.strip()))

# fallback: одна SESSION_STR без номера
if not SESSIONS and os.environ.get("SESSION_STR"):
    SESSIONS.append((1, os.environ["SESSION_STR"].strip()))

if not SESSIONS:
    log.error("❌ Нет сессий. Добавь SESSION_STR_1, SESSION_STR_2...")
    raise SystemExit(1)

log.info(f"🔑 Загружено сессий: {len(SESSIONS)}")


# =========================================================
#                       КЛИЕНТЫ
# =========================================================
clients = [
    (idx, TelegramClient(StringSession(sess), API_ID, API_HASH))
    for idx, sess in SESSIONS
]


# =========================================================
#                ЧЕЛОВЕЧЕСКИЕ ПАУЗЫ
# =========================================================
async def human_pause(min_s: float, max_s: float):
    await asyncio.sleep(random.uniform(min_s, max_s))


# =========================================================
#                ОТПРАВКА БОНУСА
# =========================================================
async def send_bonus_one(idx: int, c: TelegramClient):
    try:
        # Пауза — как будто человек открыл чат
        await human_pause(2.0, 8.0)

        await c.send_message(TARGET_BOT, BONUS_TEXT)
        log.info(f"📨 [акк {idx}] Отправлено '{BONUS_TEXT}'")

    except FloodWaitError as e:
        log.warning(f"⏳ [акк {idx}] FloodWait {e.seconds} сек")
        await asyncio.sleep(e.seconds)
        try:
            await c.send_message(TARGET_BOT, BONUS_TEXT)
            log.info(f"📨 [акк {idx}] Отправлено со 2-й попытки")
        except Exception as e2:
            log.error(f"❌ [акк {idx}] {e2}")
    except AuthKeyUnregisteredError:
        log.error(f"🚫 [акк {idx}] Сессия отозвана — обнови SESSION_STR_{idx}")
    except Exception as e:
        log.exception(f"❌ [акк {idx}] Ошибка: {e}")


# =========================================================
#                  РАСПИСАНИЕ
# =========================================================
scheduler = AsyncIOScheduler()


def parse_schedule(raw: str):
    """'09:00,17:00' → [(9,0),(17,0)]"""
    result = []
    if not raw:
        return result
    for item in raw.split(","):
        try:
            h, m = item.strip().split(":")
            result.append((int(h), int(m)))
        except Exception:
            log.warning(f"⚠️ Неверный формат времени: {item!r}")
    return result


def schedule_bonus():
    if TEST_MODE:
        # 🧪 ТЕСТ: каждый аккаунт отправляет один раз через 2 минуты (+0.3с между аккаунтами)
        log.info("🧪 TEST_MODE: рассылка один раз через 2 минуты")
        for i, (idx, c) in enumerate(clients):
            delay_sec = 120 + i * 0.3
            run_at = datetime.now(BONUS_TZ) + timedelta(seconds=delay_sec)
            scheduler.add_job(
                send_bonus_one,
                trigger="date",
                run_date=run_at,
                args=[idx, c],
                id=f"test_bonus_{idx}",
                replace_existing=True,
            )
            log.info(f"🧪 [акк {idx}] тест → {run_at.strftime('%H:%M:%S')} (Самара)")
        return

    # Продакшн: слоты из SCHEDULE_N
    log.info("⏰ Планирование задач")
    for idx, c in clients:
        raw = os.environ.get(f"SCHEDULE_{idx}", "07:07")
        slots = parse_schedule(raw)

        for h, m in slots:
            scheduler.add_job(
                send_bonus_one,
                trigger="cron",
                hour=h,
                minute=m,
                timezone=BONUS_TZ,
                args=[idx, c],
                id=f"bonus_{idx}_{h:02d}{m:02d}",
                replace_existing=True,
            )
            log.info(f"✅ [акк {idx}] → {h:02d}:{m:02d} (Самара)")


# =========================================================
#              ОБРАБОТЧИК КАПЧИ
# =========================================================
def make_handler(idx: int):
    async def handler(event):
        msg = event.message
        try:
            if msg.photo:
                log.info(f"📩 [акк {idx}] Капча получена")

                # Пауза — человек смотрит на картинку
                await human_pause(2.5, 6.5)

                image_bytes = await msg.download_media(bytes)
                code = solve_captcha(image_bytes)
                log.info(f"🔍 [акк {idx}] Распознан: {code!r}")

                if len(code) < 4:
                    log.warning(f"⚠️ [акк {idx}] Короткий код — пропуск")
                    return

                # Пауза — человек вводит код руками
                await human_pause(1.5, 4.0)

                await event.reply(code)
                log.info(f"📤 [акк {idx}] Отправлен: {code}")

                await human_pause(0.5, 1.5)

            elif msg.text:
                log.info(f"💬 [акк {idx}] Бот: {msg.text}")

        except Exception as e:
            log.error(f"❌ [акк {idx}] {e}")

    return handler


# =========================================================
#                      ЗАПУСК
# =========================================================
async def main():
    log.info("=" * 55)
    log.info(f"🚀 Запуск • Целевой бот: {TARGET_BOT} • Аккаунтов: {len(clients)}")
    log.info(f"🧪 TEST_MODE: {TEST_MODE}")
    log.info("=" * 55)

    # Регистрируем обработчики капчи
    for idx, c in clients:
        c.add_event_handler(make_handler(idx), events.NewMessage(from_users=TARGET_BOT))

    # Запускаем клиенты с паузой между ними
    started = []
    for i, (idx, c) in enumerate(clients):
        try:
            await c.start()
            me = await c.get_me()
            started.append((idx, c))
            log.info(f"🚀 [акк {idx}] Запущен как @{me.username or me.id}")

            if i < len(clients) - 1:
                await human_pause(1.5, 3.5)
        except AuthKeyUnregisteredError:
            log.error(f"🚫 [акк {idx}] Сессия недействительна")
        except Exception as e:
            log.exception(f"❌ [акк {idx}] Ошибка запуска: {e}")

    if not started:
        log.error("❌ Ни один аккаунт не запустился")
        return

    clients.clear()
    clients.extend(started)

    schedule_bonus()
    scheduler.start()

    log.info("✅ Всё готово")
    await asyncio.gather(*(c.run_until_disconnected() for _, c in clients))


if __name__ == "__main__":
    with clients[0][1]:
        clients[0][1].loop.run_until_complete(main())
