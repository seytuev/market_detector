# Действующие контракты LevelFrame

Этот индекс связывает техническую стабилизацию с существующими правилами рынка. Статусы реализации и приёмки приведены в [отчёте](LevelFrame_Stabilization_Implementation_RU_2026_10_13.md).

| Область | Документ | Проверки |
|---|---|---|
| Стабилизация | [ТЗ](LevelFrame_Stabilization_TZ_RU_2026_10_12.md) | `test_stabilization_storage`, `test_stabilization_pipeline`, `test_stabilization_settings`; CI |
| Производительность | [ТЗ](Service_Performance_Optimization_Spec_RU_2026_10_12.md) | `test_calc_queue`, `test_metrics`, storage-tail benchmark; полный поток ещё не принят |
| Свечи | [ТЗ](Candle_Synchronization_Spec_RU_2026_10_12.md) | `test_data_sync`, `test_candle_revision`, `test_realtime_binance` |
| HTF | [Исходная спецификация](../HTF_Zones_Spec_v0.1.md), [дополнение](../Unified_Zones_Engine_Spec_Addendum_v0.2.md) | HTF acceptance, replay, lifecycle |
| LTF | [Правила контекста](HTF_LTF_Context_Rules_v1_2026-10-10.md) | LTF engine, context, ranges, entries, causality |
| H1 OB/FVG | [ТЗ](H1_OB_FVG_After_BOS_SMS_Spec_RU_2026-10-11.md) | `test_h1_zones_after_break`, `test_ltf_ob_fvg_lifecycle` |
| ALT | [Уточнение структур](Alt_Range_Engine_Refinement_TZ_RU_2026_10_12.md) | `test_alt_rules_v04`, эталонные fixtures |
| Events | [ТЗ](Events_Time_Market_Direction_Spec_RU_2026_10_11.md) | Events rules/store/stats/API |

Runtime registry различает основной ALT v1/v2 и структурную геометрию alt-0.4, включённую предшествующим commit `9959eef`. Уведомления v1/v2 сохраняют собственный канал. Эта стабилизация не включает новую торговую стратегию.

Для добавления требования указывать версию контракта, соответствующий тест либо протокол ручной проверки и фактическую runtime version. Исторические аудиты остаются историческими: их замечания проверяются по текущему коду перед повторной реализацией.

## Расширение исследований

[Пакет расширенных ТЗ от 15.10.2026](research-expansion-2026-10/README.md): 01–06 внедрены в объёме рабочих этапов через перенос модулей STVTerminal внутрь Market Researcher. Фактический объём и отложенные расширения приведены в начале каждого ТЗ. Проверки — tests/test_research.py и test_research_workbench.py на SQLite/PostgreSQL, изолированные браузерные сценарии; нагрузочная приёмка влияния на основной worker ещё не завершена. Эти модули не заменяют контракты HTF/H1/ALT/Events.
