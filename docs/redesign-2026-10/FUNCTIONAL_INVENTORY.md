# Реестр существующих функций и контрактов

Снимок исходников от 10.10.2026. Этот реестр дополняет SPEC_RU.md. Не все функции существуют как статические HTML-контролы: динамические handlers и контракты API нужно сверять перед реализацией. Указанные controls включают скрытые алиасы и служебные элементы; их нельзя механически переносить в видимый UI.

## 1. Матрица функционального сохранения

| Область | Новый вход | Что сохранить |
|---|---|---|
| Обзор | Обзор | Состояния, приоритет, данные, наблюдение, включение анализа |
| HTF | Рабочее место / HTF | D1/W1, зоны, группировка, фильтры, типы, архив, детали и история |
| H1 | Рабочее место / H1 | Структура, точки, BOS/SMS, уровни, OB/FVG/BSL/SSL, идеи, close/select-context, события |
| Проверка | Проверка | Все decisions, assessments, correction/anchor, comment, history, export |
| Ручные зоны | Overlay рабочего места | Рисование, числовая форма, level/range, направление, ТФ, правило, anchor/name/comment |
| Контекст | Контекст рынка | Overview, journal, calendar, rules, health, derivatives, situations, gate history, at_event/now |
| Настройки | Настройки | Все серверные поля и группы, notifications, experimental, theme, recalc status |
| Альткоины | Скринер → детали | Все buckets, min/max filters, sorting, search, list/table, setup/candidate, график, targets, revisions, PNG |
| Экспорт | Проверка / детали | Labels, reviews, chart PNG и PNG 2× |
| Связность | Общий shell | Авторизация, WS, freshness, diagnostics, старые ссылки и Telegram deep links |

## Контролы: app/web/static/index.html

| Тип | ID / data | Подпись / варианты |
|---|---|---|
| button | data-filter=all | Все |
| button | data-filter=attention | Нужно внимание |
| button | data-filter=watch | Наблюдаю |
| select | instrument-select | Инструмент |
| button | asset-active-btn | Выключить актив |
| select | tf-select | D1 W1 Свечи H1 |
| input | layer-tf-d1 | checkbox |
| input | layer-tf-w1 | checkbox |
| input | tf-all | checkbox |
| input | show-candidates | checkbox |
| input | layer-history | checkbox |
| input | layer-mids | checkbox |
| input | layer-labels | checkbox |
| select | h1-points | Последние 20 История Скрыть |
| input | h1-diagnostic | checkbox |
| input | h1-breaks | checkbox |
| input | h1-expected | checkbox |
| input | h1-zones | checkbox |
| input | h1-ob | checkbox |
| input | h1-fvg | checkbox |
| input | h1-bsl | checkbox |
| input | h1-ssl | checkbox |
| input | h1-eligible-only | checkbox |
| select | h1-idea | Все HTF-идеи |
| button | h1-close-idea | Закрыть выбранную идею |
| input | h1-candidates | checkbox |
| input | h1-zone-history | checkbox |
| input | h1-htf | checkbox |
| button | btn-new-zone | + Зона |
| button | mode-context / data-chart-mode=context | Контекст |
| button | lnk-ltf / data-chart-mode=h1 | Структура H1 |
| button | h1-offscreen | Вне экрана: 0 |
| button | draw-cancel | Отмена |
| button | btn-table-mode | Таблица |
| button | data-bucket=live | Актуальные |
| button | data-bucket=candidate | Кандидаты |
| button | data-bucket=archive | Архив |
| select | zone-type-filter | Все типы FVG OB PRB Breaker SSL BSL Ручная |
| select | zone-tf-filter | D1 + W1 D1 W1 |
| select | zone-rel-filter | Положение Цена внутри Выше цены Ниже цены |
| button | btn-resort | Обновить порядок |
| select | zone-status-filter | актуальные кандидаты активные отработанные отклонённые все |
| button | detail-close | ← К зонам |
| select | review-tf-filter | D1 + W1 D1 W1 |
| button | btn-download-labels | Скачать проверенные |
| button | data-kind=all | Все |
| button | data-kind=market | Рынок |
| button | data-kind=decisions | Решения |
| button | data-kind=delivery | Доставка |
| button | data-theme-choice=system | Как в системе |
| button | data-theme-choice=dark | Тёмная тема |
| button | data-theme-choice=light | Светлая тема |
| button | settings-cancel | Отменить |
| button | settings-save | Сохранить |
| button | choice-draw | Нарисовать на графике |
| button | choice-form | Ввести границы |
| button | choice-cancel | Отмена |
| input | bounds-lower | number |
| input | bounds-upper | number |
| input | bounds-anchor-dt | datetime-local; Время открытия свечи-якоря |
| button | bounds-anchor-pick | На графике |
| button | bounds-anchor-clear | Сбросить |
| input | bounds-anchor | number |
| button | bounds-save | Сохранить |
| button | bounds-cancel | Отмена |
| button | anchor-pick-cancel | Отмена |
| select | mz-mode | Диапазон Уровень |
| input | mz-lower | number |
| input | mz-upper | number |
| input | mz-level | number; L = U |
| select | mz-direction | Бычий Медвежий |
| select | mz-timeframe | D1 W1 |
| select | mz-ztype | OB FVG без правила |
| input | mz-anchor | datetime-local |
| input | mz-name | text |
| textarea | mz-comment | Динамическое поле / подпись в родительском label |
| button | mz-save | Создать зону |
| button | mz-cancel | Отмена |
| button | btn-settings | button |

