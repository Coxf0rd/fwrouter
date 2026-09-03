from __future__ import annotations

from typing import Any


DEFAULT_UI_TEXT_LOCALE = "ru"
SUPPORTED_UI_TEXT_LOCALES = {"ru", "en"}


def _normalize_ui_text_locale(locale: Any = None) -> str:
    raw = str(locale or DEFAULT_UI_TEXT_LOCALE).strip().lower().replace("_", "-")
    lang = raw.split("-", 1)[0]
    return lang if lang in SUPPORTED_UI_TEXT_LOCALES else DEFAULT_UI_TEXT_LOCALE


def _normalize_i18n_map(values: dict[str, str] | None) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    normalized: dict[str, str] = {}
    for locale, value in values.items():
        lang = str(locale or "").strip().lower().replace("_", "-").split("-", 1)[0]
        text = str(value or "").strip()
        if lang and text:
            normalized[lang] = text
    return normalized


def _ui_text(
    *,
    title_i18n: dict[str, str] | None = None,
    reason_i18n: dict[str, str] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    normalized_title = _normalize_i18n_map(title_i18n)
    if normalized_title:
        entry["title_i18n"] = normalized_title
        entry["title"] = normalized_title.get(DEFAULT_UI_TEXT_LOCALE) or next(
            iter(normalized_title.values())
        )
    normalized_reason = _normalize_i18n_map(reason_i18n)
    if normalized_reason:
        entry["reason_i18n"] = normalized_reason
        entry["reason"] = normalized_reason.get(DEFAULT_UI_TEXT_LOCALE) or next(
            iter(normalized_reason.values())
        )
    return entry


UI_TEXT_REGISTRY = {'watchdog.status': {'paused_signal_unavailable': {'title_i18n': {'ru': 'Нет свежего '
                                                                        'сигнала '
                                                                        'трафика',
                                                                  'en': 'No fresh '
                                                                        'traffic '
                                                                        'signal'},
                                                   'reason_i18n': {'ru': 'Нет свежего '
                                                                         'достоверного '
                                                                         'снимка '
                                                                         'счетчиков '
                                                                         'трафика, '
                                                                         'поэтому '
                                                                         'автоматическая '
                                                                         'смена '
                                                                         'подавлена, '
                                                                         'чтобы не '
                                                                         'переключать '
                                                                         'сервер по '
                                                                         'ложному '
                                                                         'сигналу.',
                                                                   'en': 'There is no '
                                                                         'fresh '
                                                                         'reliable '
                                                                         'traffic-counter '
                                                                         'snapshot, so '
                                                                         'automatic '
                                                                         'switching is '
                                                                         'suppressed '
                                                                         'to avoid '
                                                                         'changing '
                                                                         'servers on a '
                                                                         'false '
                                                                         'signal.'}},
                     'traffic_failure_pending': {'title_i18n': {'ru': 'Сбой трафика '
                                                                      'еще '
                                                                      'подтверждается',
                                                                'en': 'Traffic failure '
                                                                      'is still being '
                                                                      'confirmed'},
                                                 'reason_i18n': {'ru': 'Замечен '
                                                                       'исходящий '
                                                                       'VPN-трафик без '
                                                                       'ответных '
                                                                       'байтов; '
                                                                       'watchdog ждет '
                                                                       'повторный '
                                                                       'свежий снимок '
                                                                       'перед '
                                                                       'failover.',
                                                                 'en': 'Outgoing VPN '
                                                                       'traffic was '
                                                                       'seen without '
                                                                       'response '
                                                                       'bytes; '
                                                                       'watchdog waits '
                                                                       'for a repeated '
                                                                       'fresh snapshot '
                                                                       'before '
                                                                       'failover.'}},
                     'no_failure_no_traffic': {'title_i18n': {'ru': 'Трафика нет, это '
                                                                    'не считается '
                                                                    'сбоем',
                                                              'en': 'No traffic, not '
                                                                    'treated as a '
                                                                    'failure'},
                                               'reason_i18n': {'ru': 'Watchdog не '
                                                                     'видел '
                                                                     'VPN-трафика, '
                                                                     'поэтому считает '
                                                                     'состояние idle и '
                                                                     'не переключает '
                                                                     'сервер.',
                                                               'en': 'Watchdog did not '
                                                                     'see VPN traffic, '
                                                                     'so it treats the '
                                                                     'state as idle '
                                                                     'and does not '
                                                                     'switch '
                                                                     'servers.'}},
                     'healthy': {'title_i18n': {'ru': 'VPN-сервер отвечает',
                                                'en': 'VPN server is healthy'},
                                 'reason_i18n': {'ru': 'Watchdog увидел VPN-трафик и '
                                                       'успешную проверку активного '
                                                       'сервера.',
                                                 'en': 'Watchdog saw VPN traffic and a '
                                                       'successful check of the active '
                                                       'server.'}},
                     'failover_candidate_found': {'title_i18n': {'ru': 'Кандидат '
                                                                       'найден, смена '
                                                                       'не применялась',
                                                                 'en': 'Candidate '
                                                                       'found, switch '
                                                                       'was not '
                                                                       'applied'},
                                                  'reason_i18n': {'ru': 'Проверка '
                                                                        'нашла рабочий '
                                                                        'сервер, но '
                                                                        'текущий '
                                                                        'запуск был '
                                                                        'без права '
                                                                        'применять '
                                                                        'смену.',
                                                                  'en': 'The check '
                                                                        'found a '
                                                                        'working '
                                                                        'server, but '
                                                                        'this run was '
                                                                        'not allowed '
                                                                        'to apply the '
                                                                        'switch.'}},
                     'failover_applied': {'title_i18n': {'ru': 'Failover применен',
                                                         'en': 'Failover applied'},
                                          'reason_i18n': {'ru': 'Watchdog подтвердил '
                                                                'проблему и применил '
                                                                'рабочий VPN-auto '
                                                                'кандидат.',
                                                          'en': 'Watchdog confirmed '
                                                                'the problem and '
                                                                'applied a working '
                                                                'VPN-auto candidate.'}},
                     'fail_open_direct_recommended': {'title_i18n': {'ru': 'Рабочий '
                                                                           'кандидат '
                                                                           'не найден',
                                                                     'en': 'No working '
                                                                           'candidate '
                                                                           'found'},
                                                      'reason_i18n': {'ru': 'Сбой '
                                                                            'VPN-трафика '
                                                                            'подтвержден, '
                                                                            'но среди '
                                                                            'кандидатов '
                                                                            'не найден '
                                                                            'рабочий '
                                                                            'сервер '
                                                                            'для '
                                                                            'автоматической '
                                                                            'смены.',
                                                                      'en': 'The VPN '
                                                                            'traffic '
                                                                            'failure '
                                                                            'was '
                                                                            'confirmed, '
                                                                            'but no '
                                                                            'working '
                                                                            'server '
                                                                            'candidate '
                                                                            'was found '
                                                                            'for '
                                                                            'automatic '
                                                                            'switching.'}},
                     'runtime_convergence_failed': {'title_i18n': {'ru': 'Runtime '
                                                                         'маршрутизации '
                                                                         'нездоров',
                                                                   'en': 'Routing '
                                                                         'runtime is '
                                                                         'unhealthy'},
                                                    'reason_i18n': {'ru': 'Сначала '
                                                                          'нужно '
                                                                          'восстановить '
                                                                          'dataplane/runtime; '
                                                                          'смена '
                                                                          'VPN-сервера '
                                                                          'могла бы '
                                                                          'скрыть '
                                                                          'основную '
                                                                          'проблему.',
                                                                    'en': 'The '
                                                                          'dataplane/runtime '
                                                                          'must be '
                                                                          'restored '
                                                                          'first; '
                                                                          'changing '
                                                                          'the VPN '
                                                                          'server '
                                                                          'could hide '
                                                                          'the root '
                                                                          'problem.'}},
                     'runtime_unavailable': {'title_i18n': {'ru': 'VPN runtime '
                                                                  'недоступен',
                                                            'en': 'VPN runtime is '
                                                                  'unavailable'},
                                             'reason_i18n': {'ru': 'Активный VPN '
                                                                   'runtime не готов '
                                                                   'или не отвечает, '
                                                                   'поэтому сервер не '
                                                                   'менялся.',
                                                             'en': 'The active VPN '
                                                                   'runtime is not '
                                                                   'ready or not '
                                                                   'responding, so the '
                                                                   'server was not '
                                                                   'changed.'}},
                     'external_runtime_active': {'title_i18n': {'ru': 'Активен внешний '
                                                                      'VPN runtime',
                                                                'en': 'External VPN '
                                                                      'runtime is '
                                                                      'active'},
                                                 'reason_i18n': {'ru': 'FWRouter видит '
                                                                       'внешний VPN '
                                                                       'runtime и не '
                                                                       'управляет его '
                                                                       'selector '
                                                                       'напрямую.',
                                                                 'en': 'FWRouter sees '
                                                                       'an external '
                                                                       'VPN runtime '
                                                                       'and does not '
                                                                       'control its '
                                                                       'selector '
                                                                       'directly.'}},
                     'external_runtime_failover_unavailable': {'title_i18n': {'ru': 'У '
                                                                                    'внешнего '
                                                                                    'VPN '
                                                                                    'runtime '
                                                                                    'нет '
                                                                                    'failover',
                                                                              'en': 'External '
                                                                                    'VPN '
                                                                                    'runtime '
                                                                                    'has '
                                                                                    'no '
                                                                                    'failover'},
                                                               'reason_i18n': {'ru': 'Сбой '
                                                                                     'трафика '
                                                                                     'подтвержден, '
                                                                                     'но '
                                                                                     'внешний '
                                                                                     'VPN '
                                                                                     'runtime '
                                                                                     'не '
                                                                                     'предоставил '
                                                                                     'endpoint '
                                                                                     'для '
                                                                                     'автоматического '
                                                                                     'failover.',
                                                                               'en': 'The '
                                                                                     'traffic '
                                                                                     'failure '
                                                                                     'was '
                                                                                     'confirmed, '
                                                                                     'but '
                                                                                     'the '
                                                                                     'external '
                                                                                     'VPN '
                                                                                     'runtime '
                                                                                     'did '
                                                                                     'not '
                                                                                     'provide '
                                                                                     'an '
                                                                                     'endpoint '
                                                                                     'for '
                                                                                     'automatic '
                                                                                     'failover.'}},
                     'needs_initial_auto_selection': {'title_i18n': {'ru': 'Нет '
                                                                           'валидного '
                                                                           'активного '
                                                                           'auto-сервера',
                                                                     'en': 'No valid '
                                                                           'active '
                                                                           'auto '
                                                                           'server'},
                                                      'reason_i18n': {'ru': 'В режиме '
                                                                            'VPN-auto '
                                                                            'нет '
                                                                            'валидного '
                                                                            'активного '
                                                                            'сервера; '
                                                                            'нужен '
                                                                            'первичный '
                                                                            'выбор.',
                                                                      'en': 'VPN-auto '
                                                                            'mode has '
                                                                            'no valid '
                                                                            'active '
                                                                            'server; '
                                                                            'an '
                                                                            'initial '
                                                                            'selection '
                                                                            'is '
                                                                            'required.'}},
                     'scheduler_failed': {'title_i18n': {'ru': 'Фоновая проверка упала',
                                                         'en': 'Background check '
                                                               'failed'},
                                          'reason_i18n': {'ru': 'Внутренняя ошибка '
                                                                'остановила один шаг '
                                                                'фоновой проверки.',
                                                          'en': 'An internal error '
                                                                'stopped one '
                                                                'background check '
                                                                'step.'}},
                     'manual_selection': {'title_i18n': {'ru': 'Включен ручной выбор '
                                                               'сервера',
                                                         'en': 'Manual server '
                                                               'selection is enabled'},
                                          'reason_i18n': {'ru': 'Сбой трафика '
                                                                'подтвержден, но '
                                                                'выбран ручной режим '
                                                                'сервера, поэтому '
                                                                'автоматика не '
                                                                'переключает.',
                                                          'en': 'The traffic failure '
                                                                'was confirmed, but '
                                                                'manual server mode is '
                                                                'selected, so '
                                                                'automation does not '
                                                                'switch.'}},
                     'failover_cooldown': {'title_i18n': {'ru': 'Failover на паузе '
                                                                'после недавней смены',
                                                          'en': 'Failover is paused '
                                                                'after a recent '
                                                                'switch'},
                                           'reason_i18n': {'ru': 'Сбой трафика '
                                                                 'подтвержден, но '
                                                                 'после недавней смены '
                                                                 'еще действует '
                                                                 'cooldown.',
                                                           'en': 'The traffic failure '
                                                                 'was confirmed, but '
                                                                 'cooldown after a '
                                                                 'recent switch is '
                                                                 'still active.'}},
                     'active_quality_degraded_traffic_healthy': {'title_i18n': {'ru': 'Проверка '
                                                                                      'сервера '
                                                                                      'нестабильна, '
                                                                                      'но '
                                                                                      'VPN-трафик '
                                                                                      'отвечает',
                                                                                'en': 'Server '
                                                                                      'check '
                                                                                      'is '
                                                                                      'unstable, '
                                                                                      'but '
                                                                                      'VPN '
                                                                                      'traffic '
                                                                                      'responds'},
                                                                 'reason_i18n': {'ru': 'Delay-check '
                                                                                       'текущего '
                                                                                       'сервера '
                                                                                       'нестабилен, '
                                                                                       'но '
                                                                                       'есть '
                                                                                       'ответный '
                                                                                       'VPN-трафик; '
                                                                                       'watchdog '
                                                                                       'не '
                                                                                       'меняет '
                                                                                       'сервер '
                                                                                       'по '
                                                                                       'одному '
                                                                                       'техническому '
                                                                                       'сигналу.',
                                                                                 'en': 'The '
                                                                                       'current-server '
                                                                                       'delay-check '
                                                                                       'is '
                                                                                       'unstable, '
                                                                                       'but '
                                                                                       'response '
                                                                                       'VPN '
                                                                                       'traffic '
                                                                                       'is '
                                                                                       'present; '
                                                                                       'watchdog '
                                                                                       'does '
                                                                                       'not '
                                                                                       'change '
                                                                                       'server '
                                                                                       'on '
                                                                                       'one '
                                                                                       'technical '
                                                                                       'signal.'}},
                     'active_quality_degraded_pending': {'title_i18n': {'ru': 'Качество '
                                                                              'сервера '
                                                                              'деградирует, '
                                                                              'идет '
                                                                              'подтверждение',
                                                                        'en': 'Server '
                                                                              'quality '
                                                                              'is '
                                                                              'degraded, '
                                                                              'confirmation '
                                                                              'is in '
                                                                              'progress'},
                                                         'reason_i18n': {'ru': 'Ответный '
                                                                               'VPN-трафик '
                                                                               'есть, '
                                                                               'но '
                                                                               'delay-check '
                                                                               'текущего '
                                                                               'сервера '
                                                                               'повторно '
                                                                               'нестабилен; '
                                                                               'watchdog '
                                                                               'ждет '
                                                                               'окно '
                                                                               'подтверждения '
                                                                               'перед '
                                                                               'сменой.',
                                                                         'en': 'Response '
                                                                               'VPN '
                                                                               'traffic '
                                                                               'is '
                                                                               'present, '
                                                                               'but '
                                                                               'the '
                                                                               'current-server '
                                                                               'delay-check '
                                                                               'is '
                                                                               'repeatedly '
                                                                               'unstable; '
                                                                               'watchdog '
                                                                               'is '
                                                                               'waiting '
                                                                               'for '
                                                                               'the '
                                                                               'confirmation '
                                                                               'window '
                                                                               'before '
                                                                               'switching.'}}},
 'watchdog.action': {'none': {'title_i18n': {'ru': 'Сервер не менялся',
                                             'en': 'Server was not changed'}},
                     'dry_run_only': {'title_i18n': {'ru': 'Только проверка, без '
                                                           'применения',
                                                     'en': 'Check only, no changes '
                                                           'applied'}},
                     'switch_vpn_auto': {'title_i18n': {'ru': 'Выбран новый VPN-auto '
                                                              'сервер',
                                                        'en': 'New VPN-auto server '
                                                              'selected'}},
                     'fail_open_direct_recommended': {'title_i18n': {'ru': 'Нужна '
                                                                           'ручная '
                                                                           'проверка '
                                                                           'или '
                                                                           'временный '
                                                                           'DIRECT',
                                                                     'en': 'Manual '
                                                                           'check or '
                                                                           'temporary '
                                                                           'DIRECT is '
                                                                           'needed'}}},
 'watchdog.event': {'scheduler_failed': {'title_i18n': {'ru': 'Watchdog не выполнил '
                                                              'фоновую проверку',
                                                        'en': 'Watchdog did not '
                                                              'complete the background '
                                                              'check'}},
                    'switch_suppressed': {'title_i18n': {'ru': 'Watchdog не стал '
                                                               'менять VPN-сервер',
                                                         'en': 'Watchdog did not '
                                                               'change the VPN '
                                                               'server'}},
                    'switch_applied': {'title_i18n': {'ru': 'Watchdog сменил '
                                                            'VPN-сервер',
                                                      'en': 'Watchdog changed the VPN '
                                                            'server'}},
                    'switch_candidate': {'title_i18n': {'ru': 'Watchdog нашел '
                                                              'VPN-кандидата',
                                                        'en': 'Watchdog found a VPN '
                                                              'candidate'}},
                    'check_completed': {'title_i18n': {'ru': 'Watchdog проверил '
                                                             'VPN-сервер',
                                                       'en': 'Watchdog checked the VPN '
                                                             'server'}}},
 'error.code': {'RULES_VALIDATION_FAILED': {'reason_i18n': {'ru': 'В правилах '
                                                                  'маршрутизации есть '
                                                                  'некорректная строка '
                                                                  'или '
                                                                  'неподдерживаемый '
                                                                  'формат.',
                                                            'en': 'A routing rule '
                                                                  'contains an invalid '
                                                                  'line or unsupported '
                                                                  'format.'}}},
 'traffic.metric': {'direct_rx_bytes': {'title_i18n': {'ru': 'DIRECT вход',
                                                       'en': 'DIRECT inbound'}},
                    'direct_tx_bytes': {'title_i18n': {'ru': 'DIRECT выход',
                                                       'en': 'DIRECT outbound'}},
                    'vpn_rx_bytes': {'title_i18n': {'ru': 'VPN вход',
                                                    'en': 'VPN inbound'}},
                    'vpn_tx_bytes': {'title_i18n': {'ru': 'VPN выход',
                                                    'en': 'VPN outbound'}}},
 'inventory.activity': {'profile_seen_24h': {'title_i18n': {'ru': 'Профиль '
                                                                  'запрашивался за 24ч',
                                                            'en': 'Profile requested '
                                                                  'within 24h'}},
                        'traffic_seen': {'title_i18n': {'ru': 'Был трафик',
                                                        'en': 'Traffic was seen'}},
                        'runtime_active': {'title_i18n': {'ru': 'Runtime активен',
                                                          'en': 'Runtime is active'}},
                        'stale_seen': {'title_i18n': {'ru': 'Нет свежей активности',
                                                      'en': 'No recent activity'}},
                        'unknown': {'title_i18n': {'ru': 'Нет данных активности',
                                                   'en': 'No activity data'}}},
 'display.system.title': {'lan': {'title_i18n': {'ru': 'Lan / Core',
                                                 'en': 'LAN / Core'}},
                          'external_network_source': {'title_i18n': {'ru': 'Внешняя '
                                                                           'сеть',
                                                                     'en': 'External '
                                                                           'network'}},
                          'vless_client': {'title_i18n': {'ru': 'Vless',
                                                          'en': 'Vless'}},
                          'vpn_runtime': {'title_i18n': {'ru': 'VPN runtime',
                                                         'en': 'VPN runtime'}},
                          'docker': {'title_i18n': {'ru': 'Docker', 'en': 'Docker'}},
                          'host': {'title_i18n': {'ru': 'Службы хоста',
                                                  'en': 'Host services'}}},
 'display.system.description': {'lan': {'title_i18n': {'ru': 'Клиенты LAN и routing '
                                                             'core FWRouter.',
                                                       'en': 'LAN clients and FWRouter '
                                                             'routing core.'}},
                                'external_network_source': {'title_i18n': {'ru': 'Внешний '
                                                                                 'источник '
                                                                                 'клиентов; '
                                                                                 'FWRouter '
                                                                                 'показывает '
                                                                                 'его '
                                                                                 'только '
                                                                                 'когда '
                                                                                 'есть '
                                                                                 'реальные '
                                                                                 'найденные '
                                                                                 'клиенты.',
                                                                           'en': 'External '
                                                                                 'client '
                                                                                 'source; '
                                                                                 'FWRouter '
                                                                                 'shows '
                                                                                 'it '
                                                                                 'only '
                                                                                 'when '
                                                                                 'real '
                                                                                 'discovered '
                                                                                 'clients '
                                                                                 'exist.'}},
                                'vless_client': {'title_i18n': {'ru': 'Клиентское ядро '
                                                                      'Vless; '
                                                                      'конкретная '
                                                                      'реализация '
                                                                      'хранится '
                                                                      'отдельно.',
                                                                'en': 'Vless client '
                                                                      'core; the '
                                                                      'concrete '
                                                                      'implementation '
                                                                      'is stored '
                                                                      'separately.'}},
                                'vpn_runtime': {'title_i18n': {'ru': 'VPN/dataplane-адаптер '
                                                                     'FWRouter; '
                                                                     'конкретная '
                                                                     'реализация '
                                                                     'хранится '
                                                                     'отдельно.',
                                                               'en': 'FWRouter '
                                                                     'VPN/dataplane '
                                                                     'adapter; the '
                                                                     'concrete '
                                                                     'implementation '
                                                                     'is stored '
                                                                     'separately.'}},
                                'docker': {'title_i18n': {'ru': 'Отображение '
                                                                'контейнеров; это не '
                                                                'управляемый '
                                                                'runtime-модуль.',
                                                          'en': 'Container inventory '
                                                                'view; this is not a '
                                                                'managed runtime '
                                                                'module.'}},
                                'host': {'title_i18n': {'ru': 'Отображение служб хоста '
                                                              'и systemd.',
                                                        'en': 'Host and systemd '
                                                              'services inventory '
                                                              'view.'}},
                                'external_network_discovered': {'title_i18n': {'ru': 'Внешний '
                                                                                     'сетевой '
                                                                                     'источник '
                                                                                     'найден '
                                                                                     'в '
                                                                                     'inventory '
                                                                                     'клиентов.',
                                                                               'en': 'External '
                                                                                     'network '
                                                                                     'source '
                                                                                     'discovered '
                                                                                     'from '
                                                                                     'client '
                                                                                     'inventory.'}}},
 'connection.description': {'external_management': {'title_i18n': {'ru': 'Внешний '
                                                                         'управляющий '
                                                                         'клиент: '
                                                                         'вызывает API '
                                                                         'FWRouter, но '
                                                                         'не является '
                                                                         'целью '
                                                                         'маршрутизации.',
                                                                   'en': 'External '
                                                                         'management '
                                                                         'client: '
                                                                         'calls the '
                                                                         'FWRouter '
                                                                         'API, but is '
                                                                         'not a '
                                                                         'routing '
                                                                         'target.'}},
                            'external_vpn_module': {'title_i18n': {'ru': 'Внешний '
                                                                         'VPN-модуль '
                                                                         'выхода: '
                                                                         'runtime '
                                                                         'управляется '
                                                                         'пользователем '
                                                                         'и может '
                                                                         'стать '
                                                                         'VPN-провайдером '
                                                                         'после '
                                                                         'включения '
                                                                         'поддержки в '
                                                                         'dataplane.',
                                                                   'en': 'External VPN '
                                                                         'egress '
                                                                         'module: '
                                                                         'user-managed '
                                                                         'runtime that '
                                                                         'can become a '
                                                                         'VPN provider '
                                                                         'after '
                                                                         'dataplane '
                                                                         'support is '
                                                                         'enabled.'}},
                            'external_network_source': {'title_i18n': {'ru': 'Внешний '
                                                                             'источник '
                                                                             'клиентов: '
                                                                             'пользовательский '
                                                                             'ingress/network '
                                                                             'inventory '
                                                                             'provider.',
                                                                       'en': 'External '
                                                                             'client '
                                                                             'source: '
                                                                             'user-managed '
                                                                             'ingress/network '
                                                                             'inventory '
                                                                             'provider.'}},
                            'display_only': {'title_i18n': {'ru': 'Внешняя система '
                                                                  'только для '
                                                                  'отображения.',
                                                            'en': 'Display-only '
                                                                  'external system.'}}},
 'connection.api_example': {'switch_vpn_auto_server': {'title_i18n': {'ru': 'Переключить '
                                                                            'сервер '
                                                                            'VPN-auto',
                                                                      'en': 'Switch '
                                                                            'VPN-auto '
                                                                            'server'}},
                            'clear_fixed_global_server': {'title_i18n': {'ru': 'Сбросить '
                                                                               'фиксированный '
                                                                               'глобальный '
                                                                               'сервер',
                                                                         'en': 'Clear '
                                                                               'fixed '
                                                                               'global '
                                                                               'server'}}},
 'server.virtual': {'xray_vpn_auto': {'title_i18n': {'ru': 'Автоматический выбор',
                                                     'en': 'Automatic selection'}},
                    'custom_https_proxy': {'title_i18n': {'ru': 'Прокси (не заходить)',
                                                          'en': 'Proxy (do not '
                                                                'enter)'}}}}


UI_TEXT_REGISTRY.setdefault("log.event", {}).update(
    {
        "xray_binding_materialized": _ui_text(
            title_i18n={
                "ru": "Xray runtime bindings обновлены",
                "en": "Xray runtime bindings updated",
            },
            reason_i18n={
                "ru": "Backend синхронизировал metadata bindings для Xray runtime без изменения пользовательского действия.",
                "en": "The backend synchronized Xray runtime binding metadata without a user-facing action change.",
            },
        ),
        "mihomo_reconciled": _ui_text(
            title_i18n={
                "ru": "Mihomo runtime синхронизирован",
                "en": "Mihomo runtime reconciled",
            },
            reason_i18n={
                "ru": "Backend проверил и привел Mihomo runtime к текущему состоянию FWRouter.",
                "en": "The backend checked and aligned the Mihomo runtime with the current FWRouter state.",
            },
        ),
        "mihomo_reconcile_failed": _ui_text(
            title_i18n={
                "ru": "Не удалось синхронизировать Mihomo runtime",
                "en": "Failed to reconcile Mihomo runtime",
            },
            reason_i18n={
                "ru": "Backend не смог привести Mihomo runtime к текущему состоянию FWRouter.",
                "en": "The backend could not align the Mihomo runtime with the current FWRouter state.",
            },
        ),
        "mihomo_reconcile_skipped": _ui_text(
            title_i18n={
                "ru": "Mihomo runtime уже актуален",
                "en": "Mihomo runtime already current",
            },
            reason_i18n={
                "ru": "Backend сравнил active и candidate config и не стал перезапускать Mihomo без необходимости.",
                "en": "The backend compared the active and candidate config and did not restart Mihomo unnecessarily.",
            },
        ),
        "subscription_refresh_applied": _ui_text(
            title_i18n={
                "ru": "Подписка обновлена и применена",
                "en": "Subscription refreshed and applied",
            },
            reason_i18n={
                "ru": "Backend скачал новые данные подписки и синхронизировал VPN runtime.",
                "en": "The backend downloaded new subscription data and reconciled the VPN runtime.",
            },
        ),
    }
)


UNKNOWN_TEXT_FALLBACKS = {'watchdog.status': {'title_i18n': {'ru': 'Неизвестный статус watchdog',
                                    'en': 'Unknown watchdog status'},
                     'reason_i18n': {'ru': 'UI пока не знает этот машинный статус; код '
                                           'оставлен в деталях для диагностики.',
                                     'en': 'The UI does not know this machine status '
                                           'yet; the raw code is kept in details for '
                                           'diagnostics.'}},
 'watchdog.action': {'title_i18n': {'ru': 'Неизвестное действие watchdog',
                                    'en': 'Unknown watchdog action'}},
 'error.code': {'reason_i18n': {'ru': 'Ошибка без локализованного пояснения; код '
                                      'оставлен в деталях для диагностики.',
                                'en': 'Error without a localized explanation; the code '
                                      'is kept in details for diagnostics.'}},
 'traffic.metric': {'title_i18n': {'ru': 'Трафик', 'en': 'Traffic'}},
 'inventory.activity': {'title_i18n': {'ru': 'Нет данных активности',
                                       'en': 'No activity data'}},
 'display.system.title': {'title_i18n': {'ru': 'Внешняя система',
                                         'en': 'External system'}},
 'display.system.description': {'title_i18n': {'ru': 'Внешняя система.',
                                               'en': 'External system.'}},
 'connection.description': {'title_i18n': {'ru': 'Внешнее подключение.',
                                           'en': 'External connection.'}},
 'connection.api_example': {'title_i18n': {'ru': 'Пример API', 'en': 'API example'}},
 'server.virtual': {'title_i18n': {'ru': 'Виртуальный сервер', 'en': 'Virtual server'}}}


def _ui_text_entry(namespace: str, key: Any) -> dict[str, Any] | None:
    entries = UI_TEXT_REGISTRY.get(namespace)
    if not isinstance(entries, dict):
        return None
    return entries.get(str(key))


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
    if fallback is not None:
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
    if fallback is not None:
        reason = _ui_text_value(fallback, "reason", locale)
        if reason:
            return reason
    return None
