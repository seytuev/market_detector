# FUNCTION_MAP — редизайн 10.10.2026

Снимок перед правками интерфейса. Реестр `FUNCTIONAL_INVENTORY.md` сверен с обработчиками `app.js`, `now.js`, `journal.js`, `events.js`, `alt.js`, `h1_layers.js`, `ltf.js`, `common.js`. Новый экран не удаляет id. Синтетические данные макета к API не подключаются.

Маршрутизатор не переписывается: вид — hash `#now|#desk|#review|#journal|#settings`, алиасы `#overview` → desk и `#events` → журнал. Инструмент и режим H1 остаются в query `?instrument=&mode=h1`. Ссылки Telegram и `/ltf.html?stay=1` не меняются. Секция настроек — query `section`, hash не режется.

Тема: нет сохранённого `lf:theme` → светлая, система не отслеживается. Сохранённые `dark`, `light` и `system` применяются как раньше. Смена темы не вызывает пересчёт.

## Оболочка

| Было | Стало | API | Проверка |
|---|---|---|---|
| Верхние вкладки «Сейчас / Проверка / Журнал / События / Настройки / Структура H1 / Альткоины» | Sidebar: Обзор, Рабочее место, Проверка, Скринер, Журнал, Контекст рынка; Настройки внизу | нет | Клик и hash меняют `.view.active`; назад/вперёд восстанавливают вид |
| H1 в глобальном меню (`#lnk-ltf-nav`, `#lnk-ltf-mobile`) | Режим рабочего места `#lnk-ltf` / `data-chart-mode=h1`; прямой вход `?mode=h1#desk` | `GET /api/ltf/instruments/{id}/structure` | Кнопка «Структура H1» включает `state.chartMode=h1`, query `mode=h1` |
| Нижняя лента из 7 пунктов | Четыре входа + «Ещё» (контекст, журнал, настройки) | нет | ≤767 px: sidebar скрыт, панель «Ещё» открывается и закрывается по Escape |
| `#ws-indicator`, `#connection-label` | Тот же индикатор транспорта внизу sidebar (index, alt, ltf) | WebSocket `/ws` | Точка не описывает свежесть котировки |

Подписи бота «Сейчас» и «Открыть рабочее место» не меняются: это Telegram, не веб-навигация.

## Обзор `#now` (`now.js`)

| Контрол | Экран | API | Проверка |
|---|---|---|---|
| `data-filter=all\|attention\|watch` | Фильтр таблицы | `GET /api/ltf/instruments` | Селектор ограничен `#view-now`, журнал не переключается |
| `#now-stat-review` | Кнопка-счётчик → `#review` | `GET /api/candidates` (D1/W1) | Число не обнуляется при ошибке журнала; переход не меняет статусы |
| `#now-stat-zone`, `#now-stat-eligible` | Фильтры `price_in_zone` и `eligible` | то же обзорное | Повторный клик возвращает «Все» |
| Строка таблицы | Клик и «Выбрать» выделяют строку; название и Enter открывают рабочее место | `GET /api/ltf/instruments/{id}/current` | Поздний снимок чужого id не пишет карточку (`cardSeq`) |
| `.asset-off` | Выключение анализа | `POST /api/instruments/{id}/active` | Подтверждение; UI меняется после ответа |
| `#now-recent` | Последние 5 записей реального журнала | `GET /api/journal?kind=all&limit=5` | Пустой журнал и ошибка не превращаются в нулевые счётчики |
| Пустой список активов | Текст «Анализ выключен…» и ссылка в настройки | обзор | Отличается от пустого фильтра со сбросом |

## Рабочее место `#desk` (`app.js`, `h1_layers.js`)

Все id слоёв сохранены и не слиты. Группы подписей: контекст, структура, отображение.

| Контрол | API | Проверка |
|---|---|---|
| `#instrument-select`, список `#desk-assets-list` | `GET /api/instruments`, `GET /api/ltf/instruments` | Выбор пишет `htf:instrument` и `?instrument=` |
| `#asset-active-btn` | `POST /api/instruments/{id}/active` | Подпись отделена от наблюдения за сценарием; история не удаляется |
| `#tf-select` D1/W1/H1 | `GET /api/candles` | H1 в селекте включает режим структуры, не отдельную страницу |
| `#mode-context`, `#lnk-ltf` | свечи + зоны или structure | Pan/zoom и слои не сбрасываются обновлением котировки |
| `#layer-tf-d1`, `#layer-tf-w1`, `#tf-all`, `#show-candidates`, `#layer-history`, `#layer-mids`, `#layer-labels` | отрисовка уже загруженных зон | Снятие слоя не меняет state зоны |
| `#h1-points`, `#h1-diagnostic`, `#h1-breaks`, `#h1-expected`, `#h1-zones`, `#h1-ob`, `#h1-fvg`, `#h1-bsl`, `#h1-ssl`, `#h1-eligible-only`, `#h1-idea`, `#h1-close-idea`, `#h1-candidates`, `#h1-zone-history`, `#h1-htf` | `GET .../structure`, `POST /api/ltf/ideas/{id}/close` | OB и FVG, BSL и SSL — отдельные флаги |
| `#h1-offscreen` | клиентский подсчёт объектов вне шкалы | Выбор не теряется |
| `#btn-new-zone`, `#choice-draw`, `#choice-form`, `#choice-cancel`, поля `mz-*` | `POST /api/zones/manual` | Escape отменяет рисование |
| `#btn-table-mode`, `data-bucket`, фильтры зон, `#btn-resort`, `#detail-close` | `GET /api/zones`, `/grouped` | Корзины live/candidate/archive |
| Инспектор зоны: review, prefer-entry, reconcile, границы | `POST /api/zones/{id}/review`, `/prefer-entry`, `/reconcile`, `PATCH` | «Сохранено» только после ответа; 422 рядом с полем |
| `#desk-assets-toggle` | нет | ≤1279 px список свёрнут, кнопка его возвращает |
| `#h1-events-context` | `GET /api/events/overview` | Ссылка в контекст рынка, не копия всей страницы |