## Контролы: app/web/static/alt.html

| Тип | ID / data | Подпись / варианты |
|---|---|---|
| button | alt-proc-toggle | Обработка |
| button | alt-recalc | Пересчитать |
| button | data-bucket=eligible | Все активные |
| button | data-bucket=new_entries | Новые входы |
| button | data-bucket=awaiting_retest | Ожидание ретеста |
| button | data-bucket=review | Требуют проверки |
| button | data-bucket=history | История |
| select | flt-venue | Все |
| button | alt-filters-toggle | Фильтры |
| button | alt-table-toggle | Таблица |
| input | flt-rank-min | number |
| input | flt-rank-max | number |
| input | flt-age-min | number |
| input | flt-age-max | number |
| input | flt-dd-min | number |
| input | flt-dd-max | number |
| select | flt-structure | Любые BOS/SMS Манипуляция Выход |
| select | flt-stage | Как на вкладке Зрелые Формирующиеся |
| button | alt-apply | Применить |
| button | alt-reset | Сбросить фильтры |
| input | alt-search | search; Тикер, имя или пара |
| select | alt-sort | Порядок сервера Ближе ко входу Ранг капитализации Возраст диапазона |
| button | alt-collapse-list | Свернуть список |
| button | alt-reveal | Показать в списке |
| button | alt-retry-list | Повторить |
| button | alt-back | К списку |
| button | alt-mode-setup / data-mode=setup | Сетап |
| button | alt-mode-history / data-mode=history | История |
| button | alt-mode-none / data-mode=none | Без событий |
| button | data-tf=D1 | D1 |
| button | data-tf=W1 | W1 |
| button | alt-layers-toggle | Слои |
| button | alt-180 | 180D |
| button | alt-range | Весь диапазон |
| button | alt-all-history | Вся история |
| button | alt-expand | Развернуть график |
| button | alt-card-toggle | Карточка |
| input | layer-range | checkbox |
| input | layer-entries | checkbox |
| input | layer-manip | checkbox |
| select | layer-targets | Ближайшая Все Скрыть |
| input | layer-cancel | checkbox |
| input | layer-reverse | checkbox |
| button | alt-auto | Авто |
| button | alt-png | PNG |
| button | alt-png2 | PNG 2× |
| select | journal-type | Все |
| input | journal-from | date |
| input | journal-to | date |
| button | alt-collapse-card | Свернуть |
| button | alt-card-close | Закрыть |
| input | range-edit-l | number |
| input | range-edit-u | number |
| input | range-edit-start | date |
| input | range-edit-end | date |
| input | range-edit-reason | text |
| button | range-preview-btn | Проверить |
| button | range-save-btn | Сохранить |
| button | range-restore-btn | Вернуть авто |
| button | range-cancel-btn | Отмена |
| button | alt-columns-toggle | Колонки |
| button | alt-table-back | К графику |

