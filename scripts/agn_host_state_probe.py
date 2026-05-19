#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from admin_control_common import atomic_write_json, load_json, read_models_dir, repo_root, safe_name
from federated_host_state import validate_host_state_payload
from provider_registry import load_registry, probe_capabilities

DEFAULT_STALE_AFTER_SEC = 300
COLLECTOR_NAME = "agn_host_state_probe"
DEFAULT_OBSIDIAN_VAULT = Path.home() / "Documents" / "Example Vault"
GUI_AGENT_PATH = Path.home() / ".codex" / "bin" / "gui-agent"
HOST_STATE_LOCAL_FILENAME = "federated_host_state.local.json"
HOST_STATE_PREFIX = "federated_host_state"


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def _dt_to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_command(command: list[str], *, timeout_sec: float = 2.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _read_json_command(command: list[str], *, timeout_sec: float = 3.0) -> dict[str, Any]:
    result = _run_command(command, timeout_sec=timeout_sec)
    if not result or result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout or "{}")
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _bytes_to_gb(raw: int | float) -> float:
    return round(float(raw) / (1024**3), 2)


def _mac_hardware_overview() -> dict[str, Any]:
    payload = _read_json_command(["system_profiler", "SPHardwareDataType", "-json"], timeout_sec=4.0)
    items = payload.get("SPHardwareDataType")
    if not isinstance(items, list) or not items:
        return {}
    overview = items[0]
    return overview if isinstance(overview, dict) else {}


def _linux_os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    parsed: dict[str, str] = {}
    if not path.exists():
        return parsed
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip().strip('"')
    return parsed


def _loadavg_1m() -> float | None:
    try:
        return round(float(os.getloadavg()[0]), 2)
    except (AttributeError, OSError):
        return None


def _memory_total_gb(os_family: str, mac_hw: dict[str, Any]) -> float:
    if os_family == "macos":
        raw = str(mac_hw.get("physical_memory", "")).strip()
        if raw.endswith(" GB"):
            try:
                return round(float(raw[:-3].strip()), 2)
            except ValueError:
                pass
        result = _run_command(["sysctl", "-n", "hw.memsize"])
        if result and result.returncode == 0:
            try:
                return _bytes_to_gb(int((result.stdout or "0").strip()))
            except ValueError:
                return 0.0
    if os_family == "linux":
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    try:
                        kib = int(line.split()[1])
                        return round(kib / (1024**2), 2)
                    except (IndexError, ValueError):
                        return 0.0
    return 0.0


def _memory_available_gb(os_family: str) -> float:
    if os_family == "macos":
        result = _run_command(["vm_stat"])
        if result and result.returncode == 0:
            page_size = 4096
            pages: dict[str, int] = {}
            for raw_line in (result.stdout or "").splitlines():
                line = raw_line.strip()
                if line.startswith("Mach Virtual Memory Statistics:") and "page size of" in line:
                    parts = line.split("page size of", 1)
                    if len(parts) == 2:
                        try:
                            page_size = int(parts[1].split("bytes", 1)[0].strip())
                        except ValueError:
                            page_size = 4096
                    continue
                if ":" not in line:
                    continue
                key, raw_value = line.split(":", 1)
                cleaned = raw_value.strip().rstrip(".").replace(".", "")
                cleaned = cleaned.replace(",", "")
                try:
                    pages[key] = int(cleaned)
                except ValueError:
                    continue
            free_pages = (
                pages.get("Pages free", 0)
                + pages.get("Pages inactive", 0)
                + pages.get("Pages speculative", 0)
            )
            return _bytes_to_gb(free_pages * page_size)
        return 0.0

    if os_family == "linux":
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if line.startswith("MemAvailable:"):
                    try:
                        kib = int(line.split()[1])
                        return round(kib / (1024**2), 2)
                    except (IndexError, ValueError):
                        return 0.0
        return 0.0

    return 0.0


