PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instrument (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    venue TEXT NOT NULL,
    market_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    precision INTEGER NOT NULL DEFAULT 8,
    enabled INTEGER NOT NULL DEFAULT 1,
    -- «Анализировать» (настройки LTF): наблюдения открываются на все
    -- подтверждённые OB D1/W1 инструмента сразу, без касания
    ltf_analyze INTEGER NOT NULL DEFAULT 1,
    UNIQUE (venue, market_type, symbol)
);

CREATE TABLE IF NOT EXISTS candle (
    instrument_id INTEGER NOT NULL REFERENCES instrument(id),
    timeframe TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    close_time INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    closed INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, open_time)
);

CREATE TABLE IF NOT EXISTS zone (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id INTEGER NOT NULL REFERENCES instrument(id),
    type TEXT NOT NULL,
    direction TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    lower REAL NOT NULL,
    upper REAL NOT NULL,
    formed_at INTEGER NOT NULL,
    confirmed_at INTEGER,
    status TEXT NOT NULL,
    cycle_id INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'auto',
    rule_version TEXT NOT NULL DEFAULT '0.2',
    evidence TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL DEFAULT 0,
    -- §15.1.3: границы рисунка на графике и причина завершения
    display_from INTEGER,
    display_until INTEGER,
    end_reason TEXT,
    -- §6/§15.6: состояние машины OB → Breaker
    breakout_close_at INTEGER,
    breaker_forbidden INTEGER NOT NULL DEFAULT 0,
    breakout_expired INTEGER NOT NULL DEFAULT 0,
    -- §15.7: история после подтверждения неполна — нужен replay
    needs_replay INTEGER NOT NULL DEFAULT 0,
    -- ТЗ «Единый движок» (22.09.2026) §3: актуальность отдельно от пригодности
    market_validity TEXT NOT NULL DEFAULT 'active',  -- active | invalid
    max_test_depth REAL NOT NULL DEFAULT 0,          -- макс. глубина самостоятельных тестов
    has_tests INTEGER NOT NULL DEFAULT 0,
    entry_eligible INTEGER NOT NULL DEFAULT 1,
    -- ТЗ §7: ручные зоны — выбранное начало на графике и типовое правило
    anchor_time INTEGER,
    zone_type TEXT,
    test_extreme REAL                   -- глубочайший экстремум тестов (точные сравнения 90%)
);
-- идемпотентность детектора: один и тот же объект не создаётся дважды при replay/restart
CREATE UNIQUE INDEX IF NOT EXISTS ux_zone_dedup
    ON zone (instrument_id, type, direction, timeframe, lower, upper, formed_at, cycle_id);
CREATE INDEX IF NOT EXISTS ix_zone_active ON zone (instrument_id, status);

-- ТЗ «Единый движок» §5: внутренние уровни ликвидности после теста OB.
-- Кандидат — до закрытия i+3 (confirmed_at NULL); снятые остаются в истории.
CREATE TABLE IF NOT EXISTS inner_level (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_ob_id INTEGER NOT NULL REFERENCES zone(id),
    instrument_id INTEGER NOT NULL REFERENCES instrument(id),
    timeframe TEXT NOT NULL,              -- ТФ экстремумов (D1/H1 раздельно)
    kind TEXT NOT NULL,                   -- bsl | ssl
    price REAL NOT NULL,                  -- экстремум теста
    pivot_time INTEGER NOT NULL,          -- свеча экстремума
    confirmed_at INTEGER,                 -- закрытие i+3; NULL — кандидат
    source_test_id INTEGER,               -- visit.id породившего теста
    status TEXT NOT NULL DEFAULT 'candidate',  -- candidate | active | taken
    taken_at INTEGER,
    evidence TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE (parent_ob_id, timeframe, kind, price, pivot_time)
);
CREATE INDEX IF NOT EXISTS ix_inner_level_ob ON inner_level (parent_ob_id, status);

CREATE TABLE IF NOT EXISTS zone_relation (
    zone_id INTEGER PRIMARY KEY REFERENCES zone(id),
    parent_ob_id INTEGER,
    confirming_fvg_id INTEGER,
    predecessor_ob_id INTEGER,
    visual_group_id INTEGER
);

CREATE TABLE IF NOT EXISTS visit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES zone(id),
    cycle_id INTEGER NOT NULL,
    entered_at INTEGER NOT NULL,
    exited_at INTEGER,
    max_depth REAL NOT NULL DEFAULT 0,
    observed INTEGER NOT NULL DEFAULT 1,
    exit_kind TEXT,
    extreme REAL,                       -- экстремум захода (ТЗ §4)
    d_raw REAL                          -- исходная глубина до clamp (ТЗ §4)
);
CREATE INDEX IF NOT EXISTS ix_visit_zone ON visit (zone_id, cycle_id);

CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES zone(id),
    cycle_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    detected_at INTEGER NOT NULL,
    price REAL NOT NULL,
    depth REAL NOT NULL DEFAULT 0,
    delayed INTEGER NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL DEFAULT '{}',
    UNIQUE (zone_id, cycle_id, kind, occurred_at)
);
CREATE INDEX IF NOT EXISTS ix_event_time ON event (occurred_at);

CREATE TABLE IF NOT EXISTS delivery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ids TEXT NOT NULL,
    destination TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    delivered_at INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS alert_state (
    zone_id INTEGER NOT NULL,
    cycle_id INTEGER NOT NULL,
    event_kind TEXT NOT NULL,
    user TEXT NOT NULL DEFAULT 'owner',
    last_delivered_at INTEGER NOT NULL,
    muted_until INTEGER,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (zone_id, cycle_id, event_kind, user)
);

CREATE TABLE IF NOT EXISTS review (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES zone(id),
    decision TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT 'owner',
    text TEXT NOT NULL DEFAULT '',
    boundary_version INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL DEFAULT 0
);

-- §15.1.1/§15.3 (R01, R13): раздельная оценка геометрии и актуальности.
-- Текст комментария не дублируется — он в review (§12).
CREATE TABLE IF NOT EXISTS review_assessment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES zone(id),
    review_id INTEGER NOT NULL REFERENCES review(id),
    review_decision TEXT NOT NULL,
    geometry_verdict TEXT NOT NULL,      -- valid | invalid | needs_correction | unknown
    lifecycle_verdict TEXT,              -- completed | converted | NULL
    reason_code TEXT NOT NULL DEFAULT '',
    evidence_source TEXT NOT NULL DEFAULT 'manual_ui',
    assessed_as_of INTEGER NOT NULL DEFAULT 0,
    reviewed_at INTEGER NOT NULL DEFAULT 0,
    requires_clarification INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_review_assessment_zone ON review_assessment (zone_id);

-- §15.2 (R07/R08): версионированная правка границ с точной свечой-якорем
CREATE TABLE IF NOT EXISTS boundary_correction (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES zone(id),
    boundary_version INTEGER NOT NULL,
    original_lower REAL NOT NULL,
    original_upper REAL NOT NULL,
    corrected_lower REAL NOT NULL,
    corrected_upper REAL NOT NULL,
    anchor_candle_open_time INTEGER,     -- open_time свечи-якоря; NULL — не указан
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_boundary_correction_zone ON boundary_correction (zone_id);

-- ============================================================================
-- LTF Confirmations (LTF_Confirmations_Window_Spec_v0.2.md, §12)
-- Все операторы IF NOT EXISTS: Database.migrate() применяет этот файл целиком
-- и к существующим БД.
-- ============================================================================

-- Наблюдение структуры H1 после достижения HTF-зоны (§4).
-- UNIQUE(zone_id, cycle_id): повторное HTF-событие не дублирует наблюдение.
CREATE TABLE IF NOT EXISTS ltf_observation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id INTEGER NOT NULL REFERENCES instrument(id),
    zone_id INTEGER NOT NULL REFERENCES zone(id),
    zone_version INTEGER NOT NULL DEFAULT 1,
    cycle_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'waiting_structure',
    activated_at INTEGER NOT NULL,
    data_quality TEXT NOT NULL DEFAULT 'live',
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL DEFAULT '{}',
    UNIQUE (zone_id, cycle_id)
);
CREATE INDEX IF NOT EXISTS ix_ltf_observation_state ON ltf_observation (state, instrument_id);
CREATE INDEX IF NOT EXISTS ix_ltf_observation_instr ON ltf_observation (instrument_id);