`/ltf.html` без `stay=1` по-прежнему заменяет адрес на `/?mode=h1#desk`. При `stay=1` остаются наблюдения, фильтры, дни, слои, close и настройки LTF (`ltf.js`).

## Проверка `#review`

| Контрол | API | Проверка |
|---|---|---|
| Очередь, `#review-tf-filter` | `GET /api/candidates` | Пустая очередь не рисует задачу |
| Решения `correct`, `wrong_type`, `fix_boundaries`, `wrong_base`, `now_irrelevant`, `already_breaker`, `no_context` | `POST /api/zones/{id}/review` | Повторная отправка блокируется; следующий кандидат только после успеха |
| `#bounds-*`, выбор якоря | `PATCH /api/zones/{id}` | Якорь не теряется; техническое время в details |
| `#btn-download-labels` | `GET /api/export/labels`, `/api/export/reviews` | Файл после ответа сервера |
| Клавиши 1 / 2 / 3 / → | те же решения | Не срабатывают из поля ввода и из модалки |

## Журнал `#journal`

| Контрол | API | Проверка |
|---|---|---|
| `data-kind=all\|market\|decisions\|delivery` | `GET /api/journal?kind=&limit=100` | Доставка без перехода в объект |
| «Открыть событие» | desk / review / `/ltf.html?instrument&obs` | Решение ищет зону в очереди |
| Группировка 60 с | только показ загруженных строк | Разные kind, текст, статус и id не склеиваются; оригинал с id внутри группы |
| Актив, даты, текст | фильтр уже загруженного limit, не новый endpoint | Подпись, что это не вся история |
| «Новые записи: N» | тот же журнал | Пока список прокручен вниз, DOM не прыгает вверх |

Серверного поиска и постраничности нет: это ограничение контракта, не скрытый отказ.

## Настройки `#settings`

| Контрол | API | Проверка |
|---|---|---|
| Разделы Активы / Уведомления / Оформление / Правила | `GET/POST /api/settings`, группы ответа | Все разделы остаются в документе и на одном скролле; кнопки прокручивают к разделу. Ключ без META не выбрасывается |
| `#settings-save`, `#settings-cancel` | POST только dirty | Статус «сохранено» после ответа; ошибка оставляет draft |
| `#settings-recalc` | текст ответа сервера | Сохранение файла и пересчёт — разные строки |
| `data-theme-choice=system\|dark\|light` | `localStorage lf:theme` | Немедленное применение, без POST настроек детектора |
| Часовой пояс | фиксированный показ Москва · UTC+3 | Селектор пояса не добавляется: сервер его не меняет |
| Активы, два HYPE отдельно | `POST /api/instruments/{id}/active` | Строки не сливаются по тикеру |

Поля `SETTINGS_META` и прочие ключи `detector` рисует существующий `appendSettingsFields`. Проценты и минуты дайджеста — прежний двунаправленный адаптер.

## Контекст рынка `/events.html`

| Контрол | API | Проверка |
|---|---|---|
| `data-mode=now\|feed\|calendar\|rules` | overview, journal, calendar, rules | Четыре режима на месте |
| Ситуация, gates, ситуации | `GET /api/events/overview` | Unknown не становится false; доля лонгов в % и исходная дробь в details |
| Лента | `GET /api/events/journal` | Название + исходный `rule_id`; неизвестный код — «Неизвестное событие» |
| Календарь | `GET /api/events/calendar` | Сетка — проекция `utc_date`; `empty_reason` не подменяется цепочкой |
| Правила | `GET /api/events/rules` | ID, текст и class без правки порогов |

## Скринер `/alt.html`

Контролы реестра (`alt-proc-toggle`, buckets, min/max фильтры, сортировка, поиск, список/таблица, режимы графика, слои, PNG/PNG 2×, журнал сетапа, редактор диапазона preview/save/restore) остаются на прежних id и вызовах `alt.js` / `alt_chart.js`. Заголовок страницы — «Скринер»; расчёт диапазона не меняется. Открытие страницы не вызывает `POST /api/alt/recalc`.

## Что намеренно не меняется

Формулы, пороги, lifecycle, схема БД, causal timestamps, UTC/МСК на сервере, Lightweight Charts 4.2, два инструмента HYPE, тексты бота, уведомления выключенного актива.
