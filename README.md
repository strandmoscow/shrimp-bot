# Shrimp Bot 🦐

Эхо-бот на Python: `/start` выводит приветствие, обычные текстовые сообщения бот повторяет в том же чате. Другие команды и вложения игнорируются.

## Запуск

Нужен Python 3.10 или новее. На этом компьютере доступен `python3.11`.

```bash
cd /Users/andrejstrelcenko/Desktop/strand.moscow/shrimp-bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Впишите токен от BotFather в файл `.env`:

```dotenv
BOT_TOKEN=ваш_токен
```

Для нового клона сначала скопируйте `.env.example` в `.env`. Файл `.env` исключён из Git. Вместо файла можно задать переменную окружения `BOT_TOKEN`; она имеет приоритет.

```bash
python bot.py
```

Откройте бота в Telegram, отправьте `/start`, затем любой текст. Остановка — `Ctrl+C`.

Бот получает сообщения через long polling, поэтому публичный порт и webhook не нужны. Для одного токена запускайте только один экземпляр бота.

Используется [python-telegram-bot](https://docs.python-telegram-bot.org/en/stable/).