-- Направленный LTF-сценарий внутри наблюдения (§6)
CREATE TABLE IF NOT EXISTS ltf_scenario (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES ltf_observation(id),
    direction TEXT NOT NULL,
    "trigger" TEXT NOT NULL,            -- BOS | SMS
    stage TEXT NOT NULL,                -- primary | secondary
    state TEXT NOT NULL DEFAULT 'range_pending',
    trigger_event_id INTEGER,
    cancellation_reason TEXT,
    cancelled_at INTEGER,
    -- §5/§12: причинная цепочка — триггерное событие, движение, эпоха
    origin_break_event_id INTEGER,
    origin_movement_id INTEGER,
    structural_epoch_id INTEGER NOT NULL DEFAULT 1,
    -- §5/§12: уровень отмены как хранимый факт с происхождением (pivot машины)
    reverse_break_level_price REAL,
    reverse_break_pivot_id INTEGER,
    reverse_break_confirmed_at INTEGER,
    last_processed_close INTEGER,       -- ms: close_time последней обработанной H1
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ltf_scenario_obs ON ltf_scenario (observation_id, state);

-- Durable HTF theses, independent of the current LTF entry attempt.
-- The source scenario is immutable provenance, not the thesis lifecycle.
CREATE TABLE IF NOT EXISTS htf_idea (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL REFERENCES instrument(id),
    scenario_id INTEGER NOT NULL UNIQUE REFERENCES ltf_scenario(id) ON DELETE CASCADE,
    rule_version TEXT NOT NULL,
    payload TEXT NOT NULL,
    manual_closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_htf_idea_instrument ON htf_idea(instrument_id);

-- Локальные экстремумы H1 и структурные роли (§5.1, §5.2)
CREATE TABLE IF NOT EXISTS ltf_pivot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id INTEGER NOT NULL REFERENCES instrument(id),
    price REAL NOT NULL,
    kind TEXT NOT NULL,                 -- high | low
    pivot_at INTEGER NOT NULL,          -- ms: свеча экстремума
    confirmed_at INTEGER,               -- ms: закрытие i+r; NULL — кандидат
    role TEXT NOT NULL DEFAULT 'none',  -- HH | HL | LH | LL | none
    role_assigned_at INTEGER,
    "left" INTEGER NOT NULL DEFAULT 3,  -- настройки l/r (§5.1)
    "right" INTEGER NOT NULL DEFAULT 3,
    candle_open_time INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'candidate',  -- candidate | confirmed | ambiguous
    calc_version_id INTEGER,            -- версия расчёта (L03)
    superseded_by INTEGER               -- id версии, заменившей опору; NULL — действующая
);
CREATE INDEX IF NOT EXISTS ix_ltf_pivot_instr ON ltf_pivot (instrument_id, pivot_at);

-- L03: версии расчётов (параметры + правила); опоры и результаты ссылаются
CREATE TABLE IF NOT EXISTS calc_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                 -- ltf_structure | ...
    params TEXT NOT NULL DEFAULT '{}',  -- JSON параметров расчёта
    rule_version TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

-- §5.2: при пересмотре роли история сохраняется
CREATE TABLE IF NOT EXISTS ltf_pivot_role_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pivot_id INTEGER NOT NULL REFERENCES ltf_pivot(id),
    old_role TEXT,
    new_role TEXT NOT NULL,
    changed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ltf_pivot_role_log_pivot ON ltf_pivot_role_log (pivot_id);

-- Сломы структуры BOS/SMS (§6).
-- UNIQUE(scenario_id, level_key, stage): один слом на уровень/этап — §6
CREATE TABLE IF NOT EXISTS ltf_structure_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id INTEGER NOT NULL REFERENCES ltf_scenario(id),
    kind TEXT NOT NULL,                 -- BOS | SMS
    stage TEXT NOT NULL,                -- primary | secondary
    direction TEXT NOT NULL,
    break_level REAL NOT NULL,
    break_candle_open_time INTEGER NOT NULL,
    occurred_at INTEGER NOT NULL,
    detected_at INTEGER NOT NULL,
    ref_pivot_ids TEXT NOT NULL DEFAULT '[]',
    accompanying INTEGER NOT NULL DEFAULT 0,  -- §6.5: SMS сопутствующий при BOS
    level_key TEXT NOT NULL,            -- стабильный ключ уровня для дедупликации
    evidence TEXT NOT NULL DEFAULT '{}',
    UNIQUE (scenario_id, level_key, stage)
);
CREATE INDEX IF NOT EXISTS ix_ltf_structure_event_scenario ON ltf_structure_event (scenario_id);

-- Причинное движение, приведшее к конкретному BOS/SMS (§8.1)
CREATE TABLE IF NOT EXISTS ltf_movement (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id INTEGER NOT NULL REFERENCES ltf_scenario(id),
    start_pivot_id INTEGER NOT NULL,
    end_pivot_id INTEGER NOT NULL,
    start_at INTEGER NOT NULL,
    end_at INTEGER NOT NULL,
    break_event_id INTEGER,
    confirmed_at INTEGER,
    source_candle_ids TEXT NOT NULL DEFAULT '[]',
    provenance_status TEXT NOT NULL DEFAULT 'ok'  -- ok | ambiguous
);
CREATE INDEX IF NOT EXISTS ix_ltf_movement_scenario ON ltf_movement (scenario_id);

-- Версии диапазона Premium/Discount (§7): старые версии не переписываются
CREATE TABLE IF NOT EXISTS ltf_range (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id INTEGER NOT NULL REFERENCES ltf_scenario(id),
    version INTEGER NOT NULL,
    lower REAL NOT NULL,
    upper REAL NOT NULL,
    mid REAL NOT NULL,
    anchor_low_pivot_id INTEGER,
    anchor_high_pivot_id INTEGER,
    available_at INTEGER NOT NULL,
    prev_version_id INTEGER,
    -- §5/§12: continuation — диапазон продолжения; origin_reversal —
    -- зарезервировано для следующего этапа; anchor_policy — политика якорей
    kind TEXT NOT NULL DEFAULT 'continuation',
    anchor_policy TEXT,
    structural_epoch_id INTEGER NOT NULL DEFAULT 1,
    UNIQUE (scenario_id, version)
);
CREATE INDEX IF NOT EXISTS ix_ltf_range_scenario ON ltf_range (scenario_id, version);

