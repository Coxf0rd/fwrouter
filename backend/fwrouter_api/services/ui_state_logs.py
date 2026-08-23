from __future__ import annotations

from typing import Any


UI_HIDDEN_OPERATIONAL_EVENT_TYPES = {
    "apply_completed",
    "apply_dry_run_completed",
    "control_plane_maintenance_completed",
    "xray_binding_materialized",
}

UI_OPERATIONAL_EVENT_MESSAGES = {
    "mutation_set_global_mode_success": "Режим роутера применен",
    "mutation_set_global_mode_failed": "Не удалось применить режим роутера",
    "mutation_set_selective_default_success": "Правило по умолчанию для selective сохранено",
    "mutation_set_selective_default_failed": "Не удалось сохранить правило selective",
    "mutation_set_global_server_mode_success": "Режим выбора сервера применен",
    "mutation_set_global_server_mode_failed": "Не удалось применить режим выбора сервера",
    "mutation_set_subject_admin_mode_success": "Режим клиента применен",
    "mutation_set_subject_admin_mode_failed": "Не удалось применить режим клиента",
    "mutation_set_subject_user_mode_success": "Пользовательский режим клиента применен",
    "mutation_set_subject_user_mode_failed": "Не удалось применить пользовательский режим клиента",
    "mutation_clear_subject_user_mode_success": "Клиент возвращен к глобальному режиму",
    "mutation_clear_subject_user_mode_failed": "Не удалось вернуть клиента к глобальному режиму",
    "mutation_set_subject_server_override_success": "Сервер клиента выбран",
    "mutation_set_subject_server_override_failed": "Не удалось выбрать сервер клиента",
    "mutation_clear_subject_server_override_success": "Индивидуальный сервер клиента сброшен",
    "mutation_clear_subject_server_override_failed": "Не удалось сбросить сервер клиента",
    "mutation_repair_global_direct_runtime_success": "Маршрутизация восстановлена",
    "mutation_repair_global_direct_runtime_failed": "Не удалось восстановить маршрутизацию",
    "mutation_apply_manual_rules_success": "Правила маршрутизации применены",
    "mutation_apply_manual_rules_failed": "Не удалось применить правила маршрутизации",
    "routing_live_drift_detected": "Текущая маршрутизация отличается от сохраненного состояния",
    "routing_artifact_drift_detected": "Сохраненная конфигурация маршрутизации не совпадает с текущим состоянием",
    "manual_rules_apply_completed": "Правила маршрутизации применены",
    "manual_rules_apply_failed": "Не удалось применить правила маршрутизации",
    "rules_full_update_succeeded": "Re-filter обновлен и применен",
    "rules_full_update_noop": "Re-filter уже актуален",
    "rules_full_update_failed": "Не удалось применить обновленный Re-filter",
    "rules_full_update_fetch_failed": "Не удалось скачать Re-filter",
    "rules_full_update_policy_failed": "Источник Re-filter не прошел проверку",
    "rules_full_update_dnsmasq_failed": "Re-filter обновлен, но dnsmasq не применил правила",
    "rules_manual_update_dnsmasq_failed": "Правила сохранены, но dnsmasq не применил обновление",
    "subscription_refresh_completed": "Подписка обновлена",
    "subscription_refresh_failed": "Не удалось обновить подписку",
    "runtime_convergence_repaired": "Автоматика восстановила runtime маршрутизации",
    "runtime_convergence_failed": "Автоматика не смогла восстановить runtime маршрутизации",
    "vpn_auto_server_switched": "Auto VPN-сервер выбран",
    "global_fixed_server_applied": "Глобальный VPN-сервер выбран",
    "global_fixed_server_cleared": "Глобальный VPN-сервер сброшен",
    "global_fixed_server_expired": "Глобальный VPN-сервер сброшен по TTL",
    "watchdog_repair_completed": "Автоматика восстановила маршрутизацию",
    "watchdog_repair_failed": "Автоматика не смогла восстановить маршрутизацию",
    "traffic_accounting_completed": "Учет трафика обновлен",
    "traffic_accounting_failed": "Ошибка учета трафика",
    "core_bypass_enabled": "Включен обход FWRouter",
    "core_bypass_disabled": "Обход FWRouter выключен",
}