## Контролы: app/web/static/events.html

| Тип | ID / data | Подпись / варианты |
|---|---|---|
| button | data-mode=now | Сейчас |
| button | data-mode=feed | Лента |
| button | data-mode=calendar | Календарь |
| button | data-mode=rules | Правила |

## Контролы: app/web/static/ltf.html

| Тип | ID / data | Подпись / варианты |
|---|---|---|
| button | btn-ltf-settings | LTF |
| button | ltf-burger | ☰ |
| select | ltf-instrument | Актив |
| button | ltf-mode-now | Сейчас |
| button | ltf-mode-history | История |
| button | data-days=3 | 3д |
| button | data-days=7 | 7д |
| button | data-days=14 | 14д |
| button | ltf-to-price | К текущей цене |
| button | ltf-filter-toggle | Фильтры |
| button | ltf-sort-priority | По приоритету |
| input | flt-symbol | search; Актив (тикер); Поиск по тикеру |
| select | flt-venue | Биржа: все |
| select | flt-stage | Этап: все |
| select | flt-direction | Направление: все Рост Снижение Разные контексты |
| select | h1-points | Последние 20 История Скрыть |
| input | h1-diagnostic | checkbox |
| input | h1-breaks | checkbox |
| input | h1-expected | checkbox |
| input | h1-zones | checkbox |
| input | h1-ob | checkbox |
| input | h1-fvg | checkbox |
| input | h1-bsl | checkbox |
| input | h1-ssl | checkbox |
| input | h1-eligible-only | checkbox |
| select | h1-idea | Все HTF-идеи |
| button | h1-close-idea | Закрыть выбранную идею |
| input | h1-candidates | checkbox |
| input | h1-zone-history | checkbox |
| input | h1-htf | checkbox |
| input | data-layer=structure | checkbox |
| input | data-layer=liquidity | checkbox |
| input | data-layer=excluded | checkbox |
| input | data-layer=history | checkbox |
| input | data-layer=provisional | checkbox |
| button | h1-offscreen | Вне экрана: 0 |
| button | ltf-card-close | × |
| button | data-panel=eligible | Подходящие зоны |
| button | data-panel=events | События |
| button | data-panel=history | История |
| button | ltf-bottom-collapse | Свернуть |
| select | flt-hist-view | Исключённые (текущая версия) Все версии |
| select | flt-hist-reason | Причина: все |
| button | ltf-close-confirm | Завершить |
| button | ltf-close-cancel | Отмена |
| button | ltf-settings-save | Сохранить |
| button | ltf-settings-cancel | Закрыть |

## Параметры SETTINGS_META

Все дополнительные ключи detector из серверного ответа также обязательны: отсутствие META не означает, что ключ можно скрыть или удалить.

| Ключ | Текущая подпись | Группа |
|---|---|---|
| lookback_days | Глубина первичного поиска (дней, fallback) | Поиск зон |
| lookback_days_d1 | Глубина истории D1 (дней) | Поиск зон |
| lookback_days_w1 | Глубина истории W1 (дней) | Поиск зон |
| scan_timeframes | Таймфреймы поиска (через запятую) | Поиск зон |
| pivot_left | Пивот: свечей слева | Поиск зон |
| pivot_right | Пивот: свечей справа | Поиск зон |
| approach_pct | Порог приближения к зоне (доля) | Касания и глубина |
| depth_mid | Глубина «ослаблен» (доля) | Касания и глубина |
| depth_worked | Глубина «отработан» (доля) | Касания и глубина |
| suppress_hours | Пауза повторных уведомлений (часов) | Уведомления |
| delivery_target_seconds | Цель доставки уведомления (сек) | Уведомления |
| notification_digest_seconds | Интервал тихой сводки (сек) | Уведомления |
| notify_only_reviewed | Уведомлять только о подтверждённых | Уведомления |
| ltf_notify_kinds | События структуры H1 | Уведомления |
| uncalibrated_cluster_denominator | Знаменатель допуска кластера экстремумов | Не калибровано |
| cluster_tolerance_pct | Допуск объединения экстремумов (доля) | Поиск зон |
| uncalibrated_consolidation_max_candles | Максимум свечей в базе OB | Не калибровано |
| uncalibrated_consolidation_overlap_pct | Допуск перекрытия свечей базы (доля) | Не калибровано |
| uncalibrated_ob_delay_max_candles | Предел отложенного FVG (свечей) | Не калибровано |
| uncalibrated_plateau_equal_peaks | Учитывать плато с равными пиками | Не калибровано |
| uncalibrated_approach_base | База расчёта приближения | Не калибровано |
| rule_version | Версия правил | Служебное |

