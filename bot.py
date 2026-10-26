"""Shrimp Bot: a Telegram control panel for the configured server."""

import asyncio
import logging
import tempfile
from functools import wraps
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from integrations import BackupState, IntegrationError, Integrations, Service
from security import Tickets, authorized, redact
from settings import ROOT, Settings
from music import MAX_MP3_BYTES, publish_mp3, upload_name

logger = logging.getLogger(__name__)

COMMANDS = [
    ("status", "Состояние сервера и приложений"),
    ("apps", "Приложения, логи и управление"),
    ("logs", "Логи: /logs immich"),
    ("vpn", "Проверка туннеля и Telegram"),
    ("backups", "Свежесть бэкапов и ручной запуск"),
    ("minecraft", "Статус Minecraft и игроки"),
    ("home", "Датчики и домашние сценарии"),
    ("whoami", "Ваш Telegram ID и ID чата"),
    ("help", "Справка"),
]


def gib(value: float) -> str:
    return f"{value / 1024**3:.1f} ГиБ"


def telegram_text(text: str) -> str:
    """Leave room for emoji, which use two UTF-16 code units in Telegram."""
    encoded = text.encode("utf-16-le", errors="replace")
    if len(encoded) > 7400:
        return "… [начало сокращено]\n" + encoded[-7200:].decode("utf-16-le", errors="ignore")
    return text


def backup_line(state: BackupState) -> str:
    date = state.last_success.strftime("%d.%m.%Y %H:%M UTC") if state.last_success else "нет успешного запуска с известным временем"
    icon = "⚠️" if state.stale or state.latest_status == "error" else "✅"
    return (f"{icon} {state.target.name}: {date}\n"
            f"Последний запуск: {state.latest_status}; допустимый возраст: {state.target.max_age_hours:g} ч.")