UI_TECHNICAL_EVENT_MESSAGES = {
    "startup_mihomo_selector_restored": "В runtime восстановлен выбранный VPN-сервер",
    "startup_live_routing_recovered": "При запуске восстановлена live-маршрутизация",
    "routing_live_drift_detected": "Текущая маршрутизация отличается от сохраненного состояния",
    "routing_artifact_drift_detected": "Сохраненная конфигурация маршрутизации не совпадает с текущим состоянием",
    "watchdog_scheduler_failed": "Watchdog не выполнил фоновую проверку",
    "watchdog_switch_suppressed": "Watchdog не стал менять VPN-сервер",
}

UI_LOG_DETAIL_LABELS = {
    "active_auto_server_id": "Активный сервер",
    "affected_subject_ids": "Затронутые клиенты",
    "affected_subject_ids_truncated": "Еще клиентов",
    "applied_mode": "Примененный режим",
    "apply_id": "ID применения",
    "apply_state": "Состояние применения",
    "code": "Код",
    "dataplane_capability": "Dataplane-контур",
    "desired_mode": "Желаемый режим",
    "enforcement_level": "Уровень защиты",
    "expected_mode": "Ожидался режим",
    "intent": "Операция",
    "job_id": "ID задачи",
    "live_mode": "Live-режим",
    "message": "Сообщение",
    "active_after": "После",
    "active_before": "До",
    "fixed_server_until": "Действует до",
    "mode": "Режим",
    "owned_table": "Таблица nftables",
    "selected_server_id": "Сервер",
    "selected_server_name": "Название",
    "reason": "Причина",
    "requested_by": "Инициатор",
    "runtime_state_unchanged": "Live-состояние не менялось",
    "stage": "Этап",
    "traffic_enforcement_guaranteed": "Защита трафика подтверждена",
}

MODE_LABELS = {
    "direct": "DIRECT",
    "selective": "SELECTIVE",
    "vpn": "VPN",
    "global": "Глобальный",
    "disabled": "Отключен",
    "auto": "Авто",
    "fixed": "Фиксированный",
}

