from __future__ import annotations

from typing import Any

from fwrouter_api.services.ui_text import (
    DEFAULT_UI_TEXT_LOCALE,
    _normalize_ui_text_locale,
    _ui_text_entry,
    _ui_text_reason,
    _ui_text_title,
)


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

UI_LOG_DETAIL_LABELS_I18N = {
    "active_server": {"ru": "Активный сервер", "en": "Active server"},
    "affected": {"ru": "Затронуто", "en": "Affected"},
    "after": {"ru": "После", "en": "After"},
    "before": {"ru": "До", "en": "Before"},
    "candidate": {"ru": "Кандидат", "en": "Candidate"},
    "client": {"ru": "Клиент", "en": "Client"},
    "code": {"ru": "Код", "en": "Code"},
    "confirmation": {"ru": "Подтверждение", "en": "Confirmation"},
    "confirmation_window": {"ru": "Окно подтверждения", "en": "Confirmation window"},
    "expected": {"ru": "Ожидалось", "en": "Expected"},
    "fixed_server_until": {"ru": "Действует до", "en": "Valid until"},
    "hidden_fields": {"ru": "Скрыто полей", "en": "Hidden fields"},
    "initiator": {"ru": "Инициатор", "en": "Initiator"},
    "live": {"ru": "Live", "en": "Live"},
    "mode": {"ru": "Режим", "en": "Mode"},
    "name": {"ru": "Имя", "en": "Name"},
    "quality_check": {"ru": "Проверка качества", "en": "Quality check"},
    "reason": {"ru": "Причина", "en": "Reason"},
    "response_traffic": {"ru": "Ответный трафик", "en": "Response traffic"},
    "restored": {"ru": "Восстановлено", "en": "Restored"},
    "server": {"ru": "Сервер", "en": "Server"},
    "selective_default": {"ru": "По умолчанию", "en": "Default"},
    "status": {"ru": "Статус", "en": "Status"},
    "status_code": {"ru": "Код статуса", "en": "Status code"},
    "switch_allowed": {"ru": "Смена разрешена", "en": "Switch allowed"},
    "traffic_seen": {"ru": "VPN-трафик замечен", "en": "VPN traffic seen"},
    "traffic_enforcement_confirmed": {"ru": "Защита подтверждена", "en": "Traffic protection confirmed"},
    "traffic_snapshot": {"ru": "Снимок трафика", "en": "Traffic snapshot"},
    "waiting": {"ru": "Ожидание", "en": "Waiting"},
    "what_done": {"ru": "Что сделано", "en": "Action taken"},
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

MODE_LABELS_I18N = {
    "global": {"ru": "Глобальный", "en": "Global"},
    "disabled": {"ru": "Отключен", "en": "Disabled"},
    "auto": {"ru": "Авто", "en": "Auto"},
    "fixed": {"ru": "Фиксированный", "en": "Fixed"},
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


def _localized_label(labels: dict[str, str], *, locale: Any = None) -> str:
    normalized_locale = _normalize_ui_text_locale(locale)
    return labels.get(normalized_locale) or labels.get(DEFAULT_UI_TEXT_LOCALE) or next(iter(labels.values()))


def _detail_label(key: str, *, locale: Any = None) -> str:
    labels = UI_LOG_DETAIL_LABELS_I18N.get(key)
    if labels:
        return _localized_label(labels, locale=locale)
    return key


def _mode_label(value: Any, *, locale: Any = None) -> str:
    raw = str(value or "").strip()
    labels = MODE_LABELS_I18N.get(raw.lower())
    if labels:
        return _localized_label(labels, locale=locale)
    return MODE_LABELS.get(raw.lower(), raw or "—")


def _yes_no(value: Any, *, locale: Any = None) -> str:
    if _normalize_ui_text_locale(locale) == "en":
        return "Yes" if bool(value) else "No"
    return "Да" if bool(value) else "Нет"


def _count_label(value: Any, noun: str, *, locale: Any = None) -> str | None:
    if not isinstance(value, list):
        return None
    count = len(value)
    if count == 0:
        return None
    if _normalize_ui_text_locale(locale) == "en" and noun == "клиентов":
        return f"{count} clients"
    return f"{count} {noun}"


def _compact_error_message(details: dict[str, Any]) -> str | None:
    for key in ("message", "error_message"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _watchdog_status_title(status: Any, *, locale: Any = None) -> str | None:
    raw = str(status or "").strip()
    if not raw:
        return None
    return _ui_text_title("watchdog.status", raw, locale=locale)


def _watchdog_status_reason(status: Any, *, locale: Any = None) -> str | None:
    raw = str(status or "").strip()
    if not raw:
        return None
    return _ui_text_reason("watchdog.status", raw, locale=locale)


def _watchdog_event_status(event_type: str, details: dict[str, Any]) -> str:
    status = str(details.get("status") or "").strip()
    if status:
        return status
    if event_type == "watchdog_scheduler_failed":
        return "scheduler_failed"
    return event_type


def _watchdog_action_label(action: Any, *, locale: Any = None) -> str | None:
    raw = str(action or "").strip()
    if not raw:
        return None
    return _ui_text_title("watchdog.action", raw, locale=locale)


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


def _watchdog_event_message(base_key: str, label: str | None = None, *, locale: Any = None) -> str:
    base = _ui_text_title("watchdog.event", base_key, locale=locale) or base_key
    return f"{base}: {label}" if label else base


def _watchdog_message_for_event(event_type: str, details: dict[str, Any], *, locale: Any = None) -> str | None:
    if event_type == "watchdog_scheduler_failed":
        return _watchdog_event_message("scheduler_failed", locale=locale)

    status = _watchdog_event_status(event_type, details)
    label = _ui_text_title("watchdog.status", status, locale=locale) if status else None
    if event_type == "watchdog_switch_suppressed":
        return _watchdog_event_message("switch_suppressed", label, locale=locale)
    if event_type == "watchdog_switch_applied":
        return _watchdog_event_message("switch_applied", label, locale=locale)
    if event_type == "watchdog_switch_candidate":
        return _watchdog_event_message("switch_candidate", label, locale=locale)
    return None


def _localized_error_reason(details: dict[str, Any], *, locale: Any = None) -> str | None:
    code = str(details.get("code") or details.get("error_code") or "").strip()
    if code:
        reason = _ui_text_reason("error.code", code, locale=locale)
        if reason:
            return reason
    return _compact_error_message(details)


def _operator_log_details(event: dict[str, Any], *, technical: bool = False, locale: Any = None) -> dict[str, Any]:
    details = event.get("details")
    if not isinstance(details, dict):
        details = {}

    event_type = str(event.get("event_type") or "")
    is_watchdog_event = event_type.startswith("watchdog_") or event_type.startswith("vpn_watchdog_")
    level = str(event.get("level") or "info").lower()
    result: dict[str, Any] = {}

    routing = details.get("routing") if isinstance(details.get("routing"), dict) else {}
    affected = details.get("affected_subject_ids")
    affected_count = _count_label(affected, "клиентов", locale=locale)

    if event_type.startswith("mutation_set_global_mode_"):
        if routing:
            mode = str(routing.get("applied_mode") or routing.get("desired_mode") or "").strip().lower()
            result[_detail_label("mode", locale=locale)] = _mode_label(mode, locale=locale)
            if mode == "selective" and routing.get("selective_default"):
                result[_detail_label("selective_default", locale=locale)] = _mode_label(
                    routing.get("selective_default"),
                    locale=locale,
                )
            if routing.get("active_auto_server_id"):
                result[_detail_label("active_server", locale=locale)] = routing.get("active_auto_server_id")
        if affected_count:
            result[_detail_label("affected", locale=locale)] = affected_count
        if "traffic_enforcement_guaranteed" in details:
            result[_detail_label("traffic_enforcement_confirmed", locale=locale)] = _yes_no(
                details.get("traffic_enforcement_guaranteed"),
                locale=locale,
            )

    elif event_type.startswith("mutation_set_subject_") or event_type.startswith("mutation_clear_subject_"):
        subject = details.get("subject") if isinstance(details.get("subject"), dict) else {}
        result[_detail_label("client", locale=locale)] = event.get("subject_id") or subject.get("subject_id") or "—"
        if subject.get("display_name") or subject.get("alias"):
            result[_detail_label("name", locale=locale)] = subject.get("alias") or subject.get("display_name")
        effective = subject.get("effective_state") if isinstance(subject.get("effective_state"), dict) else {}
        mode = effective.get("effective_mode") or subject.get("applied_mode") or subject.get("desired_mode")
        if mode:
            result[_detail_label("mode", locale=locale)] = _mode_label(mode, locale=locale)

    elif event_type in {"routing_live_drift_detected", "routing_artifact_drift_detected"}:
        if details.get("expected_mode"):
            result[_detail_label("expected", locale=locale)] = _mode_label(details.get("expected_mode"), locale=locale)
        if details.get("live_mode"):
            result[_detail_label("live", locale=locale)] = _mode_label(details.get("live_mode"), locale=locale)
        if details.get("code"):
            result[_detail_label("code", locale=locale)] = details.get("code")
        if details.get("requested_by"):
            result[_detail_label("initiator", locale=locale)] = details.get("requested_by")

    elif event_type.startswith("startup_"):
        persisted = details.get("persisted_intent") if isinstance(details.get("persisted_intent"), dict) else {}
        if persisted.get("intended_mode"):
            result[_detail_label("mode", locale=locale)] = _mode_label(persisted.get("intended_mode"), locale=locale)
        if details.get("active_auto_server_id"):
            result[_detail_label("active_server", locale=locale)] = details.get("active_auto_server_id")
        result[_detail_label("restored", locale=locale)] = _yes_no(
            details.get("recovered", details.get("restored", True)),
            locale=locale,
        )

    elif event_type == "vpn_auto_server_switched":
        if details.get("requested_by"):
            result[_detail_label("initiator", locale=locale)] = details.get("requested_by")
        if details.get("active_before"):
            result[_detail_label("before", locale=locale)] = details.get("active_before")
        if details.get("active_after"):
            result[_detail_label("after", locale=locale)] = details.get("active_after")
        if details.get("selected_server_name") or details.get("selected_server_id"):
            result[_detail_label("server", locale=locale)] = details.get("selected_server_name") or details.get("selected_server_id")
        ping = details.get("selected_ping") if isinstance(details.get("selected_ping"), dict) else {}
        if ping.get("last_ping_ms") is not None:
            result["Ping"] = f"{ping.get('last_ping_ms')} ms"

    elif event_type in {
        "global_fixed_server_applied",
        "global_fixed_server_cleared",
        "global_fixed_server_expired",
    }:
        if details.get("requested_by"):
            result[_detail_label("initiator", locale=locale)] = details.get("requested_by")
        if details.get("active_before"):
            result[_detail_label("before", locale=locale)] = details.get("active_before")
        if details.get("active_after"):
            result[_detail_label("after", locale=locale)] = details.get("active_after")
        if details.get("server_id") or details.get("desired_fixed_server_id"):
            result[_detail_label("server", locale=locale)] = details.get("server_id") or details.get("desired_fixed_server_id")
        if details.get("fixed_server_until"):
            result[_detail_label("fixed_server_until", locale=locale)] = details.get("fixed_server_until")

    elif is_watchdog_event:
        status = _watchdog_event_status(event_type, details)
        status_key = _detail_label("status", locale=locale)
        reason_key = _detail_label("reason", locale=locale)
        code_key = _detail_label("code", locale=locale)
        result[status_key] = _watchdog_status_title(status, locale=locale) or _ui_text_title(
            "watchdog.status",
            status,
            locale=locale,
        )
        if status and _ui_text_entry("watchdog.status", status) is None:
            result[_detail_label("status_code", locale=locale)] = status
        reason = _watchdog_status_reason(status, locale=locale)
        if reason:
            result[reason_key] = reason
        if details.get("active_server_id"):
            result[_detail_label("active_server", locale=locale)] = details.get("active_server_id")
        action = _watchdog_action_label(details.get("action"), locale=locale)
        if action:
            result[_detail_label("what_done", locale=locale)] = action
        if details.get("reason"):
            result[_detail_label("initiator", locale=locale)] = details.get("reason")
        if details.get("allow_switch") is not None:
            result[_detail_label("switch_allowed", locale=locale)] = _yes_no(details.get("allow_switch"), locale=locale)

        traffic_signal = details.get("traffic_signal") if isinstance(details.get("traffic_signal"), dict) else {}
        if traffic_signal:
            if traffic_signal.get("last_collected_at"):
                result[_detail_label("traffic_snapshot", locale=locale)] = traffic_signal.get("last_collected_at")
            if traffic_signal.get("observed") is not None:
                result[_detail_label("traffic_seen", locale=locale)] = _yes_no(traffic_signal.get("observed"), locale=locale)
            if traffic_signal.get("response_observed") is not None:
                result[_detail_label("response_traffic", locale=locale)] = _yes_no(
                    traffic_signal.get("response_observed"),
                    locale=locale,
                )

        confirmation = (
            details.get("traffic_failure_confirmation")
            if isinstance(details.get("traffic_failure_confirmation"), dict)
            else {}
        )
        if confirmation:
            if confirmation.get("reason"):
                result[_detail_label("confirmation", locale=locale)] = _truncate_scalar(confirmation.get("reason"))
            if confirmation.get("elapsed_seconds") is not None:
                result[_detail_label("waiting", locale=locale)] = f"{confirmation.get('elapsed_seconds')}s"

        quality_confirmation = (
            details.get("active_quality_confirmation")
            if isinstance(details.get("active_quality_confirmation"), dict)
            else {}
        )
        if quality_confirmation:
            bad_checks = quality_confirmation.get("bad_checks")
            required_bad_checks = quality_confirmation.get("bad_checks_required")
            if bad_checks is not None and required_bad_checks is not None:
                result[_detail_label("quality_check", locale=locale)] = f"{bad_checks}/{required_bad_checks}"
            age_seconds = quality_confirmation.get("age_seconds")
            confirm_seconds = quality_confirmation.get("confirm_seconds")
            if age_seconds is not None and confirm_seconds is not None:
                result[_detail_label("confirmation_window", locale=locale)] = f"{age_seconds}/{confirm_seconds}s"

        selector = details.get("selector") if isinstance(details.get("selector"), dict) else {}
        if selector:
            if selector.get("active_after"):
                result[_detail_label("candidate", locale=locale)] = selector.get("active_after")
            if selector.get("error_message"):
                result[reason_key] = _truncate_scalar(selector.get("error_message"), limit=240)

        if details.get("error_code") and code_key not in result:
            result[code_key] = details.get("error_code")
        error_message = _compact_error_message(details)
        if error_message and reason_key not in result:
            result[reason_key] = _truncate_scalar(error_message, limit=240)

    if level in {"warning", "error"} and not is_watchdog_event:
        code_key = _detail_label("code", locale=locale)
        if details.get("code") and code_key not in result:
            result[code_key] = details.get("code")
        if details.get("error_code") and code_key not in result:
            result[code_key] = details.get("error_code")
        error_message = _localized_error_reason(details, locale=locale)
        if error_message:
            result[_detail_label("reason", locale=locale)] = _truncate_scalar(error_message, limit=240)

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


def _localized_log_details(details: Any, *, locale: Any = None) -> dict[str, Any]:
    summarized = _summarize_log_details(details)
    localized: dict[str, Any] = {}
    for key, value in summarized.items():
        if key == "_truncated":
            localized[_detail_label("hidden_fields", locale=locale)] = value
            continue
        localized[UI_LOG_DETAIL_LABELS.get(str(key), str(key))] = value
    return localized


def _localized_log_message(event: dict[str, Any], *, technical: bool = False, locale: Any = None) -> str:
    event_type = str(event.get("event_type") or "")
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    if event_type.startswith("watchdog_") or event_type.startswith("vpn_watchdog_"):
        watchdog_message = _watchdog_message_for_event(event_type, details, locale=locale)
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


def _summarize_log_event(event: dict[str, Any], *, technical: bool = False, locale: Any = None) -> dict[str, Any]:
    if technical:
        return {
            "timestamp": event.get("timestamp"),
            "level": event.get("level"),
            "component": event.get("component"),
            "event_type": event.get("event_type"),
            "category": _log_event_category(event, technical=True),
            "message": _localized_log_message(event, technical=True, locale=locale),
            "details": _operator_log_details(event, technical=True, locale=locale),
            "ui_visible": _log_event_ui_visible(event, technical=True),
        }
    return {
        "event_id": event.get("event_id"),
        "created_at": event.get("created_at"),
        "level": event.get("level"),
        "event_type": event.get("event_type"),
        "category": _log_event_category(event),
        "subject_id": event.get("subject_id"),
        "message": _localized_log_message(event, locale=locale),
        "details": _operator_log_details(event, locale=locale),
        "ui_visible": _log_event_ui_visible(event),
    }