-- Entry Zones H1 (§8). movement_id NOT NULL DEFAULT 0: NULL в UNIQUE не дедупит
CREATE TABLE IF NOT EXISTS ltf_entry_zone (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id INTEGER NOT NULL REFERENCES instrument(id),
    type TEXT NOT NULL,                 -- OB | FVG | BSL | SSL
    direction TEXT NOT NULL,
    lower REAL NOT NULL,
    upper REAL NOT NULL,                -- для уровня равна lower
    formed_at INTEGER NOT NULL,
    confirmed_at INTEGER,
    movement_id INTEGER NOT NULL DEFAULT 0,
    first_test_at INTEGER,
    validity TEXT NOT NULL DEFAULT 'fresh',  -- fresh | tested | invalid
    -- ТЗ «Единый движок» §3/§4: максимум глубины тестов за всю историю и
    -- глубочайший экстремум (точные сравнения порога 90% без epsilon)
    max_test_depth REAL NOT NULL DEFAULT 0,
    test_extreme REAL,
    source TEXT NOT NULL DEFAULT 'auto',
    rule_version TEXT NOT NULL DEFAULT 'ltf-0.2',
    evidence TEXT NOT NULL DEFAULT '{}',
    UNIQUE (instrument_id, type, direction, lower, upper, formed_at, movement_id)
);
CREATE INDEX IF NOT EXISTS ix_ltf_entry_zone_instr ON ltf_entry_zone (instrument_id, validity);
CREATE INDEX IF NOT EXISTS ix_ltf_entry_zone_movement ON ltf_entry_zone (movement_id);

-- Привязка Entry Zone к сценарию с версией диапазона (§7, §8.5)
CREATE TABLE IF NOT EXISTS ltf_scenario_entry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id INTEGER NOT NULL REFERENCES ltf_scenario(id),
    entry_zone_id INTEGER NOT NULL REFERENCES ltf_entry_zone(id),
    range_version INTEGER NOT NULL,
    eligible INTEGER NOT NULL DEFAULT 1,
    overlap TEXT NOT NULL DEFAULT 'none',   -- full | partial | none
    state TEXT NOT NULL DEFAULT 'fresh',    -- fresh | out_of_range | tested | invalid
    -- ТЗ «LTF Current Setup» §10: стабильный код пригодности
    -- (ok | outside_pd | tested_too_deep | type_disabled | invalid |
    --  swept_level | origin_unresolved | range_pending); '' — до миграции
    reason TEXT NOT NULL DEFAULT '',
    added_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE (scenario_id, entry_zone_id, range_version)
);
CREATE INDEX IF NOT EXISTS ix_ltf_scenario_entry_scenario ON ltf_scenario_entry (scenario_id, state);

-- Двухэтапное снятие BSL/SSL: снятие и закрытие той же H1-свечи (§10)
CREATE TABLE IF NOT EXISTS ltf_liquidity_test (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_zone_id INTEGER NOT NULL REFERENCES ltf_entry_zone(id),
    scenario_id INTEGER NOT NULL REFERENCES ltf_scenario(id),
    level REAL NOT NULL,
    touch_at INTEGER NOT NULL,
    candle_open_time INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'awaiting_close',
    close_price REAL,
    sweep_at INTEGER,
    resolved_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ltf_liquidity_test_state ON ltf_liquidity_test (state, scenario_id);
CREATE INDEX IF NOT EXISTS ix_ltf_liquidity_test_zone ON ltf_liquidity_test (entry_zone_id);

-- Журнал событий окна LTF (§11). UNIQUE(dedupe_key) — подавление дублей §11.5
CREATE TABLE IF NOT EXISTS ltf_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES ltf_observation(id),
    scenario_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    occurred_at INTEGER NOT NULL,
    detected_at INTEGER NOT NULL,
    dedupe_key TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    delayed INTEGER NOT NULL DEFAULT 0,
    -- F01/A01: происхождение события (live|catchup|replay|unknown) и лаг
    -- обнаружения относительно закрытия свечи; delayed = processing_mode != 'live'
    processing_mode TEXT NOT NULL DEFAULT 'unknown',
    detection_lag_ms INTEGER NOT NULL DEFAULT 0,
    -- ТЗ 07.10.2026 §7: состояние графика события отдельно от состояния
    -- доставки текста (none | sent | failed + число попыток)
    chart_state TEXT NOT NULL DEFAULT 'none',
    chart_attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE (dedupe_key)
);
CREATE INDEX IF NOT EXISTS ix_ltf_event_obs ON ltf_event (observation_id, occurred_at);
CREATE INDEX IF NOT EXISTS ix_ltf_event_scenario ON ltf_event (scenario_id);

