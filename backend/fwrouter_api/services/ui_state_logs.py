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

WATCHDOG_STATUS_LABELS = {
    "paused_signal_unavailable": "Нет свежего сигнала трафика",
    "traffic_failure_pending": "Сбой трафика еще подтверждается",
    "failover_candidate_found": "Кандидат найден, смена не применялась",
    "fail_open_direct_recommended": "Рабочий кандидат не найден",
    "runtime_convergence_failed": "Runtime маршрутизации нездоров",
    "runtime_unavailable": "VPN runtime недоступен",
    "external_runtime_active": "Активен внешний VPN runtime",
    "external_runtime_failover_unavailable": "У внешнего VPN runtime нет failover",
    "needs_initial_auto_selection": "Нет валидного активного auto-сервера",
    "scheduler_failed": "Фоновая проверка упала",
    "manual_selection": "Включен ручной выбор сервера",
    "failover_cooldown": "Failover на паузе после недавней смены",
}

WATCHDOG_REASON_LABELS = {
    "paused_signal_unavailable": (
        "Нет свежего достоверного снимка счетчиков трафика, поэтому автоматическая смена "
        "подавлена, чтобы не переключать сервер по ложному сигналу."
    ),
    "traffic_failure_pending": (
        "Замечен исходящий VPN-трафик без ответных байтов; watchdog ждет повторный свежий "
        "снимок перед failover."
    ),
    "failover_candidate_found": "Проверка нашла рабочий сервер, но текущий запуск был без права применять смену.",
    "fail_open_direct_recommended": (
        "Сбой VPN-трафика подтвержден, но среди кандидатов не найден рабочий сервер для автоматической смены."
    ),
    "runtime_convergence_failed": (
        "Сначала нужно восстановить dataplane/runtime; смена VPN-сервера могла бы скрыть основную проблему."
    ),
    "runtime_unavailable": "Активный VPN runtime не готов или не отвечает, поэтому сервер не менялся.",
    "external_runtime_active": "FWRouter видит внешний VPN runtime и не управляет его selector напрямую.",
    "external_runtime_failover_unavailable": (
        "Сбой трафика подтвержден, но внешний VPN runtime не предоставил endpoint для автоматического failover."
    ),
    "needs_initial_auto_selection": "В режиме VPN-auto нет валидного активного сервера; нужен первичный выбор.",
    "scheduler_failed": "Внутренняя ошибка остановила один шаг фоновой проверки.",
    "manual_selection": "Сбой трафика подтвержден, но выбран ручной режим сервера, поэтому автоматика не переключает.",
    "failover_cooldown": "Сбой трафика подтвержден, но после недавней смены еще действует cooldown.",
}

WATCHDOG_ACTION_LABELS = {
    "none": "Сервер не менялся",
    "dry_run_only": "Только проверка, без применения",
    "switch_vpn_auto": "Выбран новый VPN-auto сервер",
    "fail_open_direct_recommended": "Нужна ручная проверка или временный DIRECT",
}

ERROR_REASON_LABELS = {
    "RULES_VALIDATION_FAILED": "В правилах маршрутизации есть некорректная строка или неподдерживаемый формат.",
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


def _watchdog_status_reason(status: Any) -> str | None:
    raw = str(status or "").strip()
    if not raw:
        return None
    return WATCHDOG_STATUS_LABELS.get(raw, raw)


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
    return WATCHDOG_ACTION_LABELS.get(raw, raw)


def _watchdog_message_for_event(event_type: str, details: dict[str, Any]) -> str | None:
    if event_type == "watchdog_scheduler_failed":
        return "Watchdog не выполнил фоновую проверку"

    if event_type != "watchdog_switch_suppressed":
        return None

    status = _watchdog_event_status(event_type, details)
    label = _watchdog_status_reason(status)
    return f"Watchdog не стал менять VPN-сервер: {label}" if label else "Watchdog не стал менять VPN-сервер"


def _localized_error_reason(details: dict[str, Any]) -> str | None:
    code = str(details.get("code") or details.get("error_code") or "").strip()
    if code and code in ERROR_REASON_LABELS:
        return ERROR_REASON_LABELS[code]
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
        result["Статус"] = _watchdog_status_reason(status) or event_type
        reason = WATCHDOG_REASON_LABELS.get(status)
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
    if level in {"warning", "error"}:
        return True

    event_type = str(event.get("event_type") or "")
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
            "message": _localized_log_message(event, technical=True),
            "details": _operator_log_details(event, technical=True),
            "ui_visible": _log_event_ui_visible(event, technical=True),
        }
    return {
        "event_id": event.get("event_id"),
        "created_at": event.get("created_at"),
        "level": event.get("level"),
        "event_type": event.get("event_type"),
        "subject_id": event.get("subject_id"),
        "message": _localized_log_message(event),
        "details": _operator_log_details(event),
        "ui_visible": _log_event_ui_visible(event),
    }
