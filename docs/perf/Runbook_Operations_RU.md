# Runbook эксплуатации — LevelFrame (perf-пайплайн, этапы 1–7)

Практический справочник оператора: диагностика лагов, очередей, 429,
восстановление, откат и деплой. Спека:
`docs/Service_Performance_Optimization_Spec_RU_2026_10_12.md` (§13, §20, §21).
Отчёт об измерениях: `docs/perf/Performance_Report_2026-10-12.md`.

## 1. Эндпоинты наблюдаемости

Все диагностические GET требуют авторизации (`require_auth`); секреты и URL
источников в ответах отсутствуют.

| Эндпоинт | Назначение |
|---|---|
| `GET /api/live` | liveness: процесс отвечает, БД читается (`SELECT 1`) |
| `GET /api/health` | прежняя liveness-проверка со свежестью данных |
| `GET /api/ready` | readiness-фазы `web → realtime → history → calc` + прогресс backfill; `ready=true` начиная с фазы realtime (котировки текут); всегда 200 и дешёвый — healthcheck оркестратора сюда |
| `GET /api/diagnostics` | защищённый снимок runtime: роль, владельцы, очереди, курсоры, фазы |
| `GET /api/diagnostics/metrics` | снимок реестра метрик (counters/gauges/histograms с p50/p95/p99) |
| `POST /api/diagnostics/quotes/{row_id}/retry` | ручной повтор строки spool котировок (карантин/сбой) |

## 2. Как читать `/api/diagnostics`

Собирается `build_diagnostics` (`app/services/runtime.py`). Секции:

- `process_role`, `pid`, `instance_id`, `build_id`, `started_at` — кто отвечает;
  `schema_version`, `storage_contract_version`, `profile_fingerprint`,
  `config_version` — что за код/конфигурация обслуживает процесс.
- `calc_owner` / `calc_conflict`, `telegram_owner` / `telegram_conflict` —
  лизы владения (meta `runtime:calc_owner`, `runtime:telegram_owner`);
  conflict-объект = два процесса спорили за роль (норма при деплое,
  тревога — если не рассасывается за время lease, 120 с).
- `calc_heartbeat`, `last_error` — жизнь calc-роли и последнее исключение.
- `readiness` — фазы `web/realtime/history/calc` с метками времени
  (`Readiness`, in-memory; у web-only роли может быть `null`).
- `backfill` — прогресс догрузки истории по чекпоинтам
  `backfill:checkpoint:{iid}:{tf}`: `total/completed` + на канал
  `status` = pending | running | completed, `done_through_ms`, `updated_at`.
  Канал «running» долго без движения `done_through_ms` — застрявшая догрузка
  (смотреть `instruments[].last_error`, логи адаптера).
- `market_queue` (очередь котировок, этап 2, §8):
  - `degraded` — флаги деградации из meta `quote:degraded:{iid}`
    (`{since_ms, reason}`; reason: `overflow` | `calculation_failed`);
  - `spool` — `{instrument_id: число pending-строк quote_spool}` — durable
    backlog, виден и из web-роли;
  - `queues` (только calc-процесс) — `capacity` (500), `durable: true`,
    по инструменту `depth/spooled/degraded/consumer_running/idle`.
- `calc_queue` (очередь расчётов, этап 4, §9):
  - `persisted` — счётчики строк `calc_task` по статусам
    (pending/running/failed) — видны из любой роли;
  - `last_failed` — последнее упавшее задание
    `{instrument_id, timeframe, task_kind, error, updated_at}`;
  - `queue` (только calc-процесс) — in-memory глубины: `pending` по
    task_kind, `running`, `pending_oldest_wait_s` по каналу, счётчики
    `calc_queue_*` и `calc_skipped_unchanged`.
- `pipeline` (§13, этап 6a) — курсоры по каналам:
  - `channels[]`: `received` (close_time последней свечи, включая
    формирующую), `processed` (meta `data_sync:processed:{iid}:{tf}`),
    `calc_pending_wait_s` (ожидание старейшего pending-задания канала);
  - `quotes[]`: `quote_at`, `quote_age_ms` на инструмент;
  - `transport` (при включённом realtime): `connected/total/reconnects`.
