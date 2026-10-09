"""Конфигурация сервиса HTF Zones.

Числовые пороги из §14 спеки (открытые решения) собраны в DetectorConfig
и помечены uncalibrated=True — это рабочие значения по умолчанию,
не согласованные с пользователем калибровки. Всё переопределяется через ENV.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from typing import Optional

logger = logging.getLogger(__name__)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def resolve_db_path(raw: str, *, volume_mount: Optional[str] = None) -> str:
    """Путь файла SQLite. На Railway том не перекрывает каталог образа.

    RAILWAY_VOLUME_MOUNT_PATH Railway выставляет сам, когда том прикреплён.
    Файл вне этого каталога живёт в слое контейнера и пропадает на деплое:
    относительный ``data/htf_zones.db`` (рабочий каталог образа) и абсолютный
    путь не из тома оба дают пустую базу и заново собранных кандидатов.
    Если том есть, файл кладётся в него. ``:memory:`` не трогаем.
    Сравнение строковое: пути Railway — POSIX, а Path('/data') на Windows
    не считается абсолютным.
    """
    configured = (raw or "").strip()
    if configured == ":memory:":
        return configured
    if volume_mount is None:
        volume_mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    mount = (volume_mount or "").strip().rstrip("/")
    if not mount:
        return configured or "data/htf_zones.db"
    posix = configured.replace("\\", "/")
    if posix == mount or posix.startswith(mount + "/"):
        return posix
    name = posix.rstrip("/").split("/")[-1]
    if name in ("", ".", ".."):
        name = "htf_zones.db"
    return f"{mount}/{name}"


_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def parse_bool(raw: object) -> bool:
    """Строгий парсер bool из ENV/JSON: bool("false") == True — ловушка."""
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in _BOOL_TRUE:
        return True
    if s in _BOOL_FALSE:
        return False
    raise ValueError(f"не булево значение: {raw!r}")


@dataclass
class DetectorConfig:
    """Параметры детектора. uncalibrated_* — открытые решения §14 спеки."""

    # Согласованные параметры
    lookback_days: int = 180                 # §1: fallback-глубина для ТФ без своего окна
    lookback_days_d1: int = 365              # §1: глубина истории D1 — год
    lookback_days_w1: int = 730              # §1: глубина истории W1 — 2 года
    suppress_hours: int = 120                # §8: 5 дней = 120 часов
    notification_digest_seconds: int = 900  # тихая сводка, сохраняется в outbox
    approach_pct: float = 0.02               # §9: приближение на 2%
    cluster_tolerance_pct: float = 0.02      # §7: допуск объединения экстремумов 2%
    depth_mid: float = 0.5                   # §2/§3: 50%
    depth_worked: float = 0.9                # §6: 90% — отработан
    pivot_left: int = 3                      # §7: 3+3
    pivot_right: int = 3
    delivery_target_seconds: int = 120       # §11: цель приёмки доставки

    # Открытые решения §14 — НЕ калиброваны, значения рабочие
    uncalibrated_cluster_denominator: str = "p_min"   # §7/§14.3: (p_max-p_min)/p_min
    uncalibrated_consolidation_max_candles: int = 12  # §14.1: макс. свечей в базе
    uncalibrated_consolidation_overlap_pct: float = 0.0  # §14.1: допуск перекрытия
    uncalibrated_ob_delay_max_candles: int = 60       # §14.2: предел отложенного FVG
    uncalibrated_plateau_equal_peaks: bool = False    # §14.3: плато с равными пиками
    # §15.6: привязка нового FVG пробойного движения — открыто для формализации
    uncalibrated_breakout_fvg_back_candles: int = 2   # FVG начинается не раньше свечи пробоя минус N
    uncalibrated_breakout_fvg_delay_candles: int = 10 # FVG может подтвердиться позже закрытия
    # §9: база расчёта 2% приближения — текущая цена (не согласовано)
    uncalibrated_approach_base: str = "price"

    # Поведение
    notify_only_reviewed: bool = False       # §10: «уведомлять только о подтверждённых»
    scan_timeframes: str = "D1,W1"          # §1: только HTF (H1/H4 убраны по решению пользователя)
    rule_version: str = "0.2"               # ТЗ «Единый движок HTF/LTF» от 22.09.2026

    # ТЗ «Единый движок» (22.09.2026)
    # §3: повторный выбор OB как Entry Zone допустим, пока максимальная глубина
    # самостоятельных тестов СТРОГО меньше порога (ровно 0.90 — уже недопустимо).
    # Консервативная формализация владельца: максимум за ВСЮ историю действующего
    # OB, мелкий поздний тест не стирает более глубокий прежний (ТЗ п.41).
    entry_reuse_max_depth: float = 0.9
    # §5: подтверждение внутренних BSL/SSL — общее правило 3+3 на ТФ уровня
    inner_level_pivot_left: int = 3
    inner_level_pivot_right: int = 3

    # LTF Confirmations (LTF-спека §14)
    ltf_enabled: bool = True                 # включить LTF-мониторинг
    ltf_structure_left: int = 3              # структурные pivots: 3–5 слева/справа
    ltf_structure_right: int = 3
    ltf_range_right: int = 3                 # УСТАРЕЛО, движком не применяется:
                                             # опоры диапазона подтверждаются
                                             # РОВНО 3 правыми закрытыми свечами
                                             # (RANGE_PIVOT_RIGHT, ТЗ «LTF Current
                                             # Setup» §3/§9). Поле сохранено для
                                             # совместимости settings.json/API
    ltf_entry_types: str = "FVG,OB,BSL,SSL"  # типы отображаемых Entry Zones
    # Какие HTF-зоны D1/W1 запускают LTF-наблюдение. OB и FVG равноправны:
    # любое пересечение валидной зоны запускает поиск разворота на H1.
    # PRB/Breaker/BSL/SSL триггерами не являются (для уровней нужны отдельные
    # направление ожидания и условие активации). Допустимые значения: OB, FVG.
    htf_context_types: str = "OB,FVG"
    # ТЗ §16.2 (предлагаемый режим, выключен по умолчанию): «Предварительный
    # диапазон» — read-only слой временного конца текущего движения до
    # подтверждения опор тремя правыми свечами. На подтверждённые диапазоны,
    # eligibility, отмену сценария и уведомления НЕ влияет.
    ltf_provisional_range_enabled: bool = False
    # §7 (Этап 4): политика якорей диапазона. origin_reversal (по умолчанию):
    # до первой валидной continuation-пары LH→LL / HL→HH диапазон строится от
    # якоря-источника причинного движения первичного слома (kind='origin_reversal',
    # роль HH/LL не переименовывается); continuation_only — прежнее поведение:
    # без валидной пары честный range_pending (чужой старый диапазон не показывается)
    ltf_range_anchor_policy: str = "origin_reversal"
    # типы доставки LTF (этап D); выключение не меняет рыночный анализ
    ltf_notify_kinds: str = "bos_sms,entries_ready,touch,sweep_outcome,cancellation"
    ltf_poll_seconds: int = 300              # свой цикл опроса H1
    # F01/A01: grace-окно живой обработки H1-закрытия. Событие, обнаруженное
    # в пределах окна после закрытия свечи, — live (доставляется: любой лаг
    # опроса, даже миллисекунды, не должен подавлять свежий сигнал); дольше —
    # catchup (догоняющий бэклог, доставка подавлена как у replay)
    ltf_live_grace_seconds: int = 900
    ltf_history_days: int = 30               # §4: H1-история до касания HTF
    ltf_observation_stale_days: int = 14     # автоархивация наблюдения без
                                             # активности дольше N дней (0 — выкл.)
    # D02: пороги устаревания — отдельно для котировок и каждого ТФ
    stale_quote_seconds: int = 0             # 0 — авто: 2 интервала опроса
    stale_h1_intervals: int = 2              # в интервалах ТФ (2 × H1)
    stale_d1_intervals: int = 2
    stale_w1_intervals: int = 2

    def lookback_days_for(self, timeframe: str) -> int:
        """Глубина первичного поиска для ТФ: D1 — год, W1 — 2 года."""
        return {
            "D1": self.lookback_days_d1,
            "W1": self.lookback_days_w1,
        }.get(timeframe, self.lookback_days)

    def ltf_entry_type_set(self) -> set[str]:
        """Включённые типы Entry Zones (ТЗ «LTF Current Setup» §10): настройка
        применяется к расчёту пригодности, API, графику, счётчикам и новым
        уведомлениям; отключение не удаляет зоны и историю."""
        return {
            t.strip().upper()
            for t in self.ltf_entry_types.split(",")
            if t.strip()
        }

    def htf_context_type_set(self) -> set[str]:
        """Типы HTF-контекста D1/W1, запускающие LTF-наблюдение (§16.1).

        Только OB/FVG: остальные типы движка (PRB/Breaker/BSL/SSL) из строки
        настройки отбрасываются — триггерами они не являются."""
        return {
            t.strip().upper()
            for t in self.htf_context_types.split(",")
            if t.strip()
        } & {"OB", "FVG"}


# ---------------------------------------------------------------------------
# L04: строгая схема настроек детектора
# ---------------------------------------------------------------------------

# Устаревшие поля: сохраняются для миграционной совместимости (settings.json,
# ENV, GET /api/settings), но из редактирования исключены
DETECTOR_DEPRECATED_FIELDS = {"ltf_range_right"}

# Группы настроек (L04): представление/доставка/анализ/эксперимент;
# всё не перечисленное — analysis
DETECTOR_FIELD_GROUPS: dict[str, str] = {
    **{n: "delivery" for n in (
        "suppress_hours", "delivery_target_seconds", "notify_only_reviewed",
        "ltf_notify_kinds", "notification_digest_seconds",
    )},
    **{n: "experimental" for n in (
        "uncalibrated_cluster_denominator",
        "uncalibrated_consolidation_max_candles",
        "uncalibrated_consolidation_overlap_pct",
        "uncalibrated_ob_delay_max_candles",
        "uncalibrated_plateau_equal_peaks",
        "uncalibrated_breakout_fvg_back_candles",
        "uncalibrated_breakout_fvg_delay_candles",
        "uncalibrated_approach_base",
        "htf_context_types",
        "ltf_provisional_range_enabled",
    )},
    "ltf_range_right": "deprecated",
}

# Числовые границы (согласованы с правилами движка: периоды положительные,
# глубины/доли в [0,1], структурные pivots — согласованные 3–5)
_NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "lookback_days": (1, 3650),
    "lookback_days_d1": (1, 3650),
    "lookback_days_w1": (1, 7300),
    "suppress_hours": (0, 8784),
    "notification_digest_seconds": (0, 86400),
    "approach_pct": (0.0001, 0.5),
    "cluster_tolerance_pct": (0.0001, 0.5),
    "depth_mid": (0.0, 1.0),
    "depth_worked": (0.0, 1.0),
    "pivot_left": (1, 20),
    "pivot_right": (1, 20),
    "delivery_target_seconds": (1, 3600),
    "uncalibrated_consolidation_max_candles": (1, 1000),
    "uncalibrated_consolidation_overlap_pct": (0.0, 1.0),
    "uncalibrated_ob_delay_max_candles": (1, 10000),
    "uncalibrated_breakout_fvg_back_candles": (0, 100),
    "uncalibrated_breakout_fvg_delay_candles": (0, 10000),
    "entry_reuse_max_depth": (0.0, 1.0),
    "inner_level_pivot_left": (1, 20),
    "inner_level_pivot_right": (1, 20),
    "ltf_structure_left": (3, 5),
    "ltf_structure_right": (3, 5),
    "ltf_poll_seconds": (30, 86400),
    "ltf_live_grace_seconds": (0, 86400),
    "ltf_history_days": (1, 365),
    "ltf_observation_stale_days": (0, 365),
    "stale_quote_seconds": (0, 86400),
    "stale_h1_intervals": (1, 100),
    "stale_d1_intervals": (1, 100),
    "stale_w1_intervals": (1, 100),
}

# CSV-поля с допустимым перечислением значений
_CSV_ENUMS: dict[str, set[str]] = {
    "scan_timeframes": {"D1", "W1"},
    "ltf_entry_types": {"FVG", "OB", "BSL", "SSL"},
    "htf_context_types": {"OB", "FVG"},
    "ltf_notify_kinds": {
        "bos_sms", "entries_ready", "touch", "sweep_outcome", "cancellation",
    },
    "ltf_range_anchor_policy": {"origin_reversal", "continuation_only"},
}


def _validate_csv(name: str, value: str, allowed: set[str]) -> Optional[str]:
    allowed_upper = {a.upper() for a in allowed}
    bad = [t for t in (s.strip() for s in value.split(","))
           if t and t.upper() not in allowed_upper]
    if bad:
        return (
            f"недопустимые значения: {', '.join(bad)}; "
            f"разрешены: {', '.join(sorted(allowed))}"
        )
    return None


def validate_detector_payload(
    payload: dict[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    """Строгая валидация патча настроек (L04).

    Возвращает (приведённые значения, ошибки по полям). Неизвестные и
    устаревшие поля отклоняются; типы приводятся строго (bool — parse_bool);
    диапазоны и перечисления проверяются по схеме. При любой ошибке
    применять НЕЛЬЗЯ ничего (атомарность обеспечивает вызывающий).
    """
    known = {f.name: type(f.default) for f in fields(DetectorConfig)}
    values: dict[str, object] = {}
    errors: dict[str, str] = {}
    for key, raw in payload.items():
        if key in DETECTOR_DEPRECATED_FIELDS:
            errors[key] = "поле устарело и не редактируется"
            continue
        if key not in known:
            errors[key] = "неизвестное поле"
            continue
        typ = known[key]
        try:
            if typ is bool:
                value: object = parse_bool(raw)
            elif typ is int:
                if isinstance(raw, bool):
                    raise ValueError("bool вместо int")
                # A12: дробный float нельзя молча усечь (3.9 → 3) — отклоняем;
                # целый 3.0 допустим
                if isinstance(raw, float) and not raw.is_integer():
                    raise ValueError("дробное число вместо int")
                value = int(raw)  # type: ignore[arg-type]
            elif typ is float:
                if isinstance(raw, bool):
                    raise ValueError("bool вместо float")
                value = float(raw)  # type: ignore[arg-type]
            else:
                value = str(raw)
        except (ValueError, TypeError):
            errors[key] = f"ожидается {typ.__name__}, получено {raw!r}"
            continue
        if key in _NUMERIC_RANGES:
            lo, hi = _NUMERIC_RANGES[key]
            if not (lo <= value <= hi):  # type: ignore[operator]
                errors[key] = f"допустимо от {lo} до {hi}"
                continue
        if key in _CSV_ENUMS:
            msg = _validate_csv(key, str(value), _CSV_ENUMS[key])
            if msg is not None:
                errors[key] = msg
                continue
        values[key] = value
    return values, errors


def validate_detector_config(cfg: DetectorConfig) -> dict[str, str]:
    """Межполевые зависимости порогов (L04) — проверка итогового конфига."""
    errors: dict[str, str] = {}
    if not (0.0 < cfg.depth_mid < cfg.depth_worked):
        errors["depth_mid"] = (
            "требуется 0 < depth_mid < depth_worked "
            f"(сейчас {cfg.depth_mid} и {cfg.depth_worked})"
        )
    return errors


@dataclass
class AltConfig:
    """Параметры модуля «Altcoins D1 accumulation» (изолирован, таблицы alt_*).

    Пороги торговой логики и классификатора — рабочие значения по умолчанию
    (проектные, не калиброваны). Всё переопределяется через ENV вида
    HTF_ALT_<FIELD>.
    """

    # Торговые пороги
    drawdown_threshold: float = 0.80     # просадка от хая для входа в поиск
    forming_min_days: int = 50           # СТРОГО больше → forming с 51 дня
    mature_min_days: int = 100           # СТРОГО больше → mature со 101 дня
    retest_window_days: int = 14         # дедлайн ретеста после пробоя
    target_count: int = 4                # число целей TP1..TP4
    pivot_left: int = 3                  # структурные pivots 3+3
    pivot_right: int = 3

    # Классификатор диапазона — ДЕРЖИМ ОТДЕЛЬНО от торговых порогов:
    # это проектные пороги отбора формы, не параметры входа/выхода
    classifier_block_days: int = 10      # размер блока для оценки сдвига центра
    classifier_slope_max: float = 0.50   # макс. наклон (доля ширины за блок)
    classifier_center_shift_max: float = 0.50  # макс. сдвиг центра (доля ширины)
    classifier_version: str = "v1"
    main_time_fraction: float = 0.80     # только диагностика: доля времени в теле

    # Режим отмены — ПРОЕКТНЫЙ ВЫБОР (не согласован как калибровка):
    # wick_on_closed_d1 — фитиль закрытой D1-свечи за K отменяет сетап;
    # close_on_closed_d1 — только закрытие за K
    cancel_mode: str = "wick_on_closed_d1"   # wick_on_closed_d1 | close_on_closed_d1

    # Движок диапазонов v2 (ТЗ 07.10.2026, R-01..R-04; app/alt/engine_v2.py).
    # Значения проектные, калибруются на эталонах (этап 6); все переопределяются
    # через ENV HTF_ALT_<FIELD>.
    engine_version: str = "v1"               # v1 | v2 — выбор движка раннером (этап 7)
    v2_max_candidates: int = 8               # лимит живых кандидатов на актив (R-02)
    v2_max_alternatives: int = 3             # альтернативных опор на зону кандидата (R-02)
    v2_admission_window_days: int = 1095     # срок допуска после глубокой просадки (R-01)
    v2_candidate_split_days: int = 60        # разрыв без реакций → новый кандидат (R-02)
    v2_atr_period: int = 14                  # период ATR по закрытым D1 (R-03/R-04)
    v2_cluster_atr_mult: float = 0.5         # допуск кластера: множитель ATR (R-03)
    v2_cluster_pct: float = 0.02             # допуск кластера: доля медианной цены (R-03)
    v2_min_reactions: int = 2                # мин. независимых реакций у границы (R-03)
    v2_min_reaction_gap_days: int = 10       # разрыв между независимыми реакциями (R-03)
    v2_max_width_atr_mult: float = 20.0      # диагностический флаг широкой рамки (R-04)
    v2_max_days_since_reaction: int = 120    # диагностический флаг давности реакции (R-04)
    # Жизненный цикл v2 post-freeze (R-05/R-06)
    v2_sweep_max_days: int = 75              # принятие ниже L без возврата → распад (R-05).
                                             # Калибровка этапа 6: вынос, который закрывается
                                             # обратно в базу на ~40-й день, ещё возврат,
                                             # а не распад. 30 дней обрывали базу до возврата.
                                             # Это глобальный порог, не константа тикера.
    v2_breakout_tol: float = 0.0             # выход: Close > U·(1+tol), строго как v1 (R-06)
    v2_freeze_stable_days: int = 60          # freeze только при неизменной паре L/U столько
                                             # дней: поздние реакции длинных баз (AAVE/
                                             # HBAR 2022-2024) не обрезаются freeze на 101
                                             # день (§5: подтверждённый пропуск нужных баз)

    # Расписание джобы (время МСК)
    job_hour_msk: int = 3
    job_minute_msk: int = 15
    job_enabled: bool = True
    poll_interval_seconds: int = 60      # как часто цикл проверяет «пора ли запускать»

    # Вселенная альткоинов (CoinMarketCap)
    cmc_rank_min: int = 11
    cmc_rank_max: int = 300
    cmc_listing_limit: int = 300

    # Источники свечей D1 (§4): предпочтение площадки и валюты котировки
    # при выборе единственной пары актива (csv, порядок = приоритет)
    venue_preference: str = "binance,bybit"
    quote_preference: str = "USDT,USDC"
    # Блокировка дневного job: "running" старше N часов — interrupted (§4)
    run_lock_stale_hours: int = 6


@dataclass
class Settings:
    """Настройки процесса. Секреты — только ENV, в браузер не попадают (§11 п.8)."""

    db_path: str = field(
        default_factory=lambda: resolve_db_path(_env("HTF_DB_PATH", "data/htf_zones.db"))
    )
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", ""))
    auth_token: str = field(default_factory=lambda: _env("HTF_AUTH_TOKEN", "dev-token"))
    binance_base_url: str = field(
        default_factory=lambda: _env("BINANCE_BASE_URL", "https://data-api.binance.vision")
    )
    hyperliquid_base_url: str = field(
        default_factory=lambda: _env("HYPERLIQUID_BASE_URL", "https://api.hyperliquid.xyz")
    )
    poll_seconds: int = int(_env("HTF_POLL_SECONDS", "1800"))  # 30 мин — достаточно для D1/W1
    # F02/A02: отдельный быстрый цикл котировок — короткий заход цены в зону
    # между HTF-опросами не теряется; 0 — цикл выключен, котировку забирает
    # HTF-цикл (прежнее поведение)
    quote_poll_seconds: int = int(_env("HTF_QUOTE_POLL_SECONDS", "10"))
    host: str = field(default_factory=lambda: _env("HTF_HOST", "127.0.0.1"))
    port: int = int(_env("HTF_PORT", "8000"))
    # Публичный URL сайта для ссылок из Telegram (кнопка «Открыть график»);
    # нужен, когда сервис за прокси или HTF_HOST=0.0.0.0
    public_base_url: str = field(default_factory=lambda: _env("HTF_PUBLIC_BASE_URL", ""))
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    # Модуль «Altcoins D1 accumulation»: ключи/базовые URL внешних API
    cmc_api_key: str = field(default_factory=lambda: _env("CMC_API_KEY", ""))
    bybit_base_url: str = field(
        default_factory=lambda: _env("BYBIT_BASE_URL", "https://api.bybit.com")
    )
    cmc_base_url: str = field(
        default_factory=lambda: _env("CMC_BASE_URL", "https://pro-api.coinmarketcap.com")
    )
    alt_config: AltConfig = field(default_factory=AltConfig)

    def effective_base_url(self) -> str:
        """URL для внешних ссылок. Без HTF_PUBLIC_BASE_URL — локальный;
        0.0.0.0 в ссылке бессмысленен, подставляем 127.0.0.1."""
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        host = self.host if self.host not in ("", "0.0.0.0", "::") else "127.0.0.1"
        return f"http://{host}:{self.port}"


def load_settings() -> Settings:
    return Settings()


def load_detector_config() -> DetectorConfig:
    """DetectorConfig с возможностью переопределения через ENV вида HTF_DET_<FIELD>."""
    cfg = DetectorConfig()
    for f in fields(cfg):
        raw = os.environ.get(f"HTF_DET_{f.name.upper()}")
        if raw is None:
            continue
        try:
            if isinstance(f.default, bool):
                setattr(cfg, f.name, parse_bool(raw))
            else:
                setattr(cfg, f.name, type(f.default)(raw))
        except (ValueError, TypeError) as exc:
            # A05: некорректный ENV не роняет старт, но и не прячется —
            # предупреждение с полем, значением и причиной
            logger.warning(
                "HTF_DET_%s: значение %r отклонено (%s), используется %r",
                f.name.upper(), raw, exc, getattr(cfg, f.name),
            )
    return cfg


def load_alt_config() -> AltConfig:
    """AltConfig с возможностью переопределения через ENV вида HTF_ALT_<FIELD>."""
    cfg = AltConfig()
    for f in fields(cfg):
        raw = os.environ.get(f"HTF_ALT_{f.name.upper()}")
        if raw is None:
            continue
        try:
            if isinstance(f.default, bool):
                setattr(cfg, f.name, parse_bool(raw))
            else:
                setattr(cfg, f.name, type(f.default)(raw))
        except (ValueError, TypeError) as exc:
            # как A05 у детектора: некорректный ENV не роняет старт
            logger.warning(
                "HTF_ALT_%s: значение %r отклонено (%s), используется %r",
                f.name.upper(), raw, exc, getattr(cfg, f.name),
            )
    return cfg