def _parse_mac_power_state() -> tuple[dict[str, Any], bool]:
    batt = _run_command(["pmset", "-g", "batt"])
    base = {"source": "unknown", "battery_percent": None, "charging": None, "low_power_mode": None}
    has_battery = False
    if batt and batt.returncode == 0:
        text = batt.stdout or ""
        if "InternalBattery" in text:
            has_battery = True
        if "AC Power" in text:
            base["source"] = "ac"
        elif "Battery Power" in text:
            base["source"] = "battery"
        for token in text.split():
            if token.endswith("%;"):
                try:
                    base["battery_percent"] = float(token.rstrip("%;"))
                except ValueError:
                    pass
        lower = text.lower()
        if "charging;" in lower:
            base["charging"] = True
        elif "discharging;" in lower or "charged;" in lower or "finishing charge;" in lower:
            base["charging"] = False

    pmset = _run_command(["pmset", "-g"])
    if pmset and pmset.returncode == 0:
        for line in (pmset.stdout or "").splitlines():
            if "lowpowermode" not in line:
                continue
            value = line.strip().split()[-1]
            if value in {"0", "1"}:
                base["low_power_mode"] = value == "1"
            break
    if not has_battery:
        base["source"] = "ac" if base["source"] == "unknown" else base["source"]
    return base, has_battery


def _parse_linux_power_state() -> tuple[dict[str, Any], bool]:
    base = {"source": "n_a", "battery_percent": None, "charging": None, "low_power_mode": None}
    power_root = Path("/sys/class/power_supply")
    if not power_root.exists():
        return base, False
    batteries = list(power_root.glob("BAT*"))
    mains = list(power_root.glob("AC*")) + list(power_root.glob("ADP*"))
    if not batteries:
        return {"source": "ac" if mains else "n_a", "battery_percent": None, "charging": None, "low_power_mode": None}, False
    battery = batteries[0]
    has_battery = True
    capacity = battery / "capacity"
    status = battery / "status"
    if capacity.exists():
        try:
            base["battery_percent"] = float(capacity.read_text(encoding="utf-8").strip())
        except ValueError:
            base["battery_percent"] = None
    if status.exists():
        state = status.read_text(encoding="utf-8").strip().lower()
        base["charging"] = state == "charging"
        if state in {"discharging", "not charging"}:
            base["charging"] = False
    if mains:
        online_path = mains[0] / "online"
        if online_path.exists():
            base["source"] = "ac" if online_path.read_text(encoding="utf-8").strip() == "1" else "battery"
    elif base["charging"] is False:
        base["source"] = "battery"
    return base, has_battery


def _detect_power() -> tuple[dict[str, Any], dict[str, Any]]:
    os_family = _detect_os_family()
    if os_family == "macos":
        state, has_battery = _parse_mac_power_state()
    elif os_family == "linux":
        state, has_battery = _parse_linux_power_state()
    else:
        state, has_battery = (
            {"source": "unknown", "battery_percent": None, "charging": None, "low_power_mode": None},
            False,
        )
    profile = {
        "has_battery": has_battery,
        "preferred_power_source": "battery_or_ac" if has_battery else ("ac_only" if state["source"] != "n_a" else "n_a"),
    }
    return profile, state


def _detect_os_family() -> str:
    raw = platform.system().lower()
    if raw == "darwin":
        return "macos"
    if raw == "linux":
        return "linux"
    if raw == "windows":
        return "windows"
    return "other"


def _detect_os_version(os_family: str) -> str:
    if os_family == "macos":
        result = _run_command(["sw_vers"])
        if result and result.returncode == 0:
            product_name = ""
            product_version = ""
            for raw_line in (result.stdout or "").splitlines():
                line = raw_line.strip()
                if line.startswith("ProductName:"):
                    product_name = line.split(":", 1)[1].strip()
                elif line.startswith("ProductVersion:"):
                    product_version = line.split(":", 1)[1].strip()
            if product_name and product_version:
                return f"{product_name} {product_version}"
    if os_family == "linux":
        release = _linux_os_release()
        pretty = release.get("PRETTY_NAME", "").strip()
        if pretty:
            return pretty
    return platform.platform()