- `realtime` (при `HTF_REALTIME_ENABLED=1`, `BinanceRealtime.status()`):
  по символу `connected`, `streams`, `last_message_at`,
  `last_valid_message_at`, `reconnects`, `last_error`, `healthy`.
- `instruments[]` — `last_h1`, курсоры `structure_last_processed_h1` /
  `scenario_last_processed_h1` / `cursor`, `replaying`, `last_error`,
  `parents_by_reason`.

## 3. Как читать `/api/diagnostics/metrics`

Снимок реестра (`app/services/metrics.py`): `series` — словарь
`имя{label=...}` → counter (`value`), gauge (`value`, `at`) или histogram
(`count`, `sum`, `p50/p95/p99/max_ms`). Ключевые ряды:

- транспорт: `rest_request{venue,endpoint,outcome}` (ок/error/timeout/retry),
  `rest_retry_after{venue}`, `rest_retry_cooldown_s{venue}`,
  `rest_weight_used{venue}` (Binance X-MBX-USED-WEIGHT-1M; warn > 4800),
  `ws_connected`, `ws_last_message_age_s`, `ws_connections`, `ws_reconnects`,
  `ws_messages`, `ws_malformed`, `clock_skew{venue}`;
- котировки: `quote_ticks{venue,source}`, `quote_source_age_s{venue}`
  (возраст рыночных данных на момент записи: event_at → received_at),
  `quote_receive_to_persist`, `quote_receive_to_broadcast`;
- очереди: `market_queue_enqueued/spooled/drained/duplicates/errors{venue}`,
  `market_queue_depth`, `market_queue_wait{venue}`;
- расчёт: `calc_lock_wait{task}`, `calc_lock_held`, `calc_duration{task}`,
  `calc_skipped_unchanged{timeframe}`;
- свечи: `candles_received/inserted/revised/unchanged{timeframe}`,
  `sync_candles_duration{timeframe,reason}`;
- SQLite: `sql_duration{op=execute,kind=read|write}`,
  `sql_busy_errors{op=execute}`, `db_wal_bytes`.
  Оговорка: `sql_duration{op=commit}` после storage-переписи покрывает только
  autocommit-путь; коммиты единиц `batch_writes` метрикой не считаются
  (см. отчёт этапа 7).

## 4. Диагностика типовых проблем

### 4.1 Транспортный лаг (данные не текут от источника)

1. `GET /api/diagnostics` → `pipeline.quotes[].quote_age_ms` растёт;
   `pipeline.channels[].received` отстаёт от календаря.
2. Realtime включён: смотреть секцию `realtime` — `connected=false`,
   `healthy=false`, рост `reconnects`, `last_error`; метрики
   `ws_connected=0`, `ws_last_message_age_s` растёт, `ws_malformed`,
   `clock_skew` (расхождение часов с источником — проверить NTP хоста).
   При нездоровой WS REST-фолбэк продолжается автоматически (per-instrument);
   если и REST молчит — шаг 3.
3. REST: `rest_request{outcome=timeout|error|retry}` растёт по venue;
   `rest_retry_cooldown_s > 0` — действует Retry-After (см. §4.4);
   `quote_source_age_s` большой — источник отдаёт старые данные.
4. Поканально: `/api/data-sync/status` → `channels[].status/reason`
   (`source_error`, `sync_stale`, `missing_close`, `missing_current`,
   `history_gap`, `processing_lag`) и `last_attempt_at/last_success_at`.

### 4.2 Лаг обработки (данные есть, расчёт отстаёт)

1. `pipeline.channels[]`: `received` свежий, `processed` отстаёт,
   `calc_pending_wait_s` растёт.
2. `calc_queue.persisted.pending/running` растут; `queue.running` — что
   исполняется; `last_failed` — последняя ошибка задания.
3. Метрики: `calc_duration{task}` (долгие расчёты), `calc_lock_wait`
   (очередь за глобальным calc-замком, AC19), `market_queue_wait`
   (ожидание тиков до расчёта), `sql_busy_errors` (конкуренция за писателя).
4. `instruments[].replaying=true` — идёт replay канала (расчёт занят
   законно); `readiness.phase` — на старте лаг нормален до фазы `calc`.

### 4.3 Переполнение / degraded очереди котировок

