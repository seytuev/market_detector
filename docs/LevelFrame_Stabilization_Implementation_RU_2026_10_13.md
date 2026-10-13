# Реализация стабилизации LevelFrame

Дата: 13 октября 2026 года. Основание: [ТЗ с 54 критериями](LevelFrame_Stabilization_TZ_RU_2026_10_12.md). База рабочей копии: `9959eef5396229e71fdb9453f7eac0fd43ed9742`.

Статус: основная реализация подготовлена локально, приёмка частичная. Всё ТЗ завершённым не считается. Полный native pytest, HTTP и браузерный E2E, контейнерная проверка, потоковая нагрузка и 24-часовой soak ещё необходимы. Публикация не выполнена: инструмент GitHub отклонил создание ветки, поскольку операция требует разрешения, а политика среды — `never`.

## Реализованные изменения

| Блок | Реализация | Проверка и предел доказательства |
|---|---|---|
| TZ01 | Безопасное чтение PID на Windows; атомарные leases с instance/generation; heartbeat, release и fencing calc/settings | Конкурентные claims, старое поколение и тестовый дочерний процесс проверены; многоконтейнерный запуск не проверен |
| TZ02 | Соединения и кэши SQLite принадлежат потокам; savepoints, единицы записи, committed read snapshots; sync API-мутации атомарны; WS публикация после commit | SQL rollback, чужая запись, вложенный rollback и post-commit callbacks проверены; сквозной review/API/outbox ещё требует native suite |
| TZ03 | Durable receipt до публикации цены; retry/quarantine; обработка и publication journal в одном commit; ограниченные партии; outbox доставка отдельным циклом | Ошибка тика и восстановление публикации проверены; kill-process и нагрузочный сценарий не приняты |
| TZ04 | Позднее чтение ENV, общая validation, config version/CAS, leases задания, независимый от HTTP timeout результат, DB authoritative profile | Timeout и rollback проверены с отдельным движком тестового задания; реальный HTTP job и большой LTF resync не проверены |
| TZ05 | Supervisor для HTTP/worker/outbox/Telegram/maintenance; stop-сигналы; ожидание WS tasks, jobs и расчётов перед закрытием БД | Синтаксис и отдельные lifecycle-компоненты проверены; длительность shutdown и реальная Telegram-сессия не приняты |
| TZ06 | Сверка подписок и ТФ каждую секунду; disabled callbacks; pending/retry отменяются явным receipt; публикация изменения инструмента после commit | Отмена receipt проверена; race disable/closed H1 и несколько клиентов требуют E2E |
| TZ07 | Общая валидация OHLC/quotes; строгий WS closed; ограничения venue, market_type, precision и ручных цен | Невалидная свеча не попадает в БД; источниковые и API отрицательные сценарии полностью не прогнаны |
| TZ08 | HttpOnly-сессии, срок жизни и отзыв; Bearer для API; постоянный токен удалён из ссылок и WS URL; chart package локально собирается с проверкой архива | Browser/session HTTP тесты и сборка vendor не выполнены в этой среде |
| TZ09 | Live/ready разделены; lease/heartbeat и подготовка учитываются; исправлены loop lag, худший возраст WS и peak/current RSS; ограничена cardinality | Registry и SQL metrics проверены; live health и внешние источники не приняты |
| TZ10 | Ограниченные WS очереди, latest-price coalescing, timeout медленного клиента, resync; SQL LIMIT для хвоста свечей; event cursor API | Индекс и чтение хвоста проверены на 100 тыс./1 млн строк; WS нагрузка не проверена |
| TZ11 | Constraints, Python 3.11/3.12, package resources, non-root Docker, CI, backup/verify/retention и daily maintenance | Backup/restore временной БД и защита pending проверены; wheel, Docker, production RPO/RTO и ротация backup-каталогов не приняты |
| TZ12 | Runtime registry, полный fingerprint профиля и source revision; индекс контрактов; SQL/store и settings job выделены из больших модулей | ALT fixtures прошли; полное разбиение монолитов отложено до полного регрессионного прогона |
| TZ13 | Исправлены сообщения ошибок, авторизация/reconnect, job polling, logout; подготовлены screenshots/E2E сценарии трёх размеров | Каталог и presenter проверены; реальные screenshots отсутствуют |

