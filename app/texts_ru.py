"""Единый источник русских формулировок (§9).

Telegram-сообщения, подписи на снимках зон (chartimg) и веб-интерфейс
(endpoint /api/labels) берут словари отсюда, чтобы тексты не расходились.
"""
from __future__ import annotations

from .models import EventKind

# Вид события → причина в сообщении/интерфейсе
KIND_RU: dict[EventKind, str] = {
    EventKind.APPROACH: "приближение к зоне (2%)",
    EventKind.TOUCH: "первое касание",
    EventKind.DEPTH_50: "достигнуто 50% глубины",
    EventKind.DEPTH_90: "достигнуто 90% — объект отработан",
    EventKind.D1_CLOSE_INSIDE: "закрытие дневной свечи внутри зоны",
    EventKind.JUMP_THROUGH: "проход зоны скачком насквозь",
    EventKind.ALREADY_IN_ZONE: "цена уже в зоне",
    EventKind.FVG_WEAKENED: "FVG ослаблен (50%)",
    EventKind.FVG_FILLED: "FVG полностью заполнен",
    EventKind.OB_CONFIRMED: "Orderblock подтверждён",
    EventKind.BREAKER_CREATED: "Orderblock превратился в Breaker",
    EventKind.BREAKER_ARCHIVED: "Breaker пробит и архивирован",
    EventKind.PRB_ARCHIVED: "PRB пробит и архивирован",
    EventKind.LEVEL_TAKEN: "уровень пересечён (снят)",
    EventKind.ZONE_CONFIRMED_BY_USER: "зона подтверждена пользователем",
    EventKind.DATA_STALE: "данные устарели",
    EventKind.DATA_RECOVERED: "данные восстановлены",
}

# Тип зоны → название
TYPE_RU = {
    "fvg": "FVG",
    "ob": "Orderblock",
    "prb": "PRB",
    "breaker": "Breaker",
    "ssl": "SSL",
    "bsl": "BSL",
    "manual": "Ручная зона",
}

DIRECTION_RU = {"bull": "бычий", "bear": "медвежий"}

# Статус зоны → формулировка
STATUS_RU = {
    "candidate": "кандидат (ожидает проверки)",
    "active": "активна",
    "weakened": "ослаблена",
    "worked": "отработана",
    "converted": "превращена в Breaker",
    "archived": "архивирована",
    "taken": "снята",
    "rejected": "отклонена",
}

# ---------------------------------------------------------------------------
# LTF Confirmations (LTF-спека §3, §11): тот же источник для Telegram и веба
# ---------------------------------------------------------------------------

# Тип Entry Zone H1 → название
LTF_TYPE_RU = {
    "FVG": "FVG",
    "OB": "Orderblock",
    "BSL": "BSL",
    "SSL": "SSL",
}

# Состояние наблюдения (§12)
LTF_OBSERVATION_STATE_RU = {
    "waiting_structure": "ожидание слома структуры",
    "active": "активно",
    "paused_data": "пауза: проблема данных",
    "closed_by_parent": "закрыто: HTF-зона инвалидирована",
    "closed_by_user": "закрыто вручную",
    "closed_stale": "закрыто: неактивно",
}

# Состояние сценария (§12)
LTF_SCENARIO_STATE_RU = {
    "range_pending": "ожидание диапазона",
    "monitoring_entries": "отслеживание Entry Zones",
    "cancelled": "отменён",
    "closed": "завершён вручную",
}

# Причина отмены сценария (§11.4)
LTF_CANCELLATION_RU = {
    "reverse_bos": "обратный BOS H1",
    "reverse_sms": "обратный SMS H1",
    "HTF_INVALIDATED": "инвалидация HTF",
    "manual": "ручное завершение",
    "stale": "архивация по неактивности",
}

# Вид события окна LTF → название (журнал/интерфейс)
LTF_EVENT_KIND_RU = {
    "bos": "слом структуры BOS",
    "sms": "слом структуры SMS",
    "range_ready": "диапазон Premium/Discount готов",
    "entries_ready": "новые Entry Zones",
    "touch": "касание Entry Zone",
    "sweep_confirmed": "снятие уровня подтверждено",
    "sweep_failed": "снятие уровня не подтверждено",
    "cancellation": "отмена сценария",
    "context_update": "контекст сценария: снятие SSL/BSL + тест 50% D1 FVG (§18)",
    "note": "заметка",
}

# Решения ревью Entry Zone → подпись кнопки/строки истории
LTF_REVIEW_DECISION_RU = {
    "correct": "размечено верно",
    "now_irrelevant": "сейчас неактуально",
    "fix_boundaries": "исправить границы",
    "wrong_base": "другое основание",
    "wrong_type": "неверный тип/форма",
    "no_context": "нет контекста",
    "wrong": "отклонена",
    # legacy-коды из истории
    "confirmed": "размечено верно",
    "rejected": "отклонена",
    "corrected": "исправить границы",
}

# LTF-специфичные коды причины (reason_code) → подпись в select
LTF_REVIEW_REASON_RU = {
    "wrong_movement": "не то движение (movement)",
    "wrong_eligible": "не должна была попасть в сценарий",
    "wrong_level": "не тот экстремум BSL/SSL",
    "wrong_fvg_base": "не та свеча-основание FVG",
    "wrong_ob_base": "не та свеча-основание OB",
    "late_zone": "зона появилась слишком поздно",
    "duplicate_zone": "дублирует существующую зону",
    "no_context": "не могу оценить",
}