-- Ручная разметка Entry Zones (аналог review/review_assessment HTF-зон).
-- Оценка только фиксируется: validity зоны, привязки ltf_scenario_entry
-- и движок она не меняет — чистый датасет для анализа.
-- scenario_id без FK: сценарий может быть удалён, разметка остаётся.
CREATE TABLE IF NOT EXISTS ltf_review (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_zone_id INTEGER NOT NULL REFERENCES ltf_entry_zone(id),
    scenario_id INTEGER,
    decision TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT 'owner',
    text TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ltf_review_zone ON ltf_review (entry_zone_id);

-- Раздельная оценка геометрии и актуальности (§15.3 HTF-спеки, R01/R13).
-- corrected_lower/upper — исправленные границы при fix_boundaries: только
-- запись в датасет, границы самой зоны не меняются.
CREATE TABLE IF NOT EXISTS ltf_review_assessment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_zone_id INTEGER NOT NULL REFERENCES ltf_entry_zone(id),
    review_id INTEGER NOT NULL REFERENCES ltf_review(id),
    review_decision TEXT NOT NULL,
    geometry_verdict TEXT NOT NULL,      -- valid | invalid | needs_correction | unknown
    lifecycle_verdict TEXT,              -- tested | NULL
    reason_code TEXT NOT NULL DEFAULT '',
    evidence_source TEXT NOT NULL DEFAULT 'manual_ui',
    assessed_as_of INTEGER NOT NULL DEFAULT 0,
    reviewed_at INTEGER NOT NULL DEFAULT 0,
    requires_clarification INTEGER NOT NULL DEFAULT 0,
    corrected_lower REAL,
    corrected_upper REAL
);
CREATE INDEX IF NOT EXISTS ix_ltf_review_assessment_zone ON ltf_review_assessment (entry_zone_id);

-- Telegram-бот (ТЗ п.8): список наблюдения владельца бота.
-- chat_id — ключ владельца (модель «один владелец», но схема multi-user-ready);
-- alerts_enabled=0 — торговые уведомления по инструменту не доставляются.
CREATE TABLE IF NOT EXISTS bot_watchlist (
    chat_id TEXT NOT NULL,
    instrument_id INTEGER NOT NULL REFERENCES instrument(id),
    alerts_enabled INTEGER NOT NULL DEFAULT 1,
    added_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, instrument_id)
);

-- Telegram-бот (ТЗ п.9): настройки групп/видов уведомлений.
-- scope: global (scope_ref='') | instrument (scope_ref=instrument_id) |
-- context (scope_ref=zone_id); grp: htf | ltf | entry | scenario | service.
-- Дефолт — включено: строка появляется при первом изменении переключателя.
CREATE TABLE IF NOT EXISTS bot_alert_pref (
    chat_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_ref TEXT NOT NULL DEFAULT '',
    grp TEXT NOT NULL,
    kind TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chat_id, scope, scope_ref, grp, kind)
);

-- Telegram-бот (ТЗ п.9): мьютинг доставки (только доставка — расчёт,
-- история и актуальность зон продолжаются). scope: all | instrument | zone.
CREATE TABLE IF NOT EXISTS bot_mute (
    chat_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_ref TEXT NOT NULL DEFAULT '',
    until INTEGER NOT NULL,
    PRIMARY KEY (chat_id, scope, scope_ref)
);

-- ===========================================================================
-- Модуль «Altcoins D1 accumulation» (изолирован; все таблицы alt_*).
-- Время — ms UTC (int), цены — REAL, как в остальных таблицах.
-- Вся схема через CREATE ... IF NOT EXISTS: безопасно применяется целиком
-- к существующим БД из Database.migrate().
-- ===========================================================================

-- Актив вселенной альткоинов; cmc_id — ключ CoinMarketCap
CREATE TABLE IF NOT EXISTS alt_asset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cmc_id INTEGER NOT NULL UNIQUE,
    canonical_asset_id TEXT,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    cmc_rank INTEGER NOT NULL DEFAULT 0,
    exclusion_category TEXT,
    mapping_status TEXT NOT NULL DEFAULT 'pending',  -- pending | mapped | mapping_pending | manual
    mapping_reason TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_ms INTEGER NOT NULL DEFAULT 0,
    updated_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alt_asset_rank ON alt_asset (cmc_rank);

-- Снапшот вселенной CMC, на котором работал прогон джобы
CREATE TABLE IF NOT EXISTS alt_universe_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'cmc',
    stale INTEGER NOT NULL DEFAULT 0
);