## Важные изменения контрактов

Версия приложения — 0.2.0, SQLite schema — 2. `quote_spool` теперь хранит каждый принятый тик и квитанции завершения, а не только переполнение памяти. Старые ожидания тестов вида «два тика исключительно в памяти и три в БД» больше не соответствуют контракту сохранности. Необходима отдельная native-проверка обновлённых потоковых тестов.

Постоянный токен в query больше не авторизует HTTP/WS. Браузер обменивает введённый токен на сессию; серверные клиенты используют Bearer. Тесты старых URL-токенов и реальные Telegram deep links необходимо прогнать по новому контракту.

Профиль в SQLite является источником истины; settings.json — его экспорт. Ошибка пересчёта откатывает транзакцию состояния. Исторические аудиты и их старые воспроизведения сохранены как материалы предыдущего состояния; текущие гарантии проверяются `test_stabilization_*`.

Некорректные OHLC в синтетических тестах исправлены: нулевой low, open выше high, close ниже low и close выше high. Смысл BOS/отмены и теста касания сохранён; соответствующие сценарии прошли повторно. Валидация не ослаблялась ради тестов.

## Полученные результаты

- Расширенный поднабор: **268 passed** на Windows, Python 3.12.8, memory-only asyncio test loop.
- После последующих правок: **43 passed** для каталога/presenter, метрик и целевых storage/pipeline/settings сценариев.
- Windows PID probe оставил тестовый дочерний процесс живым до его штатного выхода с кодом 0.
- SQL хвост 300 свечей: p95 **1.18 мс** при 1 млн строк; 30 измерений, OS cache тёплый, SQLite cache освобождался. Это показатель repository query, а не API или production latency.
- Python AST и JavaScript syntax пройдены; `git diff --check` не выявил whitespace errors.

Отчёты: [pytest поднабора](audit/stabilization-2026-10-13/pytest-subset.txt), [целевые проверки](audit/stabilization-2026-10-13/pytest-targeted.txt), [benchmark](audit/stabilization-2026-10-13/storage-tail-benchmark.json).

Проверки Python 3.12 использовали только совместимые pure-Python компоненты локальных зависимостей. Бинарный pydantic_core существующей `.venv` собран для 3.11 и мешает native API collection; Windows Store interpreter этой `.venv` не запускается. Memory-only loop не реализует сеть и не заменяет стандартный loop для интеграции.

## Открытая приёмка

Нельзя объявлять все AC01–AC54 passed на основании поднабора. В частности, остаются:

1. Полный pytest на исправном Python 3.11/3.12 со стандартным event loop, включая старые quote/WS/settings/auth expectations.
2. Fault injection с настоящим завершением процесса в трёх точках intake/calculation/publication.
3. Одновременные review, settings, outbox и live quote; согласованность HTTP snapshots после смены профиля.
4. Проверка 100 тиков/с, burst 500 тиков/с и медленного браузера. Единственный SQLite writer может задерживать intake на длинной транзакции; текущее значение p95 всего потока не измерено.
5. SIGTERM/Windows shutdown ≤30 с, Telegram uncertain и lease takeover в работающих процессах.
6. Сборка vendor, wheel/Docker smoke, проверка HTTPS session/deep link и E2E на трёх размерах.
7. Проверенный restore полного эксплуатационного набора, включая файлы, и 24-часовой soak.
8. Автоматическая ротация backup-каталогов, завершение модульного рефакторинга и итоговая карта requirement→evidence.

Во время работы обнаружено параллельное изменение `worker.py` и связанных performance-файлов другой задачей. Финальный native прогон должен выполняться по зафиксированному дереву без конкурентных записей.

## Передача и публикация

Изменения остаются в рабочей копии; commit и push этой задачи не выполнены. Для публикации нужны разрешённая запись Git/GitHub и исправное окружение проверки. После этого: зафиксировать дерево, выполнить CI, устранить расхождения новых контрактов и только затем выпускать принятую версию. Подробности запуска и восстановления — в [эксплуатационном документе](OPERATIONS_RU.md).
