"""Bounded, asynchronous clients for the configured server integrations."""

import asyncio
import json
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from mcstatus import JavaServer

from settings import ROOT, BackupTarget, Settings


class IntegrationError(Exception):
    """A safe, user-facing error. Never include response bodies or credentials."""


@dataclass(frozen=True)
class Service:
    id: str
    kind: str
    name: str
    project: str
    environment: str
    status: str

    @property
    def label(self) -> str:
        return f"{self.project}/{self.environment}/{self.name}"


@dataclass(frozen=True)
class BackupState:
    target: BackupTarget
    last_success: datetime | None
    latest_status: str
    stale: bool


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
    except (ValueError, TypeError):
        return None


class Integrations:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings, self.http = settings, http

    async def request(self, method: str, url: str, **kwargs):
        try:
            response = await self.http.request(method, url, **kwargs)
        except httpx.HTTPError:
            message = "Сервис недоступен или не ответил вовремя."
            if method == "POST":
                message += " Запрос мог быть принят: проверьте результат перед повтором."
            raise IntegrationError(message) from None
        if not response.is_success:
            raise IntegrationError(f"API вернул HTTP {response.status_code}. Проверьте доступ и ключ.")
        try:
            return response.json() if response.content else None
        except ValueError:
            raise IntegrationError("API вернул ответ в неожиданном формате.") from None

    async def dokploy(self, endpoint: str, payload: dict | None = None, *, write: bool = False):
        s = self.settings
        if not s.dokploy_key:
            raise IntegrationError("Заполните DOKPLOY_API_KEY в окружении бота.")
        base = s.dokploy_url.removesuffix("/api")
        kwargs = {"headers": {"x-api-key": s.dokploy_key}}
        if write:
            kwargs["json"] = payload or {}
        else:
            kwargs["params"] = payload or {}
        data = await self.request("POST" if write else "GET", f"{base}/api/{endpoint}", **kwargs)
        # REST returns data directly; handle an explicit API envelope as well.
        if isinstance(data, dict) and "success" in data:
            if not data["success"]:
                raise IntegrationError("Dokploy отклонил запрос. Проверьте права API-ключа.")
            return data.get("data")
        return data

    async def services(self) -> list[Service]:
        projects = await self.dokploy("project.all")
        if not isinstance(projects, list):
            raise IntegrationError("Не удалось прочитать список проектов Dokploy.")
        result = []
        for project in projects:
            for env in project.get("environments", []):
                for kind, collection, id_key, status_key in (
                    ("application", "applications", "applicationId", "applicationStatus"),
                    ("compose", "compose", "composeId", "composeStatus"),
                ):
                    for row in env.get(collection, []):
                        result.append(Service(row[id_key], kind, row["name"], project["name"],
                                              env["name"], row.get(status_key, "unknown")))
        return result

    async def health(self) -> list[str]:
        async def check(name: str, url: str) -> str:
            try:
                response = await self.http.get(url)
                icon = "✅" if response.status_code < 400 else "⚠️"
                return f"{icon} {name}: HTTP {response.status_code}"
            except httpx.HTTPError:
                return f"❌ {name}: недоступен"
        return list(await asyncio.gather(*(check(name, url) for name, url in self.settings.health_urls.items())))

    async def find_service(self, name: str) -> Service:
        services = await self.services()
        needle = name.casefold()
        exact = [s for s in services if needle in {s.id.casefold(), s.label.casefold()}]
        matches = exact or [s for s in services if needle in {
            s.name.casefold(), s.project.casefold(), f"{s.project}/{s.name}".casefold()
        }]
        if not matches:
            raise IntegrationError("Сервис не найден. Посмотрите имена в /apps.")
        if len(matches) > 1:
            raise IntegrationError("Имя неоднозначно. Используйте полный путь из /apps:\n" +
                                   "\n".join(s.label for s in matches[:12]))
        return matches[0]

    async def logs(self, service: Service) -> str:
        if service.kind == "application":
            result = await self.dokploy("application.readLogs", {"applicationId": service.id, "tail": 60})
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        detail = await self.dokploy("compose.one", {"composeId": service.id})
        params = {"appName": detail["appName"], "appType": detail.get("composeType", "docker-compose")}
        if detail.get("serverId"):
            params["serverId"] = detail["serverId"]
        containers = await self.dokploy("docker.getContainersByAppNameMatch", params)
        if not isinstance(containers, list) or not containers:
            raise IntegrationError("У Compose-проекта нет доступных контейнеров.")
        output = []
        for container in containers[:4]:
            cid = container.get("containerId") or container.get("Id") or container.get("id")
            if not cid:
                continue
            result = await self.dokploy("compose.readLogs", {
                "composeId": service.id, "containerId": cid, "tail": 25,
            })
            output.append(f"--- {container.get('name', cid)} ---\n{result}")
        if not output:
            raise IntegrationError("API не вернул ID контейнеров Compose-проекта.")
        return "\n".join(output)

    async def service_action(self, service: Service, action: str) -> None:
        # Explicit endpoints only; never build an endpoint from a Telegram command.
        if action == "deploy":
            await self.dokploy(f"{service.kind}.deploy", {
                f"{service.kind}Id": service.id, "title": "Deploy requested via Shrimp Bot",
            }, write=True)
        elif action == "restart" and service.kind == "application":
            detail = await self.dokploy("application.one", {"applicationId": service.id})
            await self.dokploy("application.reload", {
                "applicationId": service.id, "appName": detail["appName"],
            }, write=True)
        elif action == "restart" and service.kind == "compose":
            await self.dokploy("compose.stop", {"composeId": service.id}, write=True)
            try:
                await self.dokploy("compose.start", {"composeId": service.id}, write=True)
            except IntegrationError:
                raise IntegrationError("Compose остановлен, но запуск не удался. Откройте Dokploy и запустите его вручную.") from None
        else:
            raise IntegrationError("Неизвестное действие.")

    async def server_probe(self, mode: str) -> dict:
        s = self.settings
        if not s.ssh_host:
            raise IntegrationError("Для метрик хоста заполните SERVER_SSH_HOST и настройте SSH-ключ.")
        args = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "StrictHostKeyChecking=yes", "-p", str(s.ssh_port), "-l", s.ssh_user]
        if s.ssh_key_file:
            args += ["-i", s.ssh_key_file, "-o", "IdentitiesOnly=yes"]
        if s.ssh_known_hosts:
            args += ["-o", f"UserKnownHostsFile={s.ssh_known_hosts}"]
        remote = shlex.join(["python3", "-", "--mode", mode, "--disk", s.disk_path,
                             "--interface", s.vpn_interface])
        args += ["--", s.ssh_host, remote]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            raise IntegrationError("SSH-клиент не найден. Установите openssh-client.") from None
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate((ROOT / "scripts/server_probe.py").read_bytes()), timeout=25
            )
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        except asyncio.TimeoutError:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise IntegrationError("Истекло время SSH-проверки.") from None
        if proc.returncode:
            raise IntegrationError("SSH-проверка не удалась. Проверьте ключ, known_hosts и Python 3 на сервере.")
        try:
            return json.loads(stdout)
        except ValueError:
            raise IntegrationError("Сервер вернул некорректные метрики.") from None

    async def minecraft(self) -> dict:
        if not self.settings.minecraft_host:
            raise IntegrationError("Заполните MINECRAFT_HOST в окружении бота.")
        server = JavaServer(self.settings.minecraft_host, self.settings.minecraft_port, timeout=5)
        try:
            status = await asyncio.wait_for(server.async_status(), timeout=8)
        except (OSError, asyncio.TimeoutError, ValueError):
            raise IntegrationError("Minecraft не ответил на Java Server List Ping. Проверьте адрес, порт и enable-status.") from None
        return {"online": status.players.online, "max": status.players.max,
                "version": status.version.name, "latency": round(status.latency)}

    async def ha(self, path: str, *, body: dict | None = None):
        s = self.settings
        if not s.ha_url or not s.ha_token:
            raise IntegrationError("Заполните HA_URL и HA_TOKEN в окружении бота.")
        return await self.request(
            "GET" if body is None else "POST", f"{s.ha_url}/api/{path}",
            headers={"Authorization": f"Bearer {s.ha_token}"},
            **({} if body is None else {"json": body}),
        )

    async def ha_state(self, entity: str) -> dict:
        if entity not in self.settings.ha_entities:
            raise IntegrationError("Датчик не добавлен в HA_ENTITIES.")
        return await self.ha("states/" + quote(entity, safe=""))

    async def ha_scene(self, entity: str) -> None:
        if entity not in self.settings.ha_scenes:
            raise IntegrationError("Сценарий не добавлен в HA_SCENES.")
        domain = entity.split(".")[0]
        await self.ha(f"services/{domain}/turn_on", body={"entity_id": entity})

    async def backup_state(self, target: BackupTarget) -> BackupState:
        rows = await self.dokploy("deployment.allByType", {"id": target.id, "type": target.kind})
        if not isinstance(rows, list):
            raise IntegrationError("Не удалось прочитать историю бэкапа.")
        floor = datetime.min.replace(tzinfo=timezone.utc)
        rows = sorted(rows, key=lambda r: parse_time(r.get("createdAt")) or floor, reverse=True)
        successes = [parse_time(r.get("finishedAt")) for r in rows if r.get("status") == "done"]
        last = max((date for date in successes if date), default=None)
        stale = last is None or (datetime.now(timezone.utc) - last).total_seconds() > target.max_age_hours * 3600
        return BackupState(target, last, rows[0].get("status", "unknown") if rows else "never", stale)

    async def run_backup(self, target: BackupTarget) -> None:
        if target not in self.settings.backups:
            raise IntegrationError("Бэкап не добавлен в BACKUP_TARGETS.")
        if target.kind == "volumeBackup":
            await self.dokploy("volumeBackups.runManually", {"volumeBackupId": target.id}, write=True)
            return
        detail = await self.dokploy("backup.one", {"backupId": target.id})
        if detail.get("backupType") == "compose":
            endpoint = "backup.manualBackupCompose"
        else:
            endpoint = {
                "postgres": "backup.manualBackupPostgres", "mysql": "backup.manualBackupMySql",
                "mariadb": "backup.manualBackupMariadb", "mongo": "backup.manualBackupMongo",
                "libsql": "backup.manualBackupLibsql", "web-server": "backup.manualBackupWebServer",
            }.get(detail.get("databaseType"))
        if not endpoint:
            raise IntegrationError("Этот тип бэкапа пока не поддерживается.")
        await self.dokploy(endpoint, {"backupId": target.id}, write=True)