UI_TEXT_REGISTRY = {
    "watchdog.status": {
        "paused_signal_unavailable": {
            "title": "Нет свежего сигнала трафика",
            "reason": (
                "Нет свежего достоверного снимка счетчиков трафика, поэтому автоматическая смена "
                "подавлена, чтобы не переключать сервер по ложному сигналу."
            ),
        },
        "traffic_failure_pending": {
            "title": "Сбой трафика еще подтверждается",
            "reason": (
                "Замечен исходящий VPN-трафик без ответных байтов; watchdog ждет повторный свежий "
                "снимок перед failover."
            ),
        },
        "failover_candidate_found": {
            "title": "Кандидат найден, смена не применялась",
            "reason": "Проверка нашла рабочий сервер, но текущий запуск был без права применять смену.",
        },
        "fail_open_direct_recommended": {
            "title": "Рабочий кандидат не найден",
            "reason": (
                "Сбой VPN-трафика подтвержден, но среди кандидатов не найден рабочий сервер "
                "для автоматической смены."
            ),
        },
        "runtime_convergence_failed": {
            "title": "Runtime маршрутизации нездоров",
            "reason": (
                "Сначала нужно восстановить dataplane/runtime; смена VPN-сервера могла бы скрыть "
                "основную проблему."
            ),
        },
        "runtime_unavailable": {
            "title": "VPN runtime недоступен",
            "reason": "Активный VPN runtime не готов или не отвечает, поэтому сервер не менялся.",
        },
        "external_runtime_active": {
            "title": "Активен внешний VPN runtime",
            "reason": "FWRouter видит внешний VPN runtime и не управляет его selector напрямую.",
        },
        "external_runtime_failover_unavailable": {
            "title": "У внешнего VPN runtime нет failover",
            "reason": (
                "Сбой трафика подтвержден, но внешний VPN runtime не предоставил endpoint "
                "для автоматического failover."
            ),
        },
        "needs_initial_auto_selection": {
            "title": "Нет валидного активного auto-сервера",
            "reason": "В режиме VPN-auto нет валидного активного сервера; нужен первичный выбор.",
        },
        "scheduler_failed": {
            "title": "Фоновая проверка упала",
            "reason": "Внутренняя ошибка остановила один шаг фоновой проверки.",
        },
        "manual_selection": {
            "title": "Включен ручной выбор сервера",
            "reason": (
                "Сбой трафика подтвержден, но выбран ручной режим сервера, поэтому автоматика "
                "не переключает."
            ),
        },
        "failover_cooldown": {
            "title": "Failover на паузе после недавней смены",
            "reason": "Сбой трафика подтвержден, но после недавней смены еще действует cooldown.",
        },
        "active_quality_degraded_traffic_healthy": {
            "title": "Проверка сервера нестабильна, но VPN-трафик отвечает",
            "reason": (
                "Delay-check текущего сервера нестабилен, но есть ответный VPN-трафик; "
                "watchdog не меняет сервер по одному техническому сигналу."
            ),
        },
    },
    "watchdog.action": {
        "none": {"title": "Сервер не менялся"},
        "dry_run_only": {"title": "Только проверка, без применения"},
        "switch_vpn_auto": {"title": "Выбран новый VPN-auto сервер"},
        "fail_open_direct_recommended": {"title": "Нужна ручная проверка или временный DIRECT"},
    },
    "error.code": {
        "RULES_VALIDATION_FAILED": {
            "reason": "В правилах маршрутизации есть некорректная строка или неподдерживаемый формат."
        },
    },
    "traffic.metric": {
        "direct_rx_bytes": {"title": "DIRECT вход"},
        "direct_tx_bytes": {"title": "DIRECT выход"},
        "vpn_rx_bytes": {"title": "VPN вход"},
        "vpn_tx_bytes": {"title": "VPN выход"},
    },
    "inventory.activity": {
        "profile_seen_24h": {"title": "Профиль запрашивался за 24ч"},
        "traffic_seen": {"title": "Был трафик"},
        "runtime_active": {"title": "Runtime активен"},
        "stale_seen": {"title": "Нет свежей активности"},
        "unknown": {"title": "Нет данных активности"},
    },
    "display.system.title": {
        "lan": {"title": "Lan / Core"},
        "external_network_source": {"title": "Внешняя сеть"},
        "vless_client": {"title": "Vless"},
        "vpn_runtime": {"title": "VPN runtime"},
        "docker": {"title": "Docker"},
        "host": {"title": "Службы хоста"},
    },
    "display.system.description": {
        "lan": {"title": "Клиенты LAN и routing core FWRouter."},
        "external_network_source": {
            "title": "Внешний источник клиентов; FWRouter показывает его только когда есть реальные найденные клиенты."
        },
        "vless_client": {"title": "Клиентское ядро Vless; конкретная реализация хранится отдельно."},
        "vpn_runtime": {"title": "VPN/dataplane-адаптер FWRouter; конкретная реализация хранится отдельно."},
        "docker": {"title": "Отображение контейнеров; это не управляемый runtime-модуль."},
        "host": {"title": "Отображение служб хоста и systemd."},
        "external_network_discovered": {"title": "Внешний сетевой источник найден в inventory клиентов."},
    },
    "connection.description": {
        "external_management": {"title": "Внешний управляющий клиент: вызывает API FWRouter, но не является целью маршрутизации."},
        "external_vpn_module": {
            "title": (
                "Внешний VPN-модуль выхода: runtime управляется пользователем и может стать VPN-провайдером "
                "после включения поддержки в dataplane."
            )
        },
        "external_network_source": {"title": "Внешний источник клиентов: пользовательский ingress/network inventory provider."},
        "display_only": {"title": "Внешняя система только для отображения."},
    },
    "connection.api_example": {
        "switch_vpn_auto_server": {"title": "Переключить сервер VPN-auto"},
        "clear_fixed_global_server": {"title": "Сбросить фиксированный глобальный сервер"},
    },
    "server.virtual": {
        "xray_vpn_auto": {"title": "Автоматический выбор"},
        "custom_https_proxy": {"title": "Прокси (не заходить)"},
    },
}