## HTTP / WebSocket контракты

Не означает, что каждый endpoint нужно делать отдельной кнопкой. Сопоставить существующие действия и сохранить payload, permissions и error handling.

| Метод | Путь | Источник |
|---|---|---|
| GET | /api/instruments | app/web/api.py |
| POST | /api/instruments | app/web/api.py |
| POST | /api/instruments/{instrument_id}/toggle | app/web/api.py |
| POST | /api/instruments/{instrument_id}/active | app/web/api.py |
| POST | /api/instruments/{instrument_id}/ltf-analyze | app/web/api.py |
| GET | /api/zones | app/web/api.py |
| GET | /api/zones/grouped | app/web/api.py |
| POST | /api/zones/manual | app/web/api.py |
| GET | /api/zones/{zone_id} | app/web/api.py |
| GET | /api/zones/{zone_id}/inner-levels | app/web/api.py |
| PATCH | /api/zones/{zone_id} | app/web/api.py |
| POST | /api/zones/{zone_id}/review | app/web/api.py |
| POST | /api/zones/{zone_id}/prefer-entry | app/web/api.py |
| POST | /api/zones/{zone_id}/reconcile | app/web/api.py |
| GET | /api/candidates | app/web/api.py |
| GET | /api/events | app/web/api.py |
| GET | /api/journal | app/web/api.py |
| GET | /api/candles | app/web/api.py |
| GET | /api/settings | app/web/api.py |
| POST | /api/settings | app/web/api.py |
| GET | /api/labels | app/web/api.py |
| GET | /api/export/labels | app/web/api.py |
| GET | /api/export/reviews | app/web/api.py |
| GET | /api/health | app/web/api.py |
| GET | /api/diagnostics | app/web/api.py |
| WEBSOCKET | /ws | app/web/api.py |
| GET | /api/ltf/observations | app/web/ltf_api.py |
| GET | /api/ltf/observations/{observation_id} | app/web/ltf_api.py |
| GET | /api/ltf/observations/{observation_id}/chart | app/web/ltf_api.py |
| GET | /api/ltf/instruments | app/web/ltf_api.py |
| GET | /api/ltf/instruments/{instrument_id}/current | app/web/ltf_api.py |
| GET | /api/ltf/instruments/{instrument_id}/structure | app/web/ltf_api.py |
| POST | /api/ltf/instruments/{instrument_id}/select-context | app/web/ltf_api.py |
| GET | /api/ltf/observations/{observation_id}/journal | app/web/ltf_api.py |
| GET | /api/ltf/scenarios/{scenario_id}/entries | app/web/ltf_api.py |
| GET | /api/ltf/scenarios/{scenario_id}/journal | app/web/ltf_api.py |
| POST | /api/ltf/ideas/{idea_id}/close | app/web/ltf_api.py |
| POST | /api/ltf/scenarios/{scenario_id}/close | app/web/ltf_api.py |
| POST | /api/ltf/entry-zones/{entry_zone_id}/review | app/web/ltf_api.py |
| GET | /api/alt/setups | app/web/alt_api.py |
| GET | /api/alt/setup/{setup_id} | app/web/alt_api.py |
| GET | /api/alt/candidate/{candidate_id} | app/web/alt_api.py |
| GET | /api/alt/asset/{asset_id}/ranges | app/web/alt_api.py |
| POST | /api/alt/ranges/{subject_kind}/{subject_id}/preview | app/web/alt_api.py |
| POST | /api/alt/ranges/{subject_kind}/{subject_id}/revisions | app/web/alt_api.py |
| GET | /api/alt/ranges/{subject_kind}/{subject_id}/revisions | app/web/alt_api.py |
| POST | /api/alt/ranges/{subject_kind}/{subject_id}/restore-auto | app/web/alt_api.py |
| GET | /api/alt/run-status | app/web/alt_api.py |
| POST | /api/alt/recalc | app/web/alt_api.py |
| GET | /api/events/overview | app/web/events_api.py |
| GET | /api/events/health | app/web/events_api.py |
| GET | /api/events/journal | app/web/events_api.py |
| GET | /api/events/calendar | app/web/events_api.py |
| GET | /api/events/rules | app/web/events_api.py |
| GET | /api/events/derivatives | app/web/events_api.py |
| GET | /api/events/situations | app/web/events_api.py |
| GET | /api/events/situations/{setup}/gate-history | app/web/events_api.py |
| GET | /api/ltf/events/{event_id}/context | app/web/events_api.py |
| POST | /api/events/refresh | app/web/events_api.py |