def _detect_device_model(os_family: str, mac_hw: dict[str, Any]) -> str:
    if os_family == "macos":
        name = str(mac_hw.get("machine_name", "")).strip()
        if name:
            return name
        result = _run_command(["sysctl", "-n", "hw.model"])
        if result and result.returncode == 0:
            return (result.stdout or "").strip()
    if os_family == "linux":
        for candidate in (
            Path("/sys/devices/virtual/dmi/id/product_name"),
            Path("/sys/class/dmi/id/product_name"),
        ):
            if candidate.exists():
                value = candidate.read_text(encoding="utf-8").strip()
                if value:
                    return value
    return platform.machine()


def _detect_instance_id(os_family: str, mac_hw: dict[str, Any], hostname: str) -> str:
    override = str(os.getenv("AGN_INSTANCE_ID", "")).strip()
    if override:
        return override
    if os_family == "macos":
        for key in ("platform_UUID", "serial_number"):
            value = str(mac_hw.get(key, "")).strip()
            if value:
                return safe_name(value.lower(), default="instance")
    if os_family == "linux":
        for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            if candidate.exists():
                value = candidate.read_text(encoding="utf-8").strip()
                if value:
                    return safe_name(value.lower(), default="instance")
    return safe_name(f"{hostname}-{platform.machine()}-{os_family}", default="instance")


def _derive_identity(
    *,
    hostname: str,
    os_family: str,
    device_model: str,
    has_battery: bool,
    instance_id: str,
) -> dict[str, str]:
    host_class = str(os.getenv("AGN_HOST_CLASS", "")).strip()
    environment = str(os.getenv("AGN_HOST_ENVIRONMENT", "")).strip()
    display_name = str(os.getenv("AGN_HOST_DISPLAY_NAME", "")).strip()
    role_hint = str(os.getenv("AGN_HOST_ROLE_HINT", "")).strip()
    if not host_class:
        if os_family == "macos" and has_battery:
            host_class = "mac_laptop"
        elif os_family == "macos":
            host_class = "mac_desktop"
        elif os_family == "linux":
            host_class = "linux_server"
        else:
            host_class = "other"
    if not environment:
        if host_class == "mac_laptop":
            environment = "portable_local"
        elif host_class == "mac_desktop":
            environment = "primary_local"
        elif host_class == "linux_server":
            environment = "cloud_remote"
        else:
            environment = "other"
    if not display_name:
        display_name = device_model or hostname
    if not role_hint:
        if host_class == "mac_laptop":
            role_hint = "portable_control_node"
        elif host_class == "mac_desktop":
            role_hint = "primary_execution_node"
        elif host_class == "linux_server":
            role_hint = "remote_service_node"
        else:
            role_hint = "general_node"
    host_id = str(os.getenv("AGN_HOST_ID", "")).strip()
    if not host_id:
        host_id = safe_name(f"{display_name}-{hostname}-{environment}".lower(), default="host")
    return {
        "host_id": host_id,
        "instance_id": instance_id,
        "display_name": display_name,
        "host_class": host_class,
        "environment": environment,
        "role_hint": role_hint,
    }


def _storage_kind(path: Path) -> str:
    if path == Path("/"):
        return "internal"
    mac_volume_prefix = str(Path("/Volumes")) + "/"
    if str(path).startswith(mac_volume_prefix) or str(path).startswith("/media/"):
        return "external"
    if str(path).startswith("/mnt/"):
        return "network"
    return "other"