-- Источник свечей D1 актива; версия источника — часть ключа (смена
-- источника истории создаёт новую строку, старые свечи не переписываются)
CREATE TABLE IF NOT EXISTS alt_instrument_source (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES alt_asset(id),
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quote TEXT NOT NULL DEFAULT 'USDT',
    earliest_available_ms INTEGER NOT NULL DEFAULT 0,
    last_closed_ms INTEGER NOT NULL DEFAULT 0,
    history_scope TEXT NOT NULL DEFAULT 'full',      -- full | partial
    source_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (asset_id, venue, symbol, quote, source_version)
);

-- Свечи D1 источника (таймфрейм модуля всегда D1)
CREATE TABLE IF NOT EXISTS alt_candle (
    source_id INTEGER NOT NULL REFERENCES alt_instrument_source(id),
    open_time INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (source_id, open_time)
);

-- Живой кандидат диапазона накопления (пересчитывается до заморозки);
-- origin_key — стабильный ключ пары якорей для дедупликации версий
CREATE TABLE IF NOT EXISTS alt_range_candidate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES alt_asset(id),
    origin_key TEXT NOT NULL,
    start_anchor_open_time INTEGER NOT NULL,
    rebound_anchor_open_time INTEGER NOT NULL,
    lower REAL NOT NULL,
    upper REAL NOT NULL,
    width REAL NOT NULL,
    mid REAL NOT NULL,
    n_days INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'searching',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    first_seen_ms INTEGER NOT NULL DEFAULT 0,
    updated_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alt_range_candidate_asset ON alt_range_candidate (asset_id, state);

-- Замороженная зрелая версия диапазона (геометрия фиксируется навсегда)
CREATE TABLE IF NOT EXISTS alt_frozen_range (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    range_id INTEGER NOT NULL REFERENCES alt_range_candidate(id),
    lower REAL NOT NULL,
    upper REAL NOT NULL,
    width REAL NOT NULL,
    mid REAL NOT NULL,
    start_anchor_open_time INTEGER NOT NULL,
    rebound_anchor_open_time INTEGER NOT NULL,
    included_candles INTEGER NOT NULL DEFAULT 0,
    mature_at_ms INTEGER NOT NULL DEFAULT 0,
    classifier_version TEXT NOT NULL DEFAULT 'v1',
    range_version INTEGER NOT NULL DEFAULT 1
);

-- Сетап накопления по замороженному диапазону; один актив — один сетап
-- на диапазон (UNIQUE(asset_id, range_id)). cancel_price — уровень K,
-- режим проверки отмены — cancel_mode (проектный выбор, см. AltConfig)
CREATE TABLE IF NOT EXISTS alt_setup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES alt_asset(id),
    source_id INTEGER NOT NULL REFERENCES alt_instrument_source(id),
    range_id INTEGER NOT NULL REFERENCES alt_frozen_range(id),
    state TEXT NOT NULL DEFAULT 'searching',
    flags_json TEXT NOT NULL DEFAULT '{}',
    confirmation_event_id INTEGER,
    targets_json TEXT NOT NULL DEFAULT '[]',
    cancel_price REAL,
    cancel_mode TEXT NOT NULL DEFAULT 'wick_on_closed_d1',
    cancel_reachable INTEGER NOT NULL DEFAULT 1,
    breakout_close REAL,
    breakout_closed_at INTEGER,
    retest_deadline_ms INTEGER,
    entry_a_id INTEGER,
    entry_b_id INTEGER,
    universe_eligible INTEGER NOT NULL DEFAULT 1,
    created_ms INTEGER NOT NULL DEFAULT 0,
    updated_ms INTEGER NOT NULL DEFAULT 0,
    terminated_ms INTEGER,
    UNIQUE (asset_id, range_id)
);
CREATE INDEX IF NOT EXISTS ix_alt_setup_state ON alt_setup (state);

-- Структурное событие D1 внутри сетапа: слом BOS/SMS или снятие SSL
CREATE TABLE IF NOT EXISTS alt_structure_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id INTEGER NOT NULL REFERENCES alt_setup(id),
    kind TEXT NOT NULL,                   -- BOS | SMS | SSL
    level_price REAL NOT NULL,
    close_price REAL NOT NULL,
    candle_open_time INTEGER NOT NULL,
    anchors_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_alt_structure_event_setup ON alt_structure_event (setup_id);

-- Эпизод манипуляции: уход цены под L диапазона и возврат
CREATE TABLE IF NOT EXISTS alt_manipulation_episode (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id INTEGER NOT NULL REFERENCES alt_setup(id),
    started_candle_open_time INTEGER NOT NULL,
    min_price REAL NOT NULL,
    ended_candle_open_time INTEGER,
    days_below INTEGER NOT NULL DEFAULT 0
);