## Серверные настройки DetectorConfig

Ниже ключи из класса конфигурации. Настройки UI строить по реальному GET /api/settings, его groups, списку deprecated и применимости. Не выводить секреты/параметры подключения из общего Settings.

`lookback_days`, `lookback_days_d1`, `lookback_days_w1`, `suppress_hours`, `notification_digest_seconds`, `approach_pct`, `cluster_tolerance_pct`, `depth_mid`, `depth_worked`, `pivot_left`, `pivot_right`, `delivery_target_seconds`, `uncalibrated_cluster_denominator`, `uncalibrated_consolidation_max_candles`, `uncalibrated_consolidation_overlap_pct`, `uncalibrated_ob_delay_max_candles`, `uncalibrated_plateau_equal_peaks`, `uncalibrated_breakout_fvg_back_candles`, `uncalibrated_breakout_fvg_delay_candles`, `uncalibrated_approach_base`, `notify_only_reviewed`, `scan_timeframes`, `rule_version`, `entry_reuse_max_depth`, `inner_level_pivot_left`, `inner_level_pivot_right`, `ltf_enabled`, `ltf_structure_left`, `ltf_structure_right`, `ltf_range_right`, `ltf_entry_types`, `htf_context_types`, `htf_context_wait_hours`, `htf_context_wait_hours_d1`, `htf_context_wait_hours_w1`, `ltf_provisional_range_enabled`, `ltf_range_anchor_policy`, `ltf_notify_kinds`, `ltf_poll_seconds`, `ltf_live_grace_seconds`, `ltf_history_days`, `ltf_observation_stale_days`, `stale_quote_seconds`, `stale_h1_intervals`, `stale_d1_intervals`, `stale_w1_intervals`

## Динамические функции для ручной сверки

- app.js: zone details/relationships/inner levels; review lifecycle и assessments; reconcile, prefer-entry; anchor selection; draw mode; server group settings; legacy view routing и splitters.
- h1_layers.js: каждый сохранённый flag, points history/recent/hidden, diagnostic и offscreen objects.
- ltf.js и ltf_api.py: historical observation, causal chart snapshot, current structure, context selection, scenario entries/journal и close.
- alt.js и alt_chart.js: table columns, list reveal, process status, setup/candidate selection, range preview/revision history/restore-auto, chart modes/layers/exports.
- common.js / now.js / journal.js / events.js: auth, formatting, freshness, selection, translations, per-source failures.
- notify/navigation.py и Telegram callbacks: реальные deep links и параметры.

Исполнитель обязан дополнить FUNCTION_MAP фактическими callbacks и tests. Этот реестр не объявляет технические диагностические endpoints обязательными пользовательскими экранами.
