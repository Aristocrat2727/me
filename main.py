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
from telethon.sessions import StringSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import zoneinfo

# ===== НАСТРОЙКИ ИЗ RAILWAY VARIABLES =====
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STR = os.environ["SESSION_STR"]
TARGET_BOT = os.environ["TARGET_BOT"]          # например @bonus_bot
BONUS_TEXT = os.environ.get("BONUS_TEXT", "🎁Бонус")
BONUS_HOUR = int(os.environ.get("BONUS_HOUR", 7))
BONUS_MINUTE = int(os.environ.get("BONUS_MINUTE", 7))
BONUS_TZ = zoneinfo.ZoneInfo("Europe/Samara")  # Самара = UTC+4
DELAY_MIN = float(os.environ.get("DELAY_MIN", 3))
DELAY_MAX = float(os.environ.get("DELAY_MAX", 8))

# 🧪 ТЕСТОВЫЙ РЕЖИМ
# TEST_MODE = "1"  → бонус отправится ОДИН РАЗ через 2 минуты после старта
# TEST_MODE = "0"  → обычный режим: каждый день в 07:07 по Самаре
TEST_MODE = os.environ.get("TEST_MODE", "0") == "1"

# ===== ЛОГИ =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("captcha-bot")

# ===== OCR =====
WHITELIST = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
TESS_CONFIG = f"--psm 7 -c tessedit_char_whitelist={WHITELIST}"


def solve_captcha(image_bytes: bytes) -> str:
    """Чистит фон и распознаёт капчу."""
    img = np.array(Image.open(BytesIO(image_bytes)).convert("RGB"))
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

    # 1. Оставляем только белые области (белый прямоугольник с текстом)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    lower_white = np.array([0, 0, 180])
    upper_white = np.array([180, 60, 255])
    mask = cv2.inRange(hsv, lower_white, upper_white)

    # 2. Инвертируем — текст становится белым на чёрном
    inv = cv2.bitwise_not(mask)

    # 3. Убираем линии/шум
    blur = cv2.GaussianBlur(inv, (3, 3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    kernel = np.ones((2, 2), np.uint8)
    cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)

    # 4. Распознаём
    text = pytesseract.image_to_string(cleaned, config=TESS_CONFIG)
    return text.strip().replace(" ", "").replace("\n", "")


# ===== КЛИЕНТ =====
client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
scheduler = AsyncIOScheduler()


# ===== ЗАДАЧА: отправить "🎁Бонус" =====
async def send_bonus():
    try:
        await client.send_message(TARGET_BOT, BONUS_TEXT)
        log.info(f"📨 Отправлено '{BONUS_TEXT}' в {TARGET_BOT}")
    except Exception as e:
        log.exception(f"❌ Ошибка отправки бонуса: {e}")


# ===== РАСПИСАНИЕ =====
def schedule_bonus():
    if TEST_MODE:
        # 🧪 ТЕСТ: один раз через 2 минуты после старта
        run_at = datetime.now(BONUS_TZ) + timedelta(minutes=2)
        scheduler.add_job(
            send_bonus,
            trigger="date",
            run_date=run_at,
            id="test_bonus",
            replace_existing=True,
        )
        log.info(f"🧪 ТЕСТ-РЕЖИМ: бонус отправится в {run_at.strftime('%H:%M:%S')} (Самара)")
    else:
        # ⏰ Продакшн: каждый день в 07:07 по Самаре
        scheduler.add_job(
            send_bonus,
            trigger="cron",
            hour=BONUS_HOUR,
            minute=BONUS_MINUTE,
            timezone=BONUS_TZ,
            id="daily_bonus",
            replace_existing=True,
        )
        log.info(f"⏰ Продакшн: бонус каждый день в {BONUS_HOUR:02d}:{BONUS_MINUTE:02d} (Самара)")


# ===== ОБРАБОТКА КАПЧИ =====
@client.on(events.NewMessage(from_users=TARGET_BOT))
async def handler(event):
    msg = event.message

    # --- Капча (картинка) ---
    if msg.photo:
        log.info("📩 Капча получена")
        try:
            image_bytes = await msg.download_media(bytes)
            code = solve_captcha(image_bytes)
            log.info(f"🔍 Распознан код: {code!r}")

            if len(code) < 4:
                log.warning("⚠️ Код слишком короткий — пропуск")
                return

            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            log.info(f"⏳ Ждём {delay:.1f} сек (имитация человека)...")
            await asyncio.sleep(delay)

            await event.reply(code)
            log.info(f"📤 Отправлен код: {code}")

        except Exception as e:
            log.exception(f"❌ Ошибка обработки капчи: {e}")

    # --- Текстовые сообщения от бота (для логов) ---
    elif msg.text:
        log.info(f"💬 Бот: {msg.text}")

    # --- Логируем кнопки, если есть ---
    if msg.buttons:
        for row_i, row in enumerate(msg.buttons):
            for btn_i, btn in enumerate(row):
                log.info(f"🔘 Кнопка [{row_i}][{btn_i}]: {btn.text!r}")


# ===== ЗАПУСК =====
async def main():
    await client.start()
    me = await client.get_me()
    log.info(f"🚀 Userbot запущен как @{me.username or me.id}")
    log.info(f"🎯 Целевой бот: {TARGET_BOT}")

    schedule_bonus()
    scheduler.start()

    await client.run_until_disconnected()


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
