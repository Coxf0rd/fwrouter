from __future__ import annotations

from typing import Any


DEFAULT_UI_TEXT_LOCALE = "ru"
SUPPORTED_UI_TEXT_LOCALES = {"ru", "en"}


def _normalize_ui_text_locale(locale: Any = None) -> str:
    raw = str(locale or DEFAULT_UI_TEXT_LOCALE).strip().lower().replace("_", "-")
    lang = raw.split("-", 1)[0]
    return lang if lang in SUPPORTED_UI_TEXT_LOCALES else DEFAULT_UI_TEXT_LOCALE


def _ui_text(
    *,
    title: str | None = None,
    title_en: str | None = None,
    reason: str | None = None,
    reason_en: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    if title is not None:
        entry["title"] = title
        entry["title_i18n"] = {
            "ru": title,
            "en": title_en or title,
        }
    if reason is not None:
        entry["reason"] = reason
        entry["reason_i18n"] = {
            "ru": reason,
            "en": reason_en or reason,
        }
    return entry


UI_TEXT_REGISTRY = {
    "watchdog.status": {
        "paused_signal_unavailable": _ui_text(
            title="Нет свежего сигнала трафика",
            title_en="No fresh traffic signal",
            reason=(
                "Нет свежего достоверного снимка счетчиков трафика, поэтому автоматическая смена "
                "подавлена, чтобы не переключать сервер по ложному сигналу."
            ),
            reason_en=(
                "There is no fresh reliable traffic-counter snapshot, so automatic switching is suppressed "
                "to avoid changing servers on a false signal."
            ),
        ),
        "traffic_failure_pending": _ui_text(
            title="Сбой трафика еще подтверждается",
            title_en="Traffic failure is still being confirmed",
            reason=(
                "Замечен исходящий VPN-трафик без ответных байтов; watchdog ждет повторный свежий "
                "снимок перед failover."
            ),
            reason_en=(
                "Outgoing VPN traffic was seen without response bytes; watchdog waits for a repeated fresh "
                "snapshot before failover."
            ),
        ),
        "failover_candidate_found": _ui_text(
            title="Кандидат найден, смена не применялась",
            title_en="Candidate found, switch was not applied",
            reason="Проверка нашла рабочий сервер, но текущий запуск был без права применять смену.",
            reason_en="The check found a working server, but this run was not allowed to apply the switch.",
        ),
        "failover_applied": _ui_text(
            title="Failover применен",
            title_en="Failover applied",
            reason="Watchdog подтвердил проблему и применил рабочий VPN-auto кандидат.",
            reason_en="Watchdog confirmed the problem and applied a working VPN-auto candidate.",
        ),
        "fail_open_direct_recommended": _ui_text(
            title="Рабочий кандидат не найден",
            title_en="No working candidate found",
            reason=(
                "Сбой VPN-трафика подтвержден, но среди кандидатов не найден рабочий сервер "
                "для автоматической смены."
            ),
            reason_en=(
                "The VPN traffic failure was confirmed, but no working server candidate was found "
                "for automatic switching."
            ),
        ),
        "runtime_convergence_failed": _ui_text(
            title="Runtime маршрутизации нездоров",
            title_en="Routing runtime is unhealthy",
            reason=(
                "Сначала нужно восстановить dataplane/runtime; смена VPN-сервера могла бы скрыть "
                "основную проблему."
            ),
            reason_en=(
                "The dataplane/runtime must be restored first; changing the VPN server could hide "
                "the root problem."
            ),
        ),
        "runtime_unavailable": _ui_text(
            title="VPN runtime недоступен",
            title_en="VPN runtime is unavailable",
            reason="Активный VPN runtime не готов или не отвечает, поэтому сервер не менялся.",
            reason_en="The active VPN runtime is not ready or not responding, so the server was not changed.",
        ),
        "external_runtime_active": _ui_text(
            title="Активен внешний VPN runtime",
            title_en="External VPN runtime is active",
            reason="FWRouter видит внешний VPN runtime и не управляет его selector напрямую.",
            reason_en="FWRouter sees an external VPN runtime and does not control its selector directly.",
        ),
        "external_runtime_failover_unavailable": _ui_text(
            title="У внешнего VPN runtime нет failover",
            title_en="External VPN runtime has no failover",
            reason=(
                "Сбой трафика подтвержден, но внешний VPN runtime не предоставил endpoint "
                "для автоматического failover."
            ),
            reason_en=(
                "The traffic failure was confirmed, but the external VPN runtime did not provide an endpoint "
                "for automatic failover."
            ),
        ),
        "needs_initial_auto_selection": _ui_text(
            title="Нет валидного активного auto-сервера",
            title_en="No valid active auto server",
            reason="В режиме VPN-auto нет валидного активного сервера; нужен первичный выбор.",
            reason_en="VPN-auto mode has no valid active server; an initial selection is required.",
        ),
        "scheduler_failed": _ui_text(
            title="Фоновая проверка упала",
            title_en="Background check failed",
            reason="Внутренняя ошибка остановила один шаг фоновой проверки.",
            reason_en="An internal error stopped one background check step.",
        ),
        "manual_selection": _ui_text(
            title="Включен ручной выбор сервера",
            title_en="Manual server selection is enabled",
            reason=(
                "Сбой трафика подтвержден, но выбран ручной режим сервера, поэтому автоматика "
                "не переключает."
            ),
            reason_en=(
                "The traffic failure was confirmed, but manual server mode is selected, so automation "
                "does not switch."
            ),
        ),
        "failover_cooldown": _ui_text(
            title="Failover на паузе после недавней смены",
            title_en="Failover is paused after a recent switch",
            reason="Сбой трафика подтвержден, но после недавней смены еще действует cooldown.",
            reason_en="The traffic failure was confirmed, but cooldown after a recent switch is still active.",
        ),
        "active_quality_degraded_traffic_healthy": _ui_text(
            title="Проверка сервера нестабильна, но VPN-трафик отвечает",
            title_en="Server check is unstable, but VPN traffic responds",
            reason=(
                "Delay-check текущего сервера нестабилен, но есть ответный VPN-трафик; "
                "watchdog не меняет сервер по одному техническому сигналу."
            ),
            reason_en=(
                "The current-server delay-check is unstable, but response VPN traffic is present; "
                "watchdog does not change server on one technical signal."
            ),
        ),
        "active_quality_degraded_pending": _ui_text(
            title="Качество сервера деградирует, идет подтверждение",
            title_en="Server quality is degraded, confirmation is in progress",
            reason=(
                "Ответный VPN-трафик есть, но delay-check текущего сервера повторно нестабилен; "
                "watchdog ждет окно подтверждения перед сменой."
            ),
            reason_en=(
                "Response VPN traffic is present, but the current-server delay-check is repeatedly unstable; "
                "watchdog is waiting for the confirmation window before switching."
            ),
        ),
    },
    "watchdog.action": {
        "none": _ui_text(title="Сервер не менялся", title_en="Server was not changed"),
        "dry_run_only": _ui_text(title="Только проверка, без применения", title_en="Check only, no changes applied"),
        "switch_vpn_auto": _ui_text(title="Выбран новый VPN-auto сервер", title_en="New VPN-auto server selected"),
        "fail_open_direct_recommended": _ui_text(
            title="Нужна ручная проверка или временный DIRECT",
            title_en="Manual check or temporary DIRECT is needed",
        ),
    },
    "watchdog.event": {
        "scheduler_failed": _ui_text(
            title="Watchdog не выполнил фоновую проверку",
            title_en="Watchdog did not complete the background check",
        ),
        "switch_suppressed": _ui_text(
            title="Watchdog не стал менять VPN-сервер",
            title_en="Watchdog did not change the VPN server",
        ),
        "switch_applied": _ui_text(
            title="Watchdog сменил VPN-сервер",
            title_en="Watchdog changed the VPN server",
        ),
        "switch_candidate": _ui_text(
            title="Watchdog нашел VPN-кандидата",
            title_en="Watchdog found a VPN candidate",
        ),
    },
    "error.code": {
        "RULES_VALIDATION_FAILED": _ui_text(
            reason="В правилах маршрутизации есть некорректная строка или неподдерживаемый формат.",
            reason_en="A routing rule contains an invalid line or unsupported format.",
        ),
    },
    "traffic.metric": {
        "direct_rx_bytes": _ui_text(title="DIRECT вход", title_en="DIRECT inbound"),
        "direct_tx_bytes": _ui_text(title="DIRECT выход", title_en="DIRECT outbound"),
        "vpn_rx_bytes": _ui_text(title="VPN вход", title_en="VPN inbound"),
        "vpn_tx_bytes": _ui_text(title="VPN выход", title_en="VPN outbound"),
    },
    "inventory.activity": {
        "profile_seen_24h": _ui_text(title="Профиль запрашивался за 24ч", title_en="Profile requested within 24h"),
        "traffic_seen": _ui_text(title="Был трафик", title_en="Traffic was seen"),
        "runtime_active": _ui_text(title="Runtime активен", title_en="Runtime is active"),
        "stale_seen": _ui_text(title="Нет свежей активности", title_en="No recent activity"),
        "unknown": _ui_text(title="Нет данных активности", title_en="No activity data"),
    },
    "display.system.title": {
        "lan": _ui_text(title="Lan / Core", title_en="LAN / Core"),
        "external_network_source": _ui_text(title="Внешняя сеть", title_en="External network"),
        "vless_client": _ui_text(title="Vless", title_en="Vless"),
        "vpn_runtime": _ui_text(title="VPN runtime", title_en="VPN runtime"),
        "docker": _ui_text(title="Docker", title_en="Docker"),
        "host": _ui_text(title="Службы хоста", title_en="Host services"),
    },
    "display.system.description": {
        "lan": _ui_text(title="Клиенты LAN и routing core FWRouter.", title_en="LAN clients and FWRouter routing core."),
        "external_network_source": _ui_text(
            title="Внешний источник клиентов; FWRouter показывает его только когда есть реальные найденные клиенты.",
            title_en="External client source; FWRouter shows it only when real discovered clients exist.",
        ),
        "vless_client": _ui_text(
            title="Клиентское ядро Vless; конкретная реализация хранится отдельно.",
            title_en="Vless client core; the concrete implementation is stored separately.",
        ),
        "vpn_runtime": _ui_text(
            title="VPN/dataplane-адаптер FWRouter; конкретная реализация хранится отдельно.",
            title_en="FWRouter VPN/dataplane adapter; the concrete implementation is stored separately.",
        ),
        "docker": _ui_text(
            title="Отображение контейнеров; это не управляемый runtime-модуль.",
            title_en="Container inventory view; this is not a managed runtime module.",
        ),
        "host": _ui_text(title="Отображение служб хоста и systemd.", title_en="Host and systemd services inventory view."),
        "external_network_discovered": _ui_text(
            title="Внешний сетевой источник найден в inventory клиентов.",
            title_en="External network source discovered from client inventory.",
        ),
    },
    "connection.description": {
        "external_management": _ui_text(
            title="Внешний управляющий клиент: вызывает API FWRouter, но не является целью маршрутизации.",
            title_en="External management client: calls the FWRouter API, but is not a routing target.",
        ),
        "external_vpn_module": _ui_text(
            title=(
                "Внешний VPN-модуль выхода: runtime управляется пользователем и может стать VPN-провайдером "
                "после включения поддержки в dataplane."
            ),
            title_en=(
                "External VPN egress module: user-managed runtime that can become a VPN provider "
                "after dataplane support is enabled."
            ),
        ),
        "external_network_source": _ui_text(
            title="Внешний источник клиентов: пользовательский ingress/network inventory provider.",
            title_en="External client source: user-managed ingress/network inventory provider.",
        ),
        "display_only": _ui_text(title="Внешняя система только для отображения.", title_en="Display-only external system."),
    },
    "connection.api_example": {
        "switch_vpn_auto_server": _ui_text(title="Переключить сервер VPN-auto", title_en="Switch VPN-auto server"),
        "clear_fixed_global_server": _ui_text(
            title="Сбросить фиксированный глобальный сервер",
            title_en="Clear fixed global server",
        ),
    },
    "server.virtual": {
        "xray_vpn_auto": _ui_text(title="Автоматический выбор", title_en="Automatic selection"),
        "custom_https_proxy": _ui_text(title="Прокси (не заходить)", title_en="Proxy (do not enter)"),
    },
}

UNKNOWN_TEXT_FALLBACKS = {
    "watchdog.status": _ui_text(
        title="Неизвестный статус watchdog",
        title_en="Unknown watchdog status",
        reason="UI пока не знает этот машинный статус; код оставлен в деталях для диагностики.",
        reason_en="The UI does not know this machine status yet; the raw code is kept in details for diagnostics.",
    ),
    "watchdog.action": _ui_text(title="Неизвестное действие watchdog", title_en="Unknown watchdog action"),
    "error.code": _ui_text(
        reason="Ошибка без локализованного пояснения; код оставлен в деталях для диагностики.",
        reason_en="Error without a localized explanation; the code is kept in details for diagnostics.",
    ),
    "traffic.metric": _ui_text(title="Трафик", title_en="Traffic"),
    "inventory.activity": _ui_text(title="Нет данных активности", title_en="No activity data"),
    "display.system.title": _ui_text(title="Внешняя система", title_en="External system"),
    "display.system.description": _ui_text(title="Внешняя система.", title_en="External system."),
    "connection.description": _ui_text(title="Внешнее подключение.", title_en="External connection."),
    "connection.api_example": _ui_text(title="Пример API", title_en="API example"),
    "server.virtual": _ui_text(title="Виртуальный сервер", title_en="Virtual server"),
}


def _ui_text_entry(namespace: str, key: Any) -> dict[str, Any] | None:
    raw = str(key or "").strip()
    if not raw:
        return None
    namespace_entries = UI_TEXT_REGISTRY.get(namespace)
    if not isinstance(namespace_entries, dict):
        return None
    entry = namespace_entries.get(raw)
    return entry if isinstance(entry, dict) else None


def _ui_text_value(entry: dict[str, Any], field: str, locale: Any = None) -> str | None:
    normalized_locale = _normalize_ui_text_locale(locale)
    localized = entry.get(f"{field}_i18n")
    if isinstance(localized, dict):
        for candidate in (normalized_locale, DEFAULT_UI_TEXT_LOCALE, "en"):
            value = localized.get(candidate)
            if isinstance(value, str) and value.strip():
                return value.strip()
    value = entry.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _ui_text_title(namespace: str, key: Any, *, locale: Any = None) -> str | None:
    entry = _ui_text_entry(namespace, key)
    if entry is not None:
        title = _ui_text_value(entry, "title", locale)
        if title:
            return title
    fallback = UNKNOWN_TEXT_FALLBACKS.get(namespace)
    if isinstance(fallback, dict):
        title = _ui_text_value(fallback, "title", locale)
        if title:
            return title
    return None


def _ui_text_reason(namespace: str, key: Any, *, locale: Any = None) -> str | None:
    entry = _ui_text_entry(namespace, key)
    if entry is not None:
        reason = _ui_text_value(entry, "reason", locale)
        if reason:
            return reason
    fallback = UNKNOWN_TEXT_FALLBACKS.get(namespace)
    if isinstance(fallback, dict):
        reason = _ui_text_value(fallback, "reason", locale)
        if reason:
            return reason
    return None
