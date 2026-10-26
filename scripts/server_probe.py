"""Read-only Linux probe, sent to the host via SSH. No third-party dependencies."""

import argparse
import json
import os
import shutil
import time
import urllib.error
import urllib.request


def cpu_sample():
    with open("/proc/stat") as stream:
        # guest/guest_nice are already included in user/nice.
        values = [int(x) for x in stream.readline().split()[1:9]]
    return sum(values), values[3] + values[4]


def status(path):
    total_before, idle_before = cpu_sample()
    time.sleep(0.25)
    total_after, idle_after = cpu_sample()
    elapsed = total_after - total_before
    with open("/proc/meminfo") as stream:
        mem = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in stream}
    disk = shutil.disk_usage(path)
    return {
        "hostname": os.uname().nodename,
        "cpu_percent": round(100 * (1 - (idle_after - idle_before) / elapsed), 1) if elapsed else 0,
        "load": list(os.getloadavg()),
        "ram_total": mem["MemTotal"],
        "ram_used": mem["MemTotal"] - mem["MemAvailable"],
        "disk_total": disk.total, "disk_used": disk.used, "disk_free": disk.free,
    }


def vpn(interface):
    result = {"interface_present": os.path.exists("/sys/class/net/" + interface)}
    started = time.monotonic()
    try:
        # A Telegram HTTP response proves reachability, not the route or exit IP.
        with urllib.request.urlopen("https://api.telegram.org", timeout=10) as response:
            result.update(telegram_ok=True, http_status=response.status)
    except urllib.error.HTTPError as exc:
        result.update(telegram_ok=True, http_status=exc.code)
    except (OSError, urllib.error.URLError):
        result.update(telegram_ok=False)
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("status", "vpn"), required=True)
    parser.add_argument("--disk", default="/")
    parser.add_argument("--interface", default="tg-vpn")
    args = parser.parse_args()
    print(json.dumps(status(args.disk) if args.mode == "status" else vpn(args.interface)))