class BotUI:
    def __init__(self, settings: Settings, integrations: Integrations):
        self.settings, self.api = settings, integrations
        self.tickets = Tickets()
        self.mutation_lock = asyncio.Lock()
        self.music_slots = asyncio.Semaphore(1)
        self.alert_states: dict[str, tuple] = {}

    def permitted(self, update: Update) -> bool:
        return bool(update.effective_user and update.effective_chat and authorized(
            self.settings, update.effective_user.id, update.effective_chat.id
        ))

    def protect(self, handler):
        @wraps(handler)
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not self.permitted(update):
                await self.show(update, "Нет доступа. Узнать свой ID: /whoami. Владелец должен добавить его в ALLOWED_USER_IDS.")
                return
            try:
                await handler(update, context)
            except IntegrationError as exc:
                await self.show(update, str(exc))
        return wrapped

    async def show(self, update: Update, text: str, rows=None, *, ttl: float = 600):
        if not update.effective_message:
            return
        safe = telegram_text(redact(text, self.settings))
        message = await update.effective_message.reply_text(safe, parse_mode=None)
        if rows:
            keyboard = []
            for row in rows:
                buttons = []
                for label, action, payload in row:
                    data = self.tickets.issue(update.effective_user.id, message.chat_id,
                                              message.message_id, action, payload, ttl)
                    buttons.append(InlineKeyboardButton(label, callback_data=data))
                keyboard.append(buttons)
            await message.edit_reply_markup(InlineKeyboardMarkup(keyboard))

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = "Shrimp Bot 🦐\n\n" + "\n".join(f"/{name} — {label}" for name, label in COMMANDS)
        text += ("\n\nПерезапуск и деплой доступны кнопками в /apps."
                 "\n/backups run имя — создать бэкап."
                 "\n/home run scene.имя — запустить разрешённый сценарий."
                 "\nПришлите MP3 до 20 МБ — добавлю его в Navidrome."
                 "\nКоманды сервера доступны только разрешённым пользователям.")
        await self.show(update, text)

    async def import_music(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.effective_message
        if not message:
            return
        media = message.audio or message.document
        if not media:
            return
        if not self.settings.navidrome_import_dir:
            raise IntegrationError("Загрузка музыки не настроена: задайте NAVIDROME_IMPORT_DIR и подключите папку Navidrome.")
        filename = upload_name(media.file_name, media.mime_type, getattr(media, "title", None))
        if not media.file_size or not 0 < media.file_size <= MAX_MP3_BYTES:
            raise IntegrationError("Telegram позволяет боту скачивать файлы до 20 МБ. Пришлите MP3 меньшего размера.")
        directory = Path(self.settings.navidrome_import_dir)
        if not directory.is_dir():
            raise IntegrationError("Папка загрузки Navidrome недоступна. Проверьте mount и NAVIDROME_IMPORT_DIR.")
        if self.music_slots.locked():
            await self.show(update, "Сейчас загружается другой трек. Дождитесь результата и пришлите MP3 ещё раз.")
            return
        async with self.music_slots:
            await self.show(update, f"🦐 Загружаю в музыкальную библиотеку: {filename}")
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=directory, prefix=".shrimp-", suffix=".part", delete=False) as stream:
                    temporary = Path(stream.name)
                remote = await media.get_file(read_timeout=30, connect_timeout=10)
                await remote.download_to_drive(custom_path=temporary, read_timeout=90, connect_timeout=10)
                if temporary.stat().st_size != media.file_size:
                    raise IntegrationError("MP3 скачан не полностью. Попробуйте отправить файл ещё раз.")
                result = await asyncio.to_thread(publish_mp3, temporary, directory, filename)
            except TelegramError:
                raise IntegrationError("Не удалось скачать MP3 из Telegram. Проверьте VPN и размер файла, затем повторите отправку.") from None
            except OSError:
                raise IntegrationError("Не удалось записать MP3. Проверьте права и свободное место в папке Navidrome.") from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        if result.duplicate:
            await self.show(update, "Этот MP3 уже есть в папке загрузок Navidrome; повторная копия не создана.")
        else:
            await self.show(update, f"✅ MP3 сохранён: {filename}\nNavidrome добавит его в библиотеку при ближайшем сканировании.")

    async def whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        await self.show(update, f"Ваш user ID: {user.id if user else 'не определён'}\n"
                        f"Chat ID: {update.effective_chat.id}\n"
                        f"Thread ID: {update.effective_message.message_thread_id or 'нет'}")

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        metrics, services, health = await asyncio.gather(
            self.api.server_probe("status"), self.api.services(), self.api.health(), return_exceptions=True
        )
        parts = ["Состояние сервера"]
        if isinstance(metrics, Exception):
            parts.append(str(metrics) if isinstance(metrics, IntegrationError) else "Метрики хоста недоступны.")
        else:
            parts += [f"Хост: {metrics['hostname']}", f"CPU: {metrics['cpu_percent']}%",
                      f"RAM: {gib(metrics['ram_used'])} / {gib(metrics['ram_total'])}",
                      f"Диск: {gib(metrics['disk_used'])} / {gib(metrics['disk_total'])}; свободно {gib(metrics['disk_free'])}"]
        if isinstance(services, Exception):
            parts.append(str(services) if isinstance(services, IntegrationError) else "Dokploy недоступен.")
        else:
            parts.append(f"\nDokploy доступен. Сервисов: {len(services)}.")
            parts.append("Последние деплои (это не проверка доступности приложения):")
            parts += [f"• {s.project}/{s.name}: {s.status}" for s in services[:15]]
        if isinstance(health, list) and health:
            parts.append("\nHTTP-проверки:")
            parts.extend(health)
        elif not self.settings.health_urls:
            parts.append("\nДля HTTP-проверок приложений заполните HEALTH_URLS.")
        await self.show(update, "\n".join(parts))

    async def apps(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.apps_page(update, 0)

    async def apps_page(self, update: Update, page: int):
        services = sorted(await self.api.services(), key=lambda s: s.label.casefold())
        if not services:
            await self.show(update, "Приложений и Compose-проектов в Dokploy нет.")
            return
        last_page = (len(services) - 1) // 6
        page = max(0, min(page, last_page))
        text, rows = [f"Приложения · {page + 1}/{last_page + 1}"], []
        for s in services[page * 6:(page + 1) * 6]:
            text.append(f"\n{s.label}\nТип: {s.kind}; последний деплой: {s.status}")
            rows.append([(f"Логи · {s.project}/{s.name}"[:60], "logs", s)])
            rows.append([("Перезапустить", "ask_restart", s), ("Обновить из источника", "ask_deploy", s)])
        nav = []
        if page:
            nav.append(("← Назад", "apps", page - 1))
        if page < last_page:
            nav.append(("Далее →", "apps", page + 1))
        if nav:
            rows.append(nav)
        await self.show(update, "\n".join(text), rows)

    async def logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await self.show(update, "Использование: /logs immich или /logs проект/окружение/сервис.")
            return
        service = await self.api.find_service(" ".join(context.args))
        await self.logs_service(update, service)

    async def logs_service(self, update: Update, service: Service):
        logs = redact(await self.api.logs(service), self.settings)
        tail = logs[-3000:] if logs else "Логи пусты."
        await self.show(update, f"Логи {service.label}\n\n{tail}")

    async def vpn(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        lines = ["Проверка Telegram и VPN"]
        try:
            await context.bot.get_me()
            lines.append("✅ Telegram Bot API доступен из контейнера бота.")
        except Exception:
            lines.append("⚠️ Telegram Bot API не ответил из контейнера бота.")
        try:
            result = await self.api.server_probe("vpn")
            lines.append(("✅" if result["interface_present"] else "⚠️") +
                         f" Интерфейс {self.settings.vpn_interface}: " +
                         ("присутствует" if result["interface_present"] else "не найден"))
            lines.append(("✅" if result["telegram_ok"] else "⚠️") +
                         f" Telegram с хоста: {'доступен' if result['telegram_ok'] else 'недоступен'}; "
                         f"{result['latency_ms']} мс.")
        except IntegrationError as exc:
            lines.append(str(exc))
        lines.append("\nПроверка доступности и наличия интерфейса не доказывает маршрут каждого соединения.")
        await self.show(update, "\n".join(lines))

    async def backups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.settings.backups:
            await self.show(update, "Добавьте задания Dokploy в BACKUP_TARGETS. Пример есть в .env.example.")
            return
        if context.args:
            if len(context.args) < 2 or context.args[0] != "run":
                raise IntegrationError("Использование: /backups или /backups run имя.")
            name = " ".join(context.args[1:]).casefold()
            target = next((t for t in self.settings.backups if t.name.casefold() == name), None)
            if target is None:
                raise IntegrationError("Бэкап не найден в BACKUP_TARGETS.")
            await self.confirm(update, "backup", target)
            return
        parts, rows = ["Бэкапы · история заданий Dokploy"], []
        for target in self.settings.backups:
            try:
                parts.append(backup_line(await self.api.backup_state(target)))
            except IntegrationError as exc:
                parts.append(f"⚠️ {target.name}: {exc}")
            rows.append([(f"Создать · {target.name}"[:60], "ask_backup", target)])
        parts.append("История успешных заданий не заменяет проверку восстановления из хранилища.")
        await self.show(update, "\n\n".join(parts), rows)

    async def minecraft(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        result = await self.api.minecraft()
        await self.show(update, f"Minecraft 🟢\nИгроков: {result['online']} / {result['max']}\n"
                        f"Версия: {result['version']}\nЗадержка: {result['latency']} мс")

    async def home(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        s = self.settings
        if context.args:
            if len(context.args) != 2 or context.args[0] != "run" or context.args[1] not in s.ha_scenes:
                raise IntegrationError("Использование: /home run scene.имя. Сценарий должен быть добавлен в HA_SCENES.")
            await self.confirm(update, "scene", context.args[1])
            return
        await self.api.ha("")
        parts, rows = ["Home Assistant 🏠"], []
        if not s.ha_entities and not s.ha_scenes:
            parts.append("API доступен. Добавьте датчики в HA_ENTITIES и сценарии в HA_SCENES.")
        for entity in s.ha_entities:
            try:
                state = await self.api.ha_state(entity)
                attrs = state.get("attributes", {})
                parts.append(f"{attrs.get('friendly_name', entity)}: {state['state']} {attrs.get('unit_of_measurement', '')}".strip())
            except IntegrationError as exc:
                parts.append(f"{entity}: {exc}")
        for entity in s.ha_scenes:
            rows.append([(f"Запустить · {entity}"[:60], "ask_scene", entity)])
        await self.show(update, "\n".join(parts), rows)

    async def confirm(self, update: Update, action: str, payload):
        labels = {"restart": "Перезапустить сервис", "deploy": "Обновить сервис из источника",
                  "backup": "Запустить бэкап", "scene": "Запустить домашний сценарий"}
        name = payload.label if isinstance(payload, Service) else getattr(payload, "name", payload)
        text = f"{labels[action]}?\n{name}\n\nПодтверждение действует 2 минуты."
        if action == "restart":
            text += "\nВо время перезапуска сервис будет недоступен."
        await self.show(update, text, [[("Подтвердить", "do_" + action, payload),
                                       ("Отмена", "cancel", None)]], ttl=120)

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query or not query.message:
            return
        if not self.permitted(update):
            await query.answer("Нет доступа.", show_alert=True)
            return
        ticket = self.tickets.take(query.data or "", query.from_user.id,
                                   query.message.chat.id, query.message.message_id)
        if ticket is None:
            await query.answer("Кнопка устарела, уже использована или принадлежит другому пользователю.", show_alert=True)
            return
        await query.answer()
        try:
            action, payload = ticket.action, ticket.payload
            if action == "apps":
                await self.apps_page(update, payload)
            elif action == "logs":
                await self.logs_service(update, payload)
            elif action.startswith("ask_"):
                await self.confirm(update, action.removeprefix("ask_"), payload)
            elif action == "cancel":
                self.tickets.revoke_message(query.message.chat.id, query.message.message_id)
                await query.edit_message_text("Отменено.")
            elif action in {"do_restart", "do_deploy", "do_backup", "do_scene"}:
                self.tickets.revoke_message(query.message.chat.id, query.message.message_id)
                await query.edit_message_reply_markup(None)
                async with self.mutation_lock:
                    if action in {"do_restart", "do_deploy"}:
                        await self.api.service_action(payload, action.removeprefix("do_"))
                    elif action == "do_backup":
                        await self.api.run_backup(payload)
                    else:
                        await self.api.ha_scene(payload)
                logger.info("Action=%s user=%s chat=%s", action, query.from_user.id, query.message.chat.id)
                await self.show(update, "Запрос принят API. Результат проверьте через /apps, /backups или /home.")
        except IntegrationError as exc:
            await self.show(update, str(exc))

    async def backup_alerts(self, context: ContextTypes.DEFAULT_TYPE):
        for target in self.settings.backups:
            try:
                state = await self.api.backup_state(target)
                issue = state.stale or state.latest_status == "error"
                signature = (issue, state.latest_status if issue else "ok", state.last_success if issue else None)
                text = backup_line(state)
            except IntegrationError:
                issue, signature = True, (True, "api_error")
                text = f"⚠️ Не удалось проверить бэкап {target.name}. Проверьте Dokploy и API-ключ."
            previous = self.alert_states.get(target.name)
            if previous == signature:
                continue
            if issue or (previous and previous[0]):
                await context.bot.send_message(
                    chat_id=self.settings.alert_chat,
                    message_thread_id=self.settings.alert_thread,
                    text=redact(text, self.settings),
                )
            self.alert_states[target.name] = signature


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    # Exceptions and tracebacks can contain API URLs with tokens.
    logger.error("Handler failed: %s", type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Не удалось выполнить команду. Проверьте настройки интеграции и её доступность.")


def build_application(settings: Settings) -> Application:
    http = httpx.AsyncClient(timeout=15, limits=httpx.Limits(max_connections=8), follow_redirects=False)
    ui = BotUI(settings, Integrations(settings, http))

    async def startup(app: Application):
        await app.bot.set_my_commands([BotCommand(name, description) for name, description in COMMANDS])
        if settings.alert_chat and settings.backups:
            app.job_queue.run_repeating(ui.backup_alerts, interval=settings.backup_check_seconds, first=30)

    async def shutdown(app: Application):
        await http.aclose()

    app = (Application.builder().token(settings.token).concurrent_updates(8)
           .post_init(startup).post_shutdown(shutdown).build())
    app.bot_data["ui"] = ui
    app.add_handler(CommandHandler(["start", "help"], ui.help))
    app.add_handler(CommandHandler("whoami", ui.whoami))
    for name in ("status", "apps", "logs", "vpn", "backups", "minecraft", "home"):
        app.add_handler(CommandHandler(name, ui.protect(getattr(ui, name))))
    app.add_handler(CallbackQueryHandler(ui.callback, pattern=r"^ui:"))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & (filters.AUDIO | filters.Document.ALL),
                                   ui.protect(ui.import_music)))
    app.add_error_handler(on_error)
    return app


def main():
    load_dotenv(ROOT / ".env")
    try:
        settings = Settings.from_env()
    except (ValueError, TypeError, KeyError):
        raise SystemExit("Некорректные настройки .env. Проверьте числовые ID и JSON BACKUP_TARGETS по .env.example.") from None
    if not settings.token:
        raise SystemExit("Добавьте BOT_TOKEN в shrimp-bot/.env или в переменные окружения.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if not settings.allowed_users:
        logger.warning("ALLOWED_USER_IDS пуст: команды сервера закрыты. Доступны /whoami и /help.")
    app = build_application(settings)
    logger.info("Starting Shrimp Bot")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