UNKNOWN_TEXT_FALLBACKS = {
    "watchdog.status": {
        "title": "Неизвестный статус watchdog",
        "reason": "UI пока не знает этот машинный статус; код оставлен в деталях для диагностики.",
    },
    "watchdog.action": {"title": "Неизвестное действие watchdog"},
    "error.code": {"reason": "Ошибка без локализованного пояснения; код оставлен в деталях для диагностики."},
    "traffic.metric": {"title": "Трафик"},
    "inventory.activity": {"title": "Нет данных активности"},
    "display.system.title": {"title": "Внешняя система"},
    "display.system.description": {"title": "Внешняя система."},
    "connection.description": {"title": "Внешнее подключение."},
    "connection.api_example": {"title": "Пример API"},
    "server.virtual": {"title": "Виртуальный сервер"},
}


def _truncate_scalar(value: Any, *, limit: int = 240) -> Any:
    if isinstance(value, dict):
        return f"{{{len(value)} fields}}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    if isinstance(value, str):
        text = value.strip()
        return text if len(text) <= limit else f"{text[:limit]}..."
    return value


def _mode_label(value: Any) -> str:
    raw = str(value or "").strip()
    return MODE_LABELS.get(raw.lower(), raw or "—")


def _yes_no(value: Any) -> str:
    return "Да" if bool(value) else "Нет"


def _count_label(value: Any, noun: str) -> str | None:
    if not isinstance(value, list):
        return None
    count = len(value)
    if count == 0:
        return None
    return f"{count} {noun}"