1. `market_queue.spool{iid}` > capacity (500) → флаг
   `market_queue.degraded[iid]` (`quote:degraded:{iid}` в meta, виден в UI
   и после рестарта, AC11); счётчик `market_queue_spooled` растёт.
2. Тики не теряются: durable `quote_spool`; потребитель дочитывает пачками
   по 50 (`spool_batch`) со строгим порядком. Сбойная строка —
   `status=retry` с экспоненциальной паузой (`next_attempt_at`,
   `min(60, 2**attempts)` с); до срока повтора канал стоит (строгая
   очередность рыночных наблюдений не нарушается), в meta пишется
   `quote:degraded:{iid}` с `reason=calculation_failed`. После 5 попыток
   строка уходит в `quarantine` и останавливает канал до ручного
   `POST /api/diagnostics/quotes/{row_id}/retry` (сбрасывает в pending).
3. Действия: устранить причину (lag расчёта → §4.2; шторм источника → §4.4),
   дождаться дочитки (`spool` → 0, `idle=true`, флаг degraded снимается сам).

### 4.4 HTTP 429 / Retry-After

- Весь REST-путь идёт через `DataSync.request`: per-venue семафор
  (`HTF_REST_CONCURRENCY`, дефолт 2) + общий на venue Retry-After
  (`AdapterError.retry_after` → пауза `_retry_until`, AC09 — обойти через
  другую очередь нельзя). Повтор внутри запроса — максимум 1 раз.
- Диагностика: `rest_retry_after{venue}` (сколько раз получен 429),
  `rest_retry_cooldown_s` (текущая пауза), `rest_request{outcome=retry}`.
- Длительный cooldown: снизить частоту опроса (`HTF_POLL_SECONDS`,
  `HTF_QUOTE_POLL_SECONDS`) или `HTF_REST_CONCURRENCY=1`; Binance вес —
  `rest_weight_used`.

## 5. Восстановление

- **WS reconnect catch-up (AC10/AC12):** при переподключении подписки
  `_ws_on_reconnect` запускает `sync_candles(reason="ws_recovery")` по каждому
  подписанному ТФ — догружает разрыв; `insert_candles` идемпотентен, дублей
  нет. Плановая ротация соединения — `HTF_WS_LIFETIME_S` (дефолт 82800 ≈ 23 ч,
  Binance рвёт через 24 ч); backoff — `HTF_WS_RECONNECT_BASE_S/CAP_S` (1/60 с,
  экспонента ×2, джиттер ±20%); молчание — `HTF_WS_IDLE_TIMEOUT` (120 с).
- **Spool drain:** автоматически потребителем market_queue; после рестарта
  `MarketQueue.start()` поднимает каналы с pending-строками из
  `quote_spool`/неподтверждёнными `market_publication`. Карантинные строки —
  только вручную через retry-эндпоинт (см. §4.3).
- **calc_task retry при рестарте (AC18):** `CalcQueue.start()` возвращает
  строки `calc_task` pending/running/failed в обработку (running при крахе
  неотличим от pending; failed перезапускается на рестарте, в горячем цикле
  не крутится). Сбой транзакции не сдвигает processing cursor — хвост
  дообрабатывается по курсору.
- **Прерванный data_sync job / backfill:** job помечается
  `interrupted_by_restart`; backfill продолжается от чекпоинтов
  `backfill:checkpoint:{iid}:{tf}` (перекрытие перечитывается и сверяется).

## 6. Откат (§20)

Порядок — отключение флагами, без удаления данных:

1. `HTF_REALTIME_ENABLED=false` — WS-поток выключается, котировки и свечи
   возвращаются на REST-опрос (поведение идентично прежнему, AC21);
   миграции цен между venue не происходит.
2. `HTF_CALC_QUEUE_ENABLED=false` — durable-очередь расчётов выключается,
   `sync_candles` считает синхронно по прежнему пути; строки `calc_task`
   остаются в БД и безвредны (аддитивная схема); при повторном включении
   подхватываются `CalcQueue.start()`.
3. Выключение флагов останавливает новых писателей; REST-путь и
   tail-синхронизация (курсоры `data_sync:processed:{iid}:{tf}`,
   `ltf:h1:last_close:{iid}`) продолжают работу. Сверка хвоста — прогон
   ручного job `POST /api/data-sync` и контроль `pipeline.channels[]`
   (received vs processed) в `/api/diagnostics`.