-- Точка входа: A — на ретесте после пробоя, B — внутри диапазона
CREATE TABLE IF NOT EXISTS alt_entry_opportunity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id INTEGER NOT NULL REFERENCES alt_setup(id),
    kind TEXT NOT NULL,                   -- 'A' | 'B'
    event_time_ms INTEGER NOT NULL,
    price REAL,
    zone_json TEXT NOT NULL DEFAULT '{}',
    bases_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_alt_entry_opportunity_setup ON alt_entry_opportunity (setup_id);

-- Прогон джобы (раз в сутки по расписанию МСК)
CREATE TABLE IF NOT EXISTS alt_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ms INTEGER NOT NULL,
    finished_ms INTEGER,
    as_of_ms INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',   -- running | ok | error
    processed INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    universe_snapshot_id INTEGER,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

-- Outbox событий модуля; дедупликация — UNIQUE(setup_id, event_type,
-- source_event_id): повторная детекция того же рыночного факта не создаёт
-- вторую строку (аналог dedupe_key у ltf_event)
CREATE TABLE IF NOT EXISTS alt_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    event_time_ms INTEGER NOT NULL DEFAULT 0,
    detected_at_ms INTEGER NOT NULL DEFAULT 0,
    run_id INTEGER,
    delivered INTEGER NOT NULL DEFAULT 0,
    created_ms INTEGER NOT NULL DEFAULT 0,
    UNIQUE (setup_id, event_type, source_event_id)
);
CREATE INDEX IF NOT EXISTS ix_alt_event_delivered ON alt_event (delivered);