def _compact_error_message(details: dict[str, Any]) -> str | None:
    for key in ("message", "error_message"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _ui_text_entry(namespace: str, key: Any) -> dict[str, str] | None:
    raw = str(key or "").strip()
    if not raw:
        return None
    namespace_entries = UI_TEXT_REGISTRY.get(namespace)
    if not isinstance(namespace_entries, dict):
        return None
    entry = namespace_entries.get(raw)
    return entry if isinstance(entry, dict) else None


def _ui_text_title(namespace: str, key: Any) -> str | None:
    entry = _ui_text_entry(namespace, key)
    if entry is not None:
        title = str(entry.get("title") or "").strip()
        if title:
            return title
    fallback = UNKNOWN_TEXT_FALLBACKS.get(namespace)
    if isinstance(fallback, dict):
        title = str(fallback.get("title") or "").strip()
        if title:
            return title
    return None


def _ui_text_reason(namespace: str, key: Any) -> str | None:
    entry = _ui_text_entry(namespace, key)
    if entry is not None:
        reason = str(entry.get("reason") or "").strip()
        if reason:
            return reason
    fallback = UNKNOWN_TEXT_FALLBACKS.get(namespace)
    if isinstance(fallback, dict):
        reason = str(fallback.get("reason") or "").strip()
        if reason:
            return reason
    return None


def _watchdog_status_title(status: Any) -> str | None:
    raw = str(status or "").strip()
    if not raw:
        return None
    return _ui_text_title("watchdog.status", raw)


def _watchdog_status_reason(status: Any) -> str | None:
    raw = str(status or "").strip()
    if not raw:
        return None
    return _ui_text_reason("watchdog.status", raw)


def _watchdog_event_status(event_type: str, details: dict[str, Any]) -> str:
    status = str(details.get("status") or "").strip()
    if status:
        return status
    if event_type == "watchdog_scheduler_failed":
        return "scheduler_failed"
    return event_type


def _watchdog_action_label(action: Any) -> str | None:
    raw = str(action or "").strip()
    if not raw:
        return None
    return _ui_text_title("watchdog.action", raw)


def _log_event_category(event: dict[str, Any], *, technical: bool = False) -> str:
    event_type = str(event.get("event_type") or "").lower()
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    requested_by = str(details.get("requested_by") or "").lower()
    reason = str(details.get("reason") or "").lower()
    component = str(event.get("component") or "").lower()

    if component == "watchdog" or event_type.startswith("watchdog_") or event_type.startswith("vpn_watchdog_"):
        return "watchdog"
    if event_type == "vpn_auto_server_switched" and (
        "watchdog" in requested_by or reason.startswith("watchdog_failover:")
    ):
        return "watchdog"
    if "rule" in event_type:
        return "routing"
    if "server" in event_type or "vpn_auto" in event_type or "mihomo" in event_type:
        return "server"
    if "routing" in event_type or "subject_mode" in event_type:
        return "routing"
    if "subscription" in event_type or "settings" in event_type:
        return "settings"
    if str(event.get("level") or "").lower() == "error":
        return "error"
    if event.get("subject_id"):
        return "user"
    return "system" if technical else "system"


def _watchdog_message_for_event(event_type: str, details: dict[str, Any]) -> str | None:
    if event_type == "watchdog_scheduler_failed":
        return "Watchdog не выполнил фоновую проверку"

    if event_type != "watchdog_switch_suppressed":
        return None

    status = _watchdog_event_status(event_type, details)
    label = _watchdog_status_title(status)
    return f"Watchdog не стал менять VPN-сервер: {label}" if label else "Watchdog не стал менять VPN-сервер"


def _localized_error_reason(details: dict[str, Any]) -> str | None:
    code = str(details.get("code") or details.get("error_code") or "").strip()
    if code:
        reason = _ui_text_reason("error.code", code)
        if reason:
            return reason
    return _compact_error_message(details)


def _operator_log_details(event: dict[str, Any], *, technical: bool = False) -> dict[str, Any]:
    details = event.get("details")
    if not isinstance(details, dict):
        details = {}

    event_type = str(event.get("event_type") or "")
    is_watchdog_event = event_type.startswith("watchdog_") or event_type.startswith("vpn_watchdog_")
    level = str(event.get("level") or "info").lower()
    result: dict[str, Any] = {}

    routing = details.get("routing") if isinstance(details.get("routing"), dict) else {}
    affected = details.get("affected_subject_ids")
    affected_count = _count_label(affected, "клиентов")

    if event_type.startswith("mutation_set_global_mode_"):
        if routing:
            mode = str(routing.get("applied_mode") or routing.get("desired_mode") or "").strip().lower()
            result["Режим"] = _mode_label(mode)
            if mode == "selective" and routing.get("selective_default"):
                result["По умолчанию"] = _mode_label(routing.get("selective_default"))
            if routing.get("active_auto_server_id"):
                result["Активный сервер"] = routing.get("active_auto_server_id")
        if affected_count:
            result["Затронуто"] = affected_count
        if "traffic_enforcement_guaranteed" in details:
            result["Защита подтверждена"] = _yes_no(details.get("traffic_enforcement_guaranteed"))

    elif event_type.startswith("mutation_set_subject_") or event_type.startswith("mutation_clear_subject_"):
        subject = details.get("subject") if isinstance(details.get("subject"), dict) else {}
        result["Клиент"] = event.get("subject_id") or subject.get("subject_id") or "—"
        if subject.get("display_name") or subject.get("alias"):
            result["Имя"] = subject.get("alias") or subject.get("display_name")
        effective = subject.get("effective_state") if isinstance(subject.get("effective_state"), dict) else {}
        mode = effective.get("effective_mode") or subject.get("applied_mode") or subject.get("desired_mode")
        if mode:
            result["Режим"] = _mode_label(mode)

    elif event_type in {"routing_live_drift_detected", "routing_artifact_drift_detected"}:
        if details.get("expected_mode"):
            result["Ожидалось"] = _mode_label(details.get("expected_mode"))
        if details.get("live_mode"):
            result["Live"] = _mode_label(details.get("live_mode"))
        if details.get("code"):
            result["Код"] = details.get("code")
        if details.get("requested_by"):
            result["Инициатор"] = details.get("requested_by")

    elif event_type.startswith("startup_"):
        persisted = details.get("persisted_intent") if isinstance(details.get("persisted_intent"), dict) else {}
        if persisted.get("intended_mode"):
            result["Режим"] = _mode_label(persisted.get("intended_mode"))
        if details.get("active_auto_server_id"):
            result["Активный сервер"] = details.get("active_auto_server_id")
        result["Восстановлено"] = _yes_no(details.get("recovered", details.get("restored", True)))

    elif event_type == "vpn_auto_server_switched":
        if details.get("requested_by"):
            result["Инициатор"] = details.get("requested_by")
        if details.get("active_before"):
            result["До"] = details.get("active_before")
        if details.get("active_after"):
            result["После"] = details.get("active_after")
        if details.get("selected_server_name") or details.get("selected_server_id"):
            result["Сервер"] = details.get("selected_server_name") or details.get("selected_server_id")
        ping = details.get("selected_ping") if isinstance(details.get("selected_ping"), dict) else {}
        if ping.get("last_ping_ms") is not None:
            result["Ping"] = f"{ping.get('last_ping_ms')} ms"

    elif event_type in {
        "global_fixed_server_applied",
        "global_fixed_server_cleared",
        "global_fixed_server_expired",
    }:
        if details.get("requested_by"):
            result["Инициатор"] = details.get("requested_by")
        if details.get("active_before"):
            result["До"] = details.get("active_before")
        if details.get("active_after"):
            result["После"] = details.get("active_after")
        if details.get("server_id") or details.get("desired_fixed_server_id"):
            result["Сервер"] = details.get("server_id") or details.get("desired_fixed_server_id")
        if details.get("fixed_server_until"):
            result["Действует до"] = details.get("fixed_server_until")

    elif is_watchdog_event:
        status = _watchdog_event_status(event_type, details)
        result["Статус"] = _watchdog_status_title(status) or "Неизвестный статус watchdog"
        if status and _ui_text_entry("watchdog.status", status) is None:
            result["Код статуса"] = status
        reason = _watchdog_status_reason(status)
        if reason:
            result["Причина"] = reason
        if details.get("active_server_id"):
            result["Активный сервер"] = details.get("active_server_id")
        action = _watchdog_action_label(details.get("action"))
        if action:
            result["Что сделано"] = action
        if details.get("reason"):
            result["Инициатор"] = details.get("reason")
        if details.get("allow_switch") is not None:
            result["Смена разрешена"] = _yes_no(details.get("allow_switch"))

        traffic_signal = details.get("traffic_signal") if isinstance(details.get("traffic_signal"), dict) else {}
        if traffic_signal:
            if traffic_signal.get("last_collected_at"):
                result["Снимок трафика"] = traffic_signal.get("last_collected_at")
            if traffic_signal.get("observed") is not None:
                result["VPN-трафик замечен"] = _yes_no(traffic_signal.get("observed"))
            if traffic_signal.get("response_observed") is not None:
                result["Ответный трафик"] = _yes_no(traffic_signal.get("response_observed"))

        confirmation = (
            details.get("traffic_failure_confirmation")
            if isinstance(details.get("traffic_failure_confirmation"), dict)
            else {}
        )
        if confirmation:
            if confirmation.get("reason"):
                result["Подтверждение"] = _truncate_scalar(confirmation.get("reason"))
            if confirmation.get("elapsed_seconds") is not None:
                result["Ожидание"] = f"{confirmation.get('elapsed_seconds')}s"

        selector = details.get("selector") if isinstance(details.get("selector"), dict) else {}
        if selector:
            if selector.get("active_after"):
                result["Кандидат"] = selector.get("active_after")
            if selector.get("error_message"):
                result["Причина"] = _truncate_scalar(selector.get("error_message"), limit=240)

        if details.get("error_code") and "Код" not in result:
            result["Код"] = details.get("error_code")
        error_message = _compact_error_message(details)
        if error_message and "Причина" not in result:
            result["Причина"] = _truncate_scalar(error_message, limit=240)

    if level in {"warning", "error"} and not is_watchdog_event:
        if details.get("code") and "Код" not in result:
            result["Код"] = details.get("code")
        if details.get("error_code") and "Код" not in result:
            result["Код"] = details.get("error_code")
        error_message = _localized_error_reason(details)
        if error_message:
            result["Причина"] = _truncate_scalar(error_message, limit=240)

    if not result:
        for key in ("code", "message", "requested_by", "active_auto_server_id"):
            value = details.get(key)
            if value in (None, "", [], {}):
                continue
            result[UI_LOG_DETAIL_LABELS.get(key, key)] = _truncate_scalar(value)

    return result


def _summarize_log_details(details: Any) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    summary: dict[str, Any] = {}
    for index, (key, value) in enumerate(details.items()):
        if index >= 8:
            summary["_truncated"] = f"{len(details) - 8} more fields"
            break
        if isinstance(value, dict):
            nested: dict[str, Any] = {}
            for nested_index, (nested_key, nested_value) in enumerate(value.items()):
                if nested_index >= 5:
                    nested["_truncated"] = f"Скрыто полей: {len(value) - 5}"
                    break
                nested[nested_key] = _truncate_scalar(nested_value)
            summary[key] = nested
        elif isinstance(value, list):
            summary[key] = [_truncate_scalar(item) for item in value[:5]]
            if len(value) > 5:
                summary[f"{key}_truncated"] = f"Скрыто элементов: {len(value) - 5}"
        else:
            summary[key] = _truncate_scalar(value)
    return summary


def _localized_log_details(details: Any) -> dict[str, Any]:
    summarized = _summarize_log_details(details)
    localized: dict[str, Any] = {}
    for key, value in summarized.items():
        if key == "_truncated":
            localized["Скрыто полей"] = value
            continue
        localized[UI_LOG_DETAIL_LABELS.get(str(key), str(key))] = value
    return localized


def _localized_log_message(event: dict[str, Any], *, technical: bool = False) -> str:
    event_type = str(event.get("event_type") or "")
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    if event_type.startswith("watchdog_") or event_type.startswith("vpn_watchdog_"):
        watchdog_message = _watchdog_message_for_event(event_type, details)
        if watchdog_message:
            return watchdog_message
    mapping = UI_TECHNICAL_EVENT_MESSAGES if technical else UI_OPERATIONAL_EVENT_MESSAGES
    localized = mapping.get(event_type)
    if localized:
        return localized
    message = str(event.get("message") or "").strip()
    if message:
        return str(_truncate_scalar(message, limit=320))
    return event_type or "Событие"


def _log_event_ui_visible(event: dict[str, Any], *, technical: bool = False) -> bool:
    level = str(event.get("level") or "info").lower()
    event_type = str(event.get("event_type") or "")
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    if (
        event_type == "watchdog_switch_suppressed"
        and str(details.get("status") or "").strip() == "paused_signal_unavailable"
    ):
        return False

    if level in {"warning", "error"}:
        return True

    if technical:
        return event_type in UI_TECHNICAL_EVENT_MESSAGES
    if event_type in UI_HIDDEN_OPERATIONAL_EVENT_TYPES:
        return False
    if event_type.startswith("mutation_"):
        return True
    return event_type in UI_OPERATIONAL_EVENT_MESSAGES


def _summarize_log_event(event: dict[str, Any], *, technical: bool = False) -> dict[str, Any]:
    if technical:
        return {
            "timestamp": event.get("timestamp"),
            "level": event.get("level"),
            "component": event.get("component"),
            "event_type": event.get("event_type"),
            "category": _log_event_category(event, technical=True),
            "message": _localized_log_message(event, technical=True),
            "details": _operator_log_details(event, technical=True),
            "ui_visible": _log_event_ui_visible(event, technical=True),
        }
    return {
        "event_id": event.get("event_id"),
        "created_at": event.get("created_at"),
        "level": event.get("level"),
        "event_type": event.get("event_type"),
        "category": _log_event_category(event),
        "subject_id": event.get("subject_id"),
        "message": _localized_log_message(event),
        "details": _operator_log_details(event),
        "ui_visible": _log_event_ui_visible(event),
    }
