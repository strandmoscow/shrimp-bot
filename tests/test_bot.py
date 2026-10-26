import asyncio
import json
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from bot import BotUI, build_application, telegram_text
from integrations import BackupState, IntegrationError, Integrations, Service
from security import Tickets, authorized, redact
from settings import BackupTarget, Settings


class SecurityTests(unittest.TestCase):
    def test_long_emoji_logs_fit_telegram_limit(self):
        text = telegram_text("🦐" * 4000)
        self.assertLess(len(text.encode("utf-16-le")) // 2, 4096)

    def test_default_deny_and_chat_restriction(self):
        self.assertFalse(authorized(Settings(), 42, -100))
        s = Settings(allowed_users=frozenset({42}), allowed_chats=frozenset({-100}))
        self.assertTrue(authorized(s, 42, -100))
        self.assertFalse(authorized(s, 43, -100))
        self.assertFalse(authorized(s, 42, 42))

    def test_confirmation_is_bound_single_use_and_expiring(self):
        tickets = Tickets()
        with patch("security.time.monotonic", return_value=10):
            data = tickets.issue(42, -100, 7, "do_restart", "app", ttl=120)
        self.assertLess(len(data.encode()), 64)
        with patch("security.time.monotonic", return_value=20):
            self.assertIsNone(tickets.take(data, 43, -100, 7))
            self.assertIsNone(tickets.take(data, 42, -101, 7))
            self.assertIsNone(tickets.take(data, 42, -100, 8))
            self.assertEqual(tickets.take(data, 42, -100, 7).action, "do_restart")
            self.assertIsNone(tickets.take(data, 42, -100, 7))
            data = tickets.issue(42, -100, 7, "do_restart", "app", ttl=1)
        with patch("security.time.monotonic", return_value=22):
            self.assertIsNone(tickets.take(data, 42, -100, 7))

    def test_cancel_revokes_confirmation_from_same_message(self):
        tickets = Tickets()
        confirm = tickets.issue(42, -100, 7, "do_restart", "app")
        other = tickets.issue(42, -100, 8, "do_restart", "app")
        tickets.revoke_message(-100, 7)
        self.assertIsNone(tickets.take(confirm, 42, -100, 7))
        self.assertIsNotNone(tickets.take(other, 42, -100, 8))

    def test_redaction_covers_known_and_unknown_credentials(self):
        s = Settings(token="123456:abcdefghijklmno_1234567890", dokploy_key="dokploy-key", ha_token="ha-key")
        text = (f'{s.token} dokploy-key ha-key\n'
                'PASSWORD="long secret with spaces"\n'
                '{"api_key": "unknown-secret"}\n'
                'Authorization: Bearer abc.def\npostgres://alex:pass@host/db\n'
                '999999:abcdefghijklmnopqrstuvwxyz0123')
        result = redact(text, s)
        for secret in (s.token, "dokploy-key", "ha-key", "long secret", "unknown-secret", "abc.def", "alex:pass", "abcdefghijklmnopqrstuvwxyz0123"):
            self.assertNotIn(secret, result)

    def test_bad_configuration_is_rejected(self):
        for env in ({"SERVER_SSH_HOST": "host; touch /tmp/no"},
                    {"ALLOWED_USER_IDS": "-100123"},
                    {"BACKUP_TARGETS": '[{"name":"a","id":"b","kind":"shell"}]'},
                    {"HA_SCENES": "switch.front_door"},
                    {"HEALTH_URLS": '{"test":"file:///etc/passwd"}'}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env()


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.responses = {}

        def handler(request):
            self.requests.append(request)
            value = self.responses.get(request.url.path, [])
            if isinstance(value, httpx.Response):
                return value
            return httpx.Response(200, json=value)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.settings = Settings(dokploy_key="api-secret", ha_url="http://ha", ha_token="ha-secret")
        self.api = Integrations(self.settings, self.http)

    async def asyncTearDown(self):
        await self.http.aclose()

    async def test_ambiguous_names_fail_and_full_path_selects_correct_service(self):
        self.responses["/api/project.all"] = [
            {"name": name, "environments": [{"name": "production", "applications": [],
              "compose": [{"composeId": name, "name": "app", "composeStatus": "done"}]}]}
            for name in ("immich", "navidrome")
        ]
        with self.assertRaisesRegex(IntegrationError, "неоднозначно"):
            await self.api.find_service("app")
        self.assertEqual((await self.api.find_service("immich")).id, "immich")
        self.assertEqual((await self.api.find_service("navidrome/production/app")).id, "navidrome")
        request = self.requests[-1]
        self.assertEqual(request.headers["x-api-key"], "api-secret")

    async def test_error_response_does_not_disclose_api_body(self):
        self.responses["/api/project.all"] = httpx.Response(403, text="password=secret-value")
        with self.assertRaises(IntegrationError) as error:
            await self.api.services()
        self.assertIn("403", str(error.exception))
        self.assertNotIn("secret-value", str(error.exception))

    async def test_missing_key_does_not_contact_dokploy(self):
        self.api.settings = Settings()
        with self.assertRaisesRegex(IntegrationError, "DOKPLOY_API_KEY"):
            await self.api.services()
        self.assertFalse(self.requests)

    async def test_restart_and_deploy_use_distinct_endpoints(self):
        service = Service("app-id", "application", "bot", "p", "prod", "done")
        self.responses["/api/application.one"] = {"appName": "actual-app-name"}
        await self.api.service_action(service, "restart")
        request = self.requests[-1]
        self.assertEqual(request.url.path, "/api/application.reload")
        self.assertEqual(json.loads(request.content), {"applicationId": "app-id", "appName": "actual-app-name"})
        await self.api.service_action(service, "deploy")
        self.assertEqual(self.requests[-1].url.path, "/api/application.deploy")
        self.assertTrue(all(r.method == "POST" for r in self.requests if not r.url.path.endswith(".one")))

    async def test_compose_restart_reports_partial_failure(self):
        self.responses["/api/compose.start"] = httpx.Response(500)
        service = Service("compose-id", "compose", "app", "p", "prod", "done")
        with self.assertRaisesRegex(IntegrationError, "остановлен"):
            await self.api.service_action(service, "restart")
        self.assertEqual([r.url.path for r in self.requests], ["/api/compose.stop", "/api/compose.start"])

    async def test_compose_logs_follow_container_ids(self):
        self.responses["/api/compose.one"] = {"appName": "immich", "composeType": "docker-compose", "serverId": "remote"}
        self.responses["/api/docker.getContainersByAppNameMatch"] = [{"containerId": "container-1", "name": "immich-server"}]
        self.responses["/api/compose.readLogs"] = "healthy"
        service = Service("compose-id", "compose", "app", "immich", "production", "done")
        self.assertIn("healthy", await self.api.logs(service))
        self.assertEqual(self.requests[-1].url.params["containerId"], "container-1")
        self.assertEqual(self.requests[-2].url.params["serverId"], "remote")

    async def test_backup_uses_last_success_not_recent_failure(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=40)).isoformat()
        self.responses["/api/deployment.allByType"] = [
            {"status": "error", "createdAt": now.isoformat(), "finishedAt": now.isoformat()},
            {"status": "done", "createdAt": old, "finishedAt": old},
        ]
        state = await self.api.backup_state(BackupTarget("db", "backup-id", max_age_hours=26))
        self.assertTrue(state.stale)
        self.assertEqual(state.latest_status, "error")
        self.assertEqual(state.last_success, datetime.fromisoformat(old))

    async def test_success_without_finished_time_is_not_considered_fresh(self):
        self.responses["/api/deployment.allByType"] = [{"status": "done", "createdAt": datetime.now(timezone.utc).isoformat()}]
        self.assertTrue((await self.api.backup_state(BackupTarget("db", "id"))).stale)

    async def test_manual_backups_only_allow_configured_targets(self):
        target = BackupTarget("db", "backup-id")
        with self.assertRaises(IntegrationError):
            await self.api.run_backup(target)
        self.assertFalse(self.requests)
        self.api.settings = replace(self.settings, backups=(target,))
        self.responses["/api/backup.one"] = {"backupType": "compose", "databaseType": "postgres"}
        await self.api.run_backup(target)
        self.assertEqual(self.requests[-1].url.path, "/api/backup.manualBackupCompose")

    async def test_home_actions_are_allowlisted(self):
        with self.assertRaises(IntegrationError):
            await self.api.ha_scene("script.open_door")
        self.assertFalse(self.requests)
        self.api.settings = replace(self.settings, ha_scenes=("scene.evening",))
        await self.api.ha_scene("scene.evening")
        request = self.requests[-1]
        self.assertEqual(request.url.path, "/api/services/scene/turn_on")
        self.assertEqual(request.headers["authorization"], "Bearer ha-secret")
        self.assertNotIn("x-api-key", request.headers)
        self.assertEqual(json.loads(request.content), {"entity_id": "scene.evening"})


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def update(self, user=42, chat=-100, data=None):
        message = SimpleNamespace(chat=SimpleNamespace(id=chat), message_id=7)
        query = SimpleNamespace(data=data, message=message, from_user=SimpleNamespace(id=user),
                                answer=AsyncMock(), edit_message_text=AsyncMock(),
                                edit_message_reply_markup=AsyncMock())
        return SimpleNamespace(effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(id=chat),
                               callback_query=query)

    def ui(self):
        settings = Settings(allowed_users=frozenset({42}), allowed_chats=frozenset({-100}))
        ui = BotUI(settings, SimpleNamespace(service_action=AsyncMock()))
        ui.show = AsyncMock()
        return ui

    async def test_unauthorized_commands_never_call_integration(self):
        ui, handler = self.ui(), AsyncMock()
        await ui.protect(handler)(self.update(user=43), None)
        handler.assert_not_awaited()

    async def test_confirmation_executes_once_and_wrong_user_cannot_consume_it(self):
        ui = self.ui()
        target = Service("id", "application", "bot", "p", "prod", "done")
        data = ui.tickets.issue(42, -100, 7, "do_restart", target)
        await ui.callback(self.update(user=43, data=data), None)
        ui.api.service_action.assert_not_awaited()
        await ui.callback(self.update(data=data), None)
        await ui.callback(self.update(data=data), None)
        ui.api.service_action.assert_awaited_once_with(target, "restart")

    async def test_request_for_confirmation_has_no_side_effect(self):
        ui = self.ui()
        data = ui.tickets.issue(42, -100, 7, "ask_restart", "target")
        await ui.callback(self.update(data=data), None)
        ui.api.service_action.assert_not_awaited()
        self.assertIn("Подтверждение", ui.show.await_args.args[1])

    async def test_backup_alerts_only_on_change_and_recovery(self):
        ui = self.ui()
        target = BackupTarget("db", "id")
        ui.settings = replace(ui.settings, backups=(target,), alert_chat=-100)
        state = BackupState(target, None, "never", True)
        ui.api.backup_state = AsyncMock(return_value=state)
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await ui.backup_alerts(context)
        await ui.backup_alerts(context)
        self.assertEqual(context.bot.send_message.await_count, 1)
        ui.api.backup_state.return_value = BackupState(target, datetime.now(timezone.utc), "done", False)
        await ui.backup_alerts(context)
        await ui.backup_alerts(context)
        self.assertEqual(context.bot.send_message.await_count, 2)

    async def test_application_registers_commands_without_network(self):
        app = build_application(Settings(token="123456:dummy-token"))
        try:
            registered = set().union(*(getattr(h, "commands", set()) for h in app.handlers[0]))
            self.assertTrue({"status", "apps", "logs", "vpn", "backups", "minecraft", "home", "whoami"} <= registered)
            self.assertIsNotNone(app.job_queue)
        finally:
            await app.bot_data["ui"].api.http.aclose()


if __name__ == "__main__":
    unittest.main()
