"""Telegram-бот, который повторяет текстовые сообщения."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Привет! Я Shrimp Bot 🦐 Отправь мне текст — я повторю его."
        )


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message and message.text:
        await message.reply_text(message.text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Не выводим URL запросов: они могут содержать токен бота.
    logger.error("Ошибка обработки Telegram: %s", type(context.error).__name__)


def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Добавьте BOT_TOKEN в shrimp-bot/.env или в переменные окружения.")

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    application.add_error_handler(on_error)

    logger.info("Запускаю Shrimp Bot. Для остановки нажмите Ctrl+C.")
    application.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