-- Эпизод диапазона v2 (ТЗ 07.10.2026 R-01/R-07/R-09): отдельная база
-- накопления со своими якорями, интервалами заливки (base_start..base_end)
-- и сопровождения, версией правил и замороженной геометрией. Экстремальные
-- тени хранятся отдельно от основных границ (R-03). origin_key UNIQUE —
-- повторный прогон не дублирует эпизод (R-10). Живёт рядом с v1-таблицами:
-- старые frozen range не редактируются
CREATE TABLE IF NOT EXISTS alt_range_episode (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES alt_asset(id),
    source_id INTEGER NOT NULL REFERENCES alt_instrument_source(id),
    origin_key TEXT NOT NULL UNIQUE,
    rules_version TEXT NOT NULL DEFAULT 'alt-0.2',
    state TEXT NOT NULL DEFAULT 'forming',
    anchor_start_open_time INTEGER NOT NULL,
    base_start_open_time INTEGER NOT NULL,
    base_end_open_time INTEGER,
    base_end_reason TEXT,                -- breakout_confirmed | decay | none
    base_end_confirmed_at_ms INTEGER,
    accompaniment_end_open_time INTEGER,
    lower REAL NOT NULL,
    upper REAL NOT NULL,
    mid REAL NOT NULL,
    width REAL NOT NULL,
    wick_low REAL,
    wick_high REAL,
    quality_json TEXT NOT NULL DEFAULT '{}',
    selection_rank_reason TEXT,
    detected_at_ms INTEGER NOT NULL DEFAULT 0,
    created_ms INTEGER NOT NULL DEFAULT 0,
    updated_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alt_range_episode_asset ON alt_range_episode (asset_id, state);

-- Вынос вниз под L эпизода v2 (R-05): отдельный факт, не расширяет
-- замороженную базу; возврат подтверждается закрытием D1 обратно в базу
CREATE TABLE IF NOT EXISTS alt_sweep_episode (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES alt_range_episode(id),
    start_open_time INTEGER NOT NULL,
    min_price REAL NOT NULL,
    min_open_time INTEGER NOT NULL,
    end_open_time INTEGER,
    return_confirmed INTEGER NOT NULL DEFAULT 0,
    return_confirmed_at_ms INTEGER,
    state TEXT NOT NULL DEFAULT 'open',  -- open | return_pending | returned | accepted_below
    created_ms INTEGER NOT NULL DEFAULT 0,
    updated_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alt_sweep_episode_episode ON alt_sweep_episode (episode_id);

-- Редактируемая геометрия диапазона. Исходный auto-диапазон остаётся в
-- alt_frozen_range/alt_range_candidate; активная manual-ревизия перекрывает
-- его во всех read-model и новых уведомлениях.
CREATE TABLE IF NOT EXISTS alt_range_revision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_kind TEXT NOT NULL,              -- setup | candidate
    subject_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'manual', -- manual | auto_restore
    lower REAL NOT NULL,
    upper REAL NOT NULL,
    mid REAL NOT NULL,
    width REAL NOT NULL,
    base_start_open_time INTEGER NOT NULL,
    base_end_open_time INTEGER,
    derived_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    expected_previous_revision INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_ms INTEGER NOT NULL DEFAULT 0,
    UNIQUE(subject_kind, subject_id, revision),
    UNIQUE(subject_kind, subject_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_alt_range_revision_active
    ON alt_range_revision(subject_kind, subject_id, active);

-- Presentation outbox: additive migration, never replays delivered history.
CREATE TABLE IF NOT EXISTS notification_packet (
    id INTEGER PRIMARY KEY,
    destination TEXT NOT NULL,
    channel TEXT NOT NULL,
    semantic_key TEXT NOT NULL,
    card TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    quiet INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    due_at INTEGER NOT NULL,
    sent_at INTEGER,
    message_id INTEGER,
    photo INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER NOT NULL DEFAULT 0,
    dirty INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    parent_id INTEGER REFERENCES notification_packet(id),
    UNIQUE(destination, channel, semantic_key)
);
CREATE INDEX IF NOT EXISTS ix_notification_due ON notification_packet(destination,status,due_at);
CREATE TABLE IF NOT EXISTS notification_member (
    id INTEGER PRIMARY KEY,
    packet_id INTEGER NOT NULL REFERENCES notification_packet(id),
    channel TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    reason TEXT,
    UNIQUE(packet_id,channel,event_id)
);
CREATE INDEX IF NOT EXISTS ix_notification_event ON notification_member(channel,event_id);
CREATE TABLE IF NOT EXISTS notification_fact (
    destination TEXT NOT NULL,
    channel TEXT NOT NULL,
    semantic_key TEXT NOT NULL,
    packet_id INTEGER NOT NULL REFERENCES notification_packet(id),
    PRIMARY KEY(destination,channel,semantic_key)
);
CREATE TABLE IF NOT EXISTS notification_incident (
    id INTEGER PRIMARY KEY,
    venue TEXT NOT NULL,
    reason TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    recovered_at INTEGER,
    packet_id INTEGER REFERENCES notification_packet(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_incident_open
    ON notification_incident(venue,reason) WHERE recovered_at IS NULL;
CREATE TABLE IF NOT EXISTS notification_incident_stream (
    incident_id INTEGER NOT NULL REFERENCES notification_incident(id),
    instrument_id INTEGER NOT NULL,
    timeframe TEXT NOT NULL,
    recovered INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(incident_id,instrument_id,timeframe)
);

-- Эпизод HTF-контекста живёт отдельно от lifecycle зоны.
-- Отработка FVG и снятие уровня эпизод не закрывают.
CREATE TABLE IF NOT EXISTS htf_context_episode (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    last_distinct_interaction_at INTEGER NOT NULL,
    candidate_direction TEXT NOT NULL,
    basis TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    invalidated_at INTEGER,
    invalid_reason TEXT,
    confirmed_scenario_id INTEGER,
    confirmed_at INTEGER,
    protected_price REAL,
    protected_candle_open INTEGER,
    protected_pivot_ref INTEGER,
    protected_kind TEXT,
    intrabar_order_unknown INTEGER NOT NULL DEFAULT 0,
    rule_version TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}',
    closed_at INTEGER,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_htf_context_episode_instr
    ON htf_context_episode(instrument_id, state);

CREATE TABLE IF NOT EXISTS htf_context_source (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL,
    zone_id INTEGER,
    zone_version INTEGER,
    interaction TEXT NOT NULL,
    interaction_at INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    evidence TEXT NOT NULL DEFAULT '{}',
    UNIQUE(episode_id, zone_id, interaction, interaction_at)
);
CREATE INDEX IF NOT EXISTS ix_htf_context_source_episode
    ON htf_context_source(episode_id);

-- Версии ноги LL → наблюдаемый high. Старая версия не переписывается,
-- поэтому исторический as_of не видит будущий максимум.
CREATE TABLE IF NOT EXISTS htf_context_leg (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    low REAL NOT NULL,
    high REAL NOT NULL,
    eq REAL NOT NULL,
    status TEXT NOT NULL,
    as_of INTEGER NOT NULL,
    high_candle_open INTEGER,
    frozen INTEGER NOT NULL DEFAULT 0,
    UNIQUE(episode_id, version)
);

-- Факт слома опоры. Карточка группируется по market_transition_key,
-- а не по этой строке: один бар может сломать несколько опор.
CREATE TABLE IF NOT EXISTS market_transition (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL,
    market_break_key TEXT NOT NULL,
    market_transition_key TEXT NOT NULL,
    direction TEXT NOT NULL,
    break_candle_open_time INTEGER NOT NULL,
    anchor_key TEXT NOT NULL,
    notification_kind TEXT NOT NULL DEFAULT 'market_transition',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    UNIQUE(market_break_key, notification_kind)
);
CREATE INDEX IF NOT EXISTS ix_market_transition_key
    ON market_transition(market_transition_key);