4. **Никогда не удалять рыночные данные** (candle/zone/event/quote_spool)
   ради возврата старого кода. Откат кода — флаги + рестарт; данные
   совместимы (миграции добавочные).

## 7. Деплой

1. Флаги с консервативными дефолтами: новое поведение выключено или
   совместимо (realtime по умолчанию выключен; calc_queue включён — основной
   путь этапа 4, откат флагом выше).
2. Миграции схемы добавочные (`CREATE ... IF NOT EXISTS`, ALTER ADD COLUMN в
   `Database.migrate`) и совместимы с отключёнными флагами.
3. **Перед изменением схемы — проверяемый backup** через SQLite backup API
   (не файловое копирование при WAL): конвенция
   `app/services/taken_levels_migration.py:_backup` —
   `sqlite3.connect("file:...?mode=ro", uri=True)` → `src.backup(dst)`.
   Штатные бэкапы сервиса: `HTF_BACKUP_ENABLED=1`,
   `HTF_BACKUP_INTERVAL_HOURS=24`, каталог `data/backups/`.
4. Сначала тестовая среда и малый список активов, затем расширение;
   независимые писатели одного инструмента одновременно не включаются (§20).
5. После деплоя: `/api/ready` → фазы до `calc`; `/api/diagnostics` →
   `calc_queue.persisted` без растущего failed, `market_queue.spool` пуст,
   `pipeline.channels[]` received≈processed.
6. **Push в GitHub ≠ production deploy.** Деплоем считается только
   обновление работающего процесса (платформа/оркестратор) с проверками
   п.5. Railway-конфиг: `railway.toml`.

## 8. Переменные окружения (perf-этапы)

| Переменная | Дефолт | Назначение |
|---|---|---|
| `HTF_REALTIME_ENABLED` | `0` | realtime-поток Binance WS (этап 3); 0 — прежний REST-режим |
| `HTF_BINANCE_WS_URL` | `wss://stream.binance.com:9443` | endpoint WS (только схема wss://) |
| `HTF_WS_IDLE_TIMEOUT` | `120` | секунд без сообщений до переподключения |
| `HTF_WS_LIFETIME_S` | `82800` | плановая ротация соединения (~23 ч) |
| `HTF_WS_RECONNECT_BASE_S` | `1` | backoff переподключения: база |
| `HTF_WS_RECONNECT_CAP_S` | `60` | backoff: потолок |
| `HTF_CALC_QUEUE_ENABLED` | `1` | durable-очередь расчётов (этап 4); 0 — синхронный расчёт (откат) |
| `HTF_CALC_CONCURRENCY` | `1` | потребители очереди расчётов (расчёт всё равно сериализует calc-замок) |
| `HTF_REST_CONCURRENCY` | `2` | параллельные REST-запросы на venue (этап 5) |
| `HTF_QUOTE_POLL_SECONDS` | `10` | быстрый цикл котировок (F02); 0 — цикл выключен, цену забирает HTF-цикл |
| `HTF_POLL_SECONDS` | `1800` | базовый HTF-цикл опроса |
| `HTF_DB_PATH` | `data/htf_zones.db` | путь к SQLite |
| `HTF_BACKUP_ENABLED` / `HTF_BACKUP_INTERVAL_HOURS` | `1` / `24` | штатные бэкапы |
| `HTF_HOST` / `HTF_PORT` / `HTF_PUBLIC_BASE_URL` | `127.0.0.1` / `8000` / `` | сеть web; публичный URL для ссылок из Telegram |
| `HTF_AUTH_TOKEN` | `dev-token` | токен API/WS (сменить в production!) |
| `HTF_DET_<FIELD>` | — | любое поле DetectorConfig, напр. `HTF_DET_SCAN_TIMEFRAMES` (список ТФ сканера), `HTF_DET_LOOKBACK_DAYS_*`, `HTF_DET_LTF_POLL_SECONDS` |

Ёмкости очередей — кодовые константы (`MarketQueue`: capacity 500,
dedup 256, spool_batch 50), не ENV; изменение — через код и тесты.
Изменения флагов видны в диагностике (`profile_fingerprint`,
`config_version`, секции `realtime`/`calc_queue`) и не смешивают активные
поколения заданий (§16).