def _candidate_storage_roots(os_family: str) -> list[Path]:
    roots = [Path("/")]
    for candidate in (Path("/mnt"), Path("/mnt/data")):
        if candidate.exists() and candidate not in roots:
            roots.append(candidate)
    if os_family == "macos":
        volumes_dir = Path("/Volumes")
        if volumes_dir.exists():
            for candidate in sorted(volumes_dir.iterdir()):
                if candidate == volumes_dir / "Macintosh HD":
                    continue
                if candidate.exists() and candidate not in roots:
                    roots.append(candidate)
    return roots


def _storage_roots(os_family: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in _candidate_storage_roots(os_family):
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        entries.append(
            {
                "name": "system" if path == Path("/") else safe_name(path.name.lower(), default="storage"),
                "path": str(path),
                "kind": _storage_kind(path),
                "total_gb": _bytes_to_gb(usage.total),
                "removable": str(path).startswith(str(Path("/Volumes")) + "/") or str(path).startswith("/media/"),
            }
        )
    return entries or [
        {
            "name": "system",
            "path": "/",
            "kind": "other",
            "total_gb": 1.0,
            "removable": False,
        }
    ]


def _storage_free(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for entry in entries:
        path = Path(str(entry["path"]))
        free_gb = 0.0
        try:
            usage = shutil.disk_usage(path)
            free_gb = _bytes_to_gb(usage.free)
        except OSError:
            free_gb = 0.0
        results.append(
            {
                "name": str(entry["name"]),
                "free_gb": free_gb,
                "writable": os.access(path, os.W_OK),
            }
        )
    return results


def _detect_codex_home() -> str:
    override = str(os.getenv("CODEX_HOME", "")).strip()
    if override:
        return str(Path(override).expanduser().resolve())
    preferred = Path.home() / ".codex_agn"
    if preferred.exists():
        return str(preferred)
    return str((Path.home() / ".codex").resolve())


def _detect_obsidian_vault() -> str | None:
    override = str(os.getenv("AGN_OBSIDIAN_VAULT", "")).strip()
    if override:
        candidate = Path(override).expanduser()
        return str(candidate.resolve()) if candidate.exists() else None
    if DEFAULT_OBSIDIAN_VAULT.exists():
        return str(DEFAULT_OBSIDIAN_VAULT.resolve())
    return None


def _local_model_roots() -> list[str]:
    roots: list[str] = []
    for candidate in (
        os.getenv("AGN_LOCAL_MODEL_ROOT", "").strip(),
        os.getenv("QWEN_LOCAL_MODEL_ROOT", "").strip(),
    ):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.exists() and path.is_dir():
            resolved = str(path.resolve())
            if resolved not in roots:
                roots.append(resolved)
    return roots


def _declared_inventory(registry: dict[str, Any]) -> dict[str, list[str]]:
    executors = registry.get("executors", {})
    reviewers = registry.get("reviewers", {})
    providers = sorted({*executors.keys(), *reviewers.keys()}) if isinstance(executors, dict) and isinstance(reviewers, dict) else []
    wrappers = sorted(
        name
        for name in ("agn_browser_use_wrapper", "agn_hindsight_wrapper", "agn_promptfoo_wrapper")
        if (repo_root() / "scripts" / f"{name}.py").exists()
    )
    tools = ["ghostty", "obsidian", "google_chrome", "gui_agent"]
    local_models = ["qwen_local_model"] if "qwen_local" in providers else []
    return {
        "declared_providers": providers,
        "declared_local_models": local_models,
        "declared_tools": tools,
        "declared_wrappers": wrappers,
    }


def _localhost_reachable(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    port = parsed.port
    if host not in {"127.0.0.1", "localhost"}:
        return True, ""
    if port is None:
        return False, "local_provider_port_missing"
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True, ""
    except OSError:
        return False, f"local_provider_endpoint_unreachable:{host}:{port}"


def _provider_items() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    registry = load_registry()
    probes = probe_capabilities(registry)
    inventory = _declared_inventory(registry)
    probe_map: dict[str, dict[str, Any]] = {}
    for group in ("executors", "reviewers"):
        entries = probes.get(group, {})
        if not isinstance(entries, dict):
            continue
        for name, item in entries.items():
            if isinstance(item, dict):
                probe_map[name] = item

    items: list[dict[str, Any]] = []
    local_models: list[dict[str, Any]] = []
    for name in inventory["declared_providers"]:
        probe = probe_map.get(name, {})
        kind = str(probe.get("kind", "")).strip().lower()
        configured = bool(probe)
        available = bool(probe.get("available", False))
        reason = ""
        if kind == "cli":
            reason = "" if available else f"cli_not_found:{probe.get('command') or name}"
        elif kind == "api":
            reason = str(probe.get("unavailable_reason", "")).strip()
            base_url = str(probe.get("base_url", "")).strip()
            local_ready, local_reason = _localhost_reachable(base_url) if base_url else (False, "provider_missing_base_url_or_model")
            if available and not local_ready:
                available = False
                reason = local_reason
            elif not reason and not available:
                reason = local_reason
        item: dict[str, Any] = {"name": name, "configured": configured, "available": available}
        if reason:
            item["reason"] = reason
        items.append(item)

        if name == "qwen_local":
            model_reason = str(probe.get("unavailable_reason", "")).strip()
            local_models.append(
                {
                    "name": "qwen_local_model",
                    "configured": configured,
                    "available": available,
                    **({"reason": model_reason} if model_reason else {}),
                }
            )

    return items, local_models


def _tool_items() -> list[dict[str, Any]]:
    checks = {
        "ghostty": shutil.which("ghostty"),
        "obsidian": shutil.which("obsidian"),
        "google_chrome": "/Applications/Google Chrome.app" if Path("/Applications/Google Chrome.app").exists() else "",
        "gui_agent": str(GUI_AGENT_PATH) if GUI_AGENT_PATH.exists() else "",
    }
    results: list[dict[str, Any]] = []
    for name, value in checks.items():
        available = bool(value)
        item: dict[str, Any] = {"name": name, "configured": available, "available": available}
        if not available:
            if name == "gui_agent":
                item["reason"] = f"missing_path:{GUI_AGENT_PATH}"
            else:
                item["reason"] = f"not_found:{name}"
                item["configured"] = False
        results.append(item)
    return results


def _wrapper_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for name in ("agn_browser_use_wrapper", "agn_hindsight_wrapper", "agn_promptfoo_wrapper"):
        path = repo_root() / "scripts" / f"{name}.py"
        available = path.exists()
        item: dict[str, Any] = {"name": name, "configured": True, "available": available}
        if not available:
            item["reason"] = f"script_missing:{path}"
        items.append(item)
    return items


def _network_state() -> dict[str, Any]:
    online = False
    last_error = ""
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=1.0):
            online = True
    except OSError as exc:
        last_error = str(exc)
    targets = _remote_targets()
    reachability: list[dict[str, Any]] = []
    for target in targets:
        host_id = str(target.get("host_id", "")).strip()
        transport = str(target.get("transport", "")).strip() or "unknown"
        address = str(target.get("address", "")).strip()
        port = int(target.get("port", 22) or 22)
        reachable = False
        latency_ms: float | None = None
        error = ""
        if address:
            started = utc_now()
            try:
                with socket.create_connection((address, port), timeout=0.8):
                    reachable = True
                    latency_ms = round((utc_now() - started).total_seconds() * 1000, 2)
            except OSError as exc:
                error = str(exc)
        entry: dict[str, Any] = {
            "target_host_id": host_id or safe_name(address or transport, default="remote"),
            "transport": transport,
            "reachable": reachable,
            "latency_ms": latency_ms,
        }
        if error:
            entry["last_error"] = error
        reachability.append(entry)
    if not online and last_error and not reachability:
        reachability = []
    return {"online": online, "default_route": online, "remote_reachability": reachability}


def _remote_targets() -> list[dict[str, Any]]:
    raw = str(os.getenv("AGN_REMOTE_TARGETS_JSON", "")).strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def output_paths_for_host(host_id: str) -> list[Path]:
    _ = host_id
    return [read_models_dir() / HOST_STATE_LOCAL_FILENAME]


def collect_host_state(*, stale_after_sec: int = DEFAULT_STALE_AFTER_SEC) -> dict[str, Any]:
    observed_at = utc_now()
    os_family = _detect_os_family()
    mac_hw = _mac_hardware_overview() if os_family == "macos" else {}
    hostname = socket.gethostname()
    power_profile, power_state = _detect_power()
    device_model = _detect_device_model(os_family, mac_hw)
    instance_id = _detect_instance_id(os_family, mac_hw, hostname)
    identity = _derive_identity(
        hostname=hostname,
        os_family=os_family,
        device_model=device_model,
        has_battery=bool(power_profile["has_battery"]),
        instance_id=instance_id,
    )
    storage_roots = _storage_roots(os_family)
    provider_items, local_model_items = _provider_items()
    registry = load_registry()
    inventory = _declared_inventory(registry)
    payload = {
        "schema_version": "agn.host_state.v1",
        "host_identity": identity,
        "static_facts": {
            "device": {
                "hostname": hostname,
                "os_family": os_family,
                "os_version": _detect_os_version(os_family),
                "architecture": platform.machine(),
                "device_model": device_model,
            },
            "resources": {
                "cpu_logical_cores": int(os.cpu_count() or 1),
                "memory_total_gb": _memory_total_gb(os_family, mac_hw),
                "storage_roots": storage_roots,
                "power_profile": power_profile,
            },
            "path_scope": {
                "repo_root": str(repo_root()),
                "codex_home": _detect_codex_home(),
                "obsidian_vault": _detect_obsidian_vault(),
                "local_model_roots": _local_model_roots(),
            },
            "capability_inventory": inventory,
        },
        "runtime_facts": {
            "resource_state": {
                "cpu_load_1m": _loadavg_1m(),
                "memory_available_gb": _memory_available_gb(os_family),
                "storage_free_gb": _storage_free(storage_roots),
                "power_state": power_state,
            },
            "availability": {
                "providers": provider_items,
                "local_models": local_model_items,
                "tools": _tool_items(),
                "wrappers": _wrapper_items(),
            },
            "network": _network_state(),
        },
        "heartbeat": {
            "observed_at": _dt_to_iso(observed_at),
            "fresh_until": _dt_to_iso(observed_at + timedelta(seconds=stale_after_sec)),
            "stale_after_sec": stale_after_sec,
            "collector": COLLECTOR_NAME,
        },
    }
    return payload


def _availability_map(payload: dict[str, Any], group: str) -> dict[str, dict[str, Any]]:
    availability = payload.get("runtime_facts", {}).get("availability", {}).get(group, [])
    if not isinstance(availability, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in availability:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if name:
            result[name] = item
    return result


def write_host_state(payload: dict[str, Any]) -> list[str]:
    paths = output_paths_for_host(str(payload["host_identity"]["host_id"]))
    written: list[str] = []
    for path in paths:
        atomic_write_json(path, payload)
        written.append(str(path))
    for candidate in read_models_dir().glob(f"{HOST_STATE_PREFIX}.*.json"):
        if candidate.name == HOST_STATE_LOCAL_FILENAME:
            continue
        candidate.unlink(missing_ok=True)
    return written


def run_self_check(
    payload: dict[str, Any],
    *,
    require_tools: list[str],
    require_wrappers: list[str],
    require_providers: list[str],
) -> dict[str, Any]:
    validation = validate_host_state_payload(payload)
    checks: list[dict[str, Any]] = [
        {"name": "schema_validation", "ok": validation.valid, "details": validation.errors},
        {"name": "host_id_present", "ok": bool(str(payload["host_identity"]["host_id"]).strip())},
    ]
    failures: list[dict[str, Any]] = []
    for path in output_paths_for_host(str(payload["host_identity"]["host_id"])):
        checks.append({"name": f"write_target_parent_exists:{path.name}", "ok": path.parent.exists(), "details": str(path.parent)})

    for name in require_tools:
        item = _availability_map(payload, "tools").get(name)
        ok = bool(item and item.get("available"))
        details = "" if ok else (str(item.get("reason", "")) if item else "tool_missing_from_payload")
        checks.append({"name": f"require_tool:{name}", "ok": ok, "details": details})
        if not ok:
            failures.append({"kind": "tool", "name": name, "details": details})

    for name in require_wrappers:
        item = _availability_map(payload, "wrappers").get(name)
        ok = bool(item and item.get("available"))
        details = "" if ok else (str(item.get("reason", "")) if item else "wrapper_missing_from_payload")
        checks.append({"name": f"require_wrapper:{name}", "ok": ok, "details": details})
        if not ok:
            failures.append({"kind": "wrapper", "name": name, "details": details})

    for name in require_providers:
        item = _availability_map(payload, "providers").get(name)
        ok = bool(item and item.get("available"))
        details = "" if ok else (str(item.get("reason", "")) if item else "provider_missing_from_payload")
        checks.append({"name": f"require_provider:{name}", "ok": ok, "details": details})
        if not ok:
            failures.append({"kind": "provider", "name": name, "details": details})

    ok = validation.valid and all(bool(check["ok"]) for check in checks if str(check["name"]).startswith("require_")) and bool(str(payload["host_identity"]["host_id"]).strip())
    return {
        "ok": ok,
        "host_id": payload["host_identity"]["host_id"],
        "observed_at": payload["heartbeat"]["observed_at"],
        "checks": checks,
        "failures": failures,
    }


def cmd_collect(args: argparse.Namespace) -> int:
    payload = collect_host_state(stale_after_sec=int(args.stale_after_sec))
    validation = validate_host_state_payload(payload)
    output_paths: list[str] = []
    if not args.no_write and validation.valid:
        output_paths = write_host_state(payload)
    result = {
        "ok": validation.valid,
        "host_id": payload["host_identity"]["host_id"],
        "observed_at": payload["heartbeat"]["observed_at"],
        "output_paths": output_paths,
        "validation_errors": validation.errors,
    }
    if args.print_state:
        result["payload"] = payload
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if validation.valid else 1


def cmd_self_check(args: argparse.Namespace) -> int:
    payload = collect_host_state(stale_after_sec=int(args.stale_after_sec))
    if not args.no_write and validate_host_state_payload(payload).valid:
        write_host_state(payload)
    report = run_self_check(
        payload,
        require_tools=list(args.require_tool or []),
        require_wrappers=list(args.require_wrapper or []),
        require_providers=list(args.require_provider or []),
    )
    if args.print_state:
        report["payload"] = payload
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and export the local AGN host state.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="Collect and write the local host state.")
    collect.add_argument("--stale-after-sec", type=int, default=DEFAULT_STALE_AFTER_SEC)
    collect.add_argument("--no-write", action="store_true")
    collect.add_argument("--print-state", action="store_true")
    collect.set_defaults(func=cmd_collect)

    self_check = subparsers.add_parser("self-check", help="Collect host state and evaluate required capabilities.")
    self_check.add_argument("--stale-after-sec", type=int, default=DEFAULT_STALE_AFTER_SEC)
    self_check.add_argument("--no-write", action="store_true")
    self_check.add_argument("--print-state", action="store_true")
    self_check.add_argument("--require-tool", action="append", default=[])
    self_check.add_argument("--require-wrapper", action="append", default=[])
    self_check.add_argument("--require-provider", action="append", default=[])
    self_check.set_defaults(func=cmd_self_check)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
