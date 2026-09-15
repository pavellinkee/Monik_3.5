"""Level 1: базовый цикл, создание Opportunity и передача Level 2.

Покрывает обязательный список ``10_LEVEL_1_SCANNER.md`` §93 и
``02_LEVEL1_SCANNER.md`` §95.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from monik.config import Configuration, parse_configuration
from monik.domain.enums.capability import CapabilityOperation
from monik.domain.enums.lifecycle import JobStatus, OpportunityStatus, ScanStatus
from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.resources import RequestPriority
from monik.domain.errors import NoRouteError, RateLimitError, ResourceError
from monik.domain.errors import TimeoutError as MonikTimeoutError
from monik.infrastructure.db import Database
from monik.infrastructure.providers.fake import FakeAdapter
from monik.services.observability import FakeClock
from tests import factories as f
from tests.component.level1.conftest import (
    Level1Harness,
    RecordingDispatcher,
    StaticFeeSource,
    StaticGasSource,
    StaticRateSource,
    arbitrage_rule,
    build_harness,
    level1_document,
    mark_unsupported,
)
from tests.unit.config.conftest import VALID_ENV


def configured(**scanner_overrides: object) -> Configuration:
    """Конфигурация с изменёнными параметрами scanner."""
    return parse_configuration(level1_document(**scanner_overrides), environ=dict(VALID_ENV)).config


# --- базовый цикл ---------------------------------------------------------


async def test_scan_creates_opportunity_and_level2_job(harness: Level1Harness) -> None:
    """Основной output Level 1 — Opportunity + Level 2 Job (§91)."""
    result = (await harness.scanner.scan_all())[0]
    assert result.status is ScanStatus.COMPLETE
    assert len(result.opportunities) == 1
    opportunity = result.opportunities[0]
    assert opportunity.status is OpportunityStatus.CREATED
    assert opportunity.buy_provider_id is ProviderId.ONEINCH
    assert opportunity.sell_provider_id is ProviderId.ZERO_X
    assert len(harness.dispatcher.submitted) == 1
    _, job = harness.dispatcher.submitted[0]
    assert job.status is JobStatus.QUEUED
    assert job.opportunity_id == opportunity.opportunity_id


async def test_level2_job_outranks_new_level1_scan(harness: Level1Harness) -> None:
    """Job получает более высокий приоритет, чем новый scan (§45, §59)."""
    await harness.scanner.scan_all()
    _, job = harness.dispatcher.submitted[0]
    assert job.priority is RequestPriority.LEVEL2
    assert job.priority.rank < RequestPriority.LEVEL1_BUY.rank
    assert job.priority.rank < RequestPriority.LEVEL1_SELL.rank


async def test_opportunity_and_job_are_persisted_atomically(harness: Level1Harness) -> None:
    """Opportunity без Job существовать не должна (``CLAUDE.md`` §29)."""
    result = (await harness.scanner.scan_all())[0]
    stored = await harness.opportunities.get_by_v_id(result.opportunities[0].v_id)
    assert stored is not None
    assert stored.opportunity_id == result.opportunities[0].opportunity_id


async def test_max_buy_is_selected_before_sell(harness: Level1Harness) -> None:
    """SELL считается от выхода лучшего BUY (§12)."""
    result = (await harness.scanner.scan_all())[0]
    opportunity = result.opportunities[0]
    amount = opportunity.amounts[0]
    # 1inch даёт 0.050 AAVE за USDT, 0x — 0.049: MAX BUY принадлежит 1inch.
    assert amount.preliminary_buy_output.as_decimal == Decimal(5)
    assert amount.preliminary_sell_output.as_decimal == Decimal("101.5")


async def test_buy_quote_is_requested_for_every_enabled_provider(
    harness: Level1Harness,
) -> None:
    """Оба провайдера участвуют в сравнении (§12, §71)."""
    await harness.scanner.scan_all()
    buys = {
        provider_id: [call for call in adapter.quote_calls if call.operation is OperationType.BUY]
        for provider_id, adapter in harness.adapters.items()
    }
    assert buys[ProviderId.ONEINCH]
    assert buys[ProviderId.ZERO_X]


async def test_sell_starts_from_the_intermediate_token(harness: Level1Harness) -> None:
    """SELL начинается ровно с промежуточного токена BUY (§82)."""
    await harness.scanner.scan_all()
    sells = [
        call
        for adapter in harness.adapters.values()
        for call in adapter.quote_calls
        if call.operation is OperationType.SELL
    ]
    assert sells
    for call in sells:
        assert call.input_token.key == f.AAVE.key
        assert call.output_token.key == f.USDT.key


async def test_scan_metadata_is_persisted(harness: Level1Harness) -> None:
    """Метаданные цикла сохраняются (§57, §76)."""
    result = (await harness.scanner.scan_all())[0]
    stored = await harness.scans.get(result.scan.scan_id)
    assert stored is not None
    assert stored.status is ScanStatus.COMPLETE
    assert stored.statistics.quote_requests > 0
    assert stored.statistics.opportunities_created == 1
    assert stored.finished_at is not None


# --- суммы ----------------------------------------------------------------


async def test_search_uses_a_single_amount(database: Database, clock: FakeClock) -> None:
    """Level 1 ищет одной суммой, а не перебирает все настроенные.

    Стоимость поиска не должна расти вместе с числом сумм, которые
    предстоит проверить Level 2: остальные суммы подставляются в уже
    найденную возможность.
    """
    harness = build_harness(configured(amounts=["100", "500"]), database, clock)
    result = (await harness.scanner.scan_all())[0]
    opportunity = result.opportunities[0]
    assert len(opportunity.amounts) == 1
    # По умолчанию поиск ведётся наименьшей из проверяемых сумм.
    assert opportunity.amounts[0].input_amount.raw == 100_000_000


async def test_search_amount_is_configurable(database: Database, clock: FakeClock) -> None:
    """Сумму поиска задаёт оператор, а не код (``01`` §22)."""
    document = level1_document(amounts=["100", "500"])
    document["scanner"].setdefault("level1", {})["amount"] = "500"
    configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
    harness = build_harness(configuration, database, clock)
    result = (await harness.scanner.scan_all())[0]
    amounts = result.opportunities[0].amounts
    assert [amount.input_amount.raw for amount in amounts] == [500_000_000]


async def test_single_route_snapshot_serves_the_opportunity(
    database: Database, clock: FakeClock
) -> None:
    """Маршрут у возможности один: отдельного маршрута у суммы нет (§24, §89)."""
    harness = build_harness(configured(amounts=["100", "500"]), database, clock)
    result = (await harness.scanner.scan_all())[0]
    opportunity = result.opportunities[0]
    assert opportunity.routes.buy_route.provider_id is ProviderId.ONEINCH
    assert opportunity.routes.sell_route.provider_id is ProviderId.ZERO_X


# --- фильтрация -----------------------------------------------------------


async def test_disabled_provider_is_not_requested(database: Database, clock: FakeClock) -> None:
    """Отключённый провайдер запросов не получает (§71)."""
    document = level1_document()
    document["providers"].append(
        {
            "provider_id": "velora",
            "enabled": False,
            "supported_networks": ["polygon"],
        }
    )
    configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
    adapters = {
        ProviderId.ONEINCH: FakeAdapter(
            ProviderId.ONEINCH, clock, output_rule=arbitrage_rule("0.050", "20.00")
        ),
        ProviderId.ZERO_X: FakeAdapter(
            ProviderId.ZERO_X, clock, output_rule=arbitrage_rule("0.049", "20.30")
        ),
        ProviderId.VELORA: FakeAdapter(
            ProviderId.VELORA, clock, output_rule=arbitrage_rule("0.060", "21.00")
        ),
    }
    harness = build_harness(configuration, database, clock, adapters=adapters)

    result = (await harness.scanner.scan_all())[0]
    assert harness.adapters[ProviderId.VELORA].quote_calls == []
    assert result.opportunities[0].buy_provider_id is ProviderId.ONEINCH


async def test_disabled_token_is_not_scanned(harness: Level1Harness) -> None:
    """Отключённый токен не сканируется (§70)."""
    await harness.scanner.scan_all()
    scanned = {
        call.output_token.symbol
        for adapter in harness.adapters.values()
        for call in adapter.quote_calls
        if call.operation is OperationType.BUY
    }
    assert scanned == {"AAVE"}


async def test_unsupported_capability_blocks_the_request(harness: Level1Harness) -> None:
    """Заведомо неподдерживаемая комбинация во внешний API не уходит (§15, §76)."""
    await mark_unsupported(
        harness.capabilities, ProviderId.ONEINCH, CapabilityOperation.QUOTE_BUY, f.AAVE
    )
    result = (await harness.scanner.scan_all())[0]
    buy_calls = [
        call
        for call in harness.adapters[ProviderId.ONEINCH].quote_calls
        if call.operation is OperationType.BUY
    ]
    assert buy_calls == []
    assert result.scan.statistics.skipped_combinations > 0


async def test_unknown_capability_still_allows_a_runtime_check(
    harness: Level1Harness,
) -> None:
    """UNKNOWN не приравнивается к UNSUPPORTED (§16)."""
    result = (await harness.scanner.scan_all())[0]
    assert harness.adapters[ProviderId.ONEINCH].quote_calls
    assert result.opportunities


async def test_runtime_check_of_unknown_cannot_be_disabled(
    database: Database, clock: FakeClock
) -> None:
    """Запрет runtime-проверки UNKNOWN убран намеренно.

    Реестр возможностей наполняется только discovery, поэтому до первого
    discovery все комбинации UNKNOWN. Запрет на них означал бы, что не
    проверяется ничего — молча, без единой ошибки в логе.
    """
    from monik.config.sections.scanner import Level1Config

    assert not hasattr(Level1Config(), "allow_unknown_capability")

    harness = build_harness(configured(), database, clock)
    result = (await harness.scanner.scan_all())[0]
    assert result.opportunities
    assert any(adapter.quote_calls for adapter in harness.adapters.values())


async def test_same_provider_pair_is_rejected_by_default(
    database: Database, clock: FakeClock
) -> None:
    """Один провайдер на обе ноги по умолчанию запрещён (§18).

    Прибыльный round-trip существует только внутри 1inch; кросс-провайдерная
    комбинация убыточна, поэтому Opportunity не создаётся.
    """
    adapters = {
        ProviderId.ONEINCH: FakeAdapter(
            ProviderId.ONEINCH, clock, output_rule=arbitrage_rule("0.050", "20.30")
        ),
        ProviderId.ZERO_X: FakeAdapter(
            ProviderId.ZERO_X, clock, output_rule=arbitrage_rule("0.049", "20.00")
        ),
    }
    harness = build_harness(configured(), database, clock, adapters=adapters)

    result = (await harness.scanner.scan_all())[0]
    assert result.opportunities == ()
    assert not harness.configuration.routes.allow_same_provider


# --- порог и расходы ------------------------------------------------------


async def test_candidate_below_threshold_is_dropped(database: Database, clock: FakeClock) -> None:
    """Ниже preliminary threshold Opportunity не создаётся (§48)."""
    adapters = {
        ProviderId.ONEINCH: FakeAdapter(
            ProviderId.ONEINCH, clock, output_rule=arbitrage_rule("0.050", "20.00")
        ),
        ProviderId.ZERO_X: FakeAdapter(
            ProviderId.ZERO_X, clock, output_rule=arbitrage_rule("0.049", "20.05")
        ),
    }
    harness = build_harness(configured(), database, clock, adapters=adapters)
    result = (await harness.scanner.scan_all())[0]
    assert result.opportunities == ()
    assert harness.dispatcher.submitted == []


async def test_unknown_gas_blocks_opportunity_creation(
    database: Database, clock: FakeClock
) -> None:
    """Неизвестный обязательный расход не считается нулём (§50)."""
    harness = build_harness(configured(), database, clock, gas=StaticGasSource(f.unknown_gas()))
    result = (await harness.scanner.scan_all())[0]
    assert result.opportunities == ()


async def test_missing_conversion_rate_blocks_opportunity(
    database: Database, clock: FakeClock
) -> None:
    """Без курса стоимость газа неизвестна, а не равна нулю."""
    harness = build_harness(configured(), database, clock, rates=StaticRateSource(None))
    result = (await harness.scanner.scan_all())[0]
    assert result.opportunities == ()


async def test_unknown_fee_blocks_opportunity(database: Database, clock: FakeClock) -> None:
    """UNKNOWN fee не превращается в ноль (§50, ``02`` §32)."""
    harness = build_harness(
        configured(),
        database,
        clock,
        fees=StaticFeeSource(fees=(f.unknown_fee(),)),
    )
    result = (await harness.scanner.scan_all())[0]
    assert result.opportunities == ()


async def test_fees_are_requested_for_both_legs(harness: Level1Harness) -> None:
    """Комиссии берутся из Fee System для BUY и для SELL (§29-30)."""
    await harness.scanner.scan_all()
    operations = {context.operation for context in harness.fees.calls}
    assert operations == {OperationType.BUY, OperationType.SELL}


async def test_gas_estimate_covers_the_whole_round_trip(harness: Level1Harness) -> None:
    """Gas учитывается по обеим ногам (§31, §51)."""
    await harness.scanner.scan_all()
    assert harness.gas.calls
    assert all(units == 400_000 for units in harness.gas.calls if units is not None)


# --- дедупликация и отпечаток --------------------------------------------


async def test_repeated_scan_is_deduplicated_within_window(
    harness: Level1Harness,
) -> None:
    """Тот же кандидат в окне не создаёт вторую Opportunity (§44, §52)."""
    first = (await harness.scanner.scan_all())[0]
    second = (await harness.scanner.scan_all())[0]
    assert len(first.opportunities) == 1
    assert second.opportunities == ()
    assert second.scan.statistics.duplicate_opportunities == 1
    assert len(harness.dispatcher.submitted) == 1


async def test_deduplication_window_expires(database: Database, clock: FakeClock) -> None:
    """За пределами окна та же возможность создаётся заново (§44)."""
    harness = build_harness(
        configured(level1={"deduplication_window_seconds": 60}), database, clock
    )
    await harness.scanner.scan_all()
    clock.advance(timedelta(seconds=120))
    second = (await harness.scanner.scan_all())[0]
    assert len(second.opportunities) == 1


async def test_fingerprint_is_deterministic(harness: Level1Harness) -> None:
    """Отпечаток не зависит от случайного идентификатора (§53)."""
    result = (await harness.scanner.scan_all())[0]
    opportunity = result.opportunities[0]
    assert len(str(opportunity.fingerprint)) == 64
    assert (
        opportunity.fingerprint
        == opportunity.replace(opportunity_id=f.OpportunityId.generate()).fingerprint
    )


# --- ёмкость и ранжирование ----------------------------------------------


async def test_backpressure_limits_created_opportunities(
    database: Database, clock: FakeClock
) -> None:
    """Переполненная очередь Level 2 останавливает создание Job (§47)."""
    harness = build_harness(
        configured(), database, clock, dispatcher=RecordingDispatcher(capacity=0)
    )
    result = (await harness.scanner.scan_all())[0]
    assert result.opportunities == ()
    assert harness.dispatcher.submitted == []


async def test_per_scan_limit_is_respected(database: Database, clock: FakeClock) -> None:
    """Лимит на цикл ограничивает число созданных Opportunity (§48)."""
    harness = build_harness(configured(level1={"max_opportunities_per_scan": 1}), database, clock)
    result = (await harness.scanner.scan_all())[0]
    assert len(result.opportunities) <= 1


# --- изоляция ошибок ------------------------------------------------------


async def test_provider_failure_does_not_stop_the_scan(
    database: Database, clock: FakeClock
) -> None:
    """Ошибка одного провайдера не прекращает цикл (§51, §74)."""
    adapters = {
        ProviderId.ONEINCH: FakeAdapter(
            ProviderId.ONEINCH, clock, output_rule=arbitrage_rule("0.050", "20.00")
        ),
        ProviderId.ZERO_X: FakeAdapter(
            ProviderId.ZERO_X, clock, error=MonikTimeoutError("provider timed out")
        ),
    }
    harness = build_harness(configured(), database, clock, adapters=adapters)
    result = (await harness.scanner.scan_all())[0]
    assert result.status is ScanStatus.PARTIAL
    assert result.failures
    assert result.opportunities == ()


async def test_rate_limit_does_not_create_false_opportunity(
    database: Database, clock: FakeClock
) -> None:
    """Rate limit фиксируется как сбой и не порождает ложную возможность (§53)."""
    adapters = {
        ProviderId.ONEINCH: FakeAdapter(ProviderId.ONEINCH, clock, error=RateLimitError("429")),
        ProviderId.ZERO_X: FakeAdapter(
            ProviderId.ZERO_X, clock, output_rule=arbitrage_rule("0.049", "20.30")
        ),
    }
    harness = build_harness(configured(), database, clock, adapters=adapters)
    result = (await harness.scanner.scan_all())[0]
    assert result.opportunities == ()
    assert any(attempt.error_message == "429" for attempt in result.failures)


async def test_zero_output_quote_is_rejected(database: Database, clock: FakeClock) -> None:
    """Нулевой output валидной возможностью не является (``02`` §25)."""
    adapters = {
        ProviderId.ONEINCH: FakeAdapter(ProviderId.ONEINCH, clock, output_rule=lambda _: 0),
        ProviderId.ZERO_X: FakeAdapter(
            ProviderId.ZERO_X, clock, output_rule=arbitrage_rule("0.049", "20.30")
        ),
    }
    harness = build_harness(configured(), database, clock, adapters=adapters)
    result = (await harness.scanner.scan_all())[0]
    assert result.opportunities == ()
    assert any(
        attempt.rejection_reason == "quote output amount is zero" for attempt in result.failures
    )


async def test_stale_quote_is_rejected(database: Database, clock: FakeClock) -> None:
    """Слишком старая котировка не используется (``02`` §28)."""
    stale_clock = FakeClock(f.NOW)
    adapters = {
        ProviderId.ONEINCH: FakeAdapter(
            ProviderId.ONEINCH, stale_clock, output_rule=arbitrage_rule("0.050", "20.00")
        ),
        ProviderId.ZERO_X: FakeAdapter(
            ProviderId.ZERO_X, stale_clock, output_rule=arbitrage_rule("0.049", "20.30")
        ),
    }
    harness = build_harness(
        configured(level1={"quote_max_age_seconds": 5}), database, clock, adapters=adapters
    )
    clock.advance(timedelta(seconds=60))
    result = (await harness.scanner.scan_all())[0]
    assert result.opportunities == ()
    assert any(
        attempt.rejection_reason == "quote is not fresh enough for this scan"
        for attempt in result.failures
    )


# --- отмена ----------------------------------------------------------------


async def test_cancelled_scan_is_not_complete(harness: Level1Harness) -> None:
    """Отменённый цикл не считается успешным (§67)."""
    task = asyncio.ensure_future(harness.scanner.scan(harness.scanner.scopes()[0]))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    recent = await harness.scans.recent(limit=1)
    assert recent[0].status is ScanStatus.CANCELLED


# --- expiration ------------------------------------------------------------


async def test_opportunity_and_job_expire(harness: Level1Harness) -> None:
    """Opportunity и Job имеют срок жизни (§86, ``02`` §41)."""
    result = (await harness.scanner.scan_all())[0]
    opportunity = result.opportunities[0]
    _, job = harness.dispatcher.submitted[0]

    ttl = harness.configuration.scanner.level1.opportunity_ttl_seconds
    assert opportunity.expires_at == opportunity.detected_at + timedelta(seconds=ttl)
    assert not opportunity.is_expired(f.NOW)
    assert job.expires_at > job.created_at


class TestBlockedByUnknownCost:
    """Видно, почему комбинация не стала возможностью.

    Порог намеренно не засчитывается, когда хотя бы один расход
    неизвестен. Без отдельного счётчика «ноль возможностей» выглядит
    одинаково и когда доходность не дотянула, и когда порог вообще не
    оценивался, — и причина отсева определялась только догадкой по
    совпадению gross и net.
    """

    async def test_unknown_gas_is_counted_and_named(
        self, database: Database, clock: FakeClock
    ) -> None:
        configuration = parse_configuration(level1_document(), environ=dict(VALID_ENV)).config
        # Курс native token недоступен: расход газа посчитать не из чего.
        harness = build_harness(configuration, database, clock, rates=StaticRateSource(rate=None))

        statistics = (await harness.scanner.scan_all())[0].scan.statistics

        assert statistics.blocked_by_unknown_cost > 0
        assert any("gas" in label for label in statistics.unknown_cost_components)

    async def test_complete_calculation_reports_nothing_blocked(
        self, harness: Level1Harness
    ) -> None:
        """Когда все расходы известны, счётчик пуст."""
        statistics = (await harness.scanner.scan_all())[0].scan.statistics

        assert statistics.blocked_by_unknown_cost == 0
        assert statistics.unknown_cost_components == ()


class TestProviderSchedule:
    """Часы работы агрегатора.

    Нужны там, где у провайдера своя суточная квота: расход ограничивают
    не только частотой запросов, но и временем работы. Вне окна запрос не
    отправляется и не отклоняется — он просто не возникает.
    """

    def _document(self, **schedule: object) -> dict[str, Any]:
        document = level1_document()
        for provider in document["providers"]:
            if provider["provider_id"] == "zero_x":
                provider["schedule"] = schedule
        return document

    async def _scan_providers(
        self, document: dict[str, Any], database: Database, clock: FakeClock
    ) -> tuple[str, ...]:
        configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
        harness = build_harness(configuration, database, clock)
        result = (await harness.scanner.scan_all())[0]
        return tuple(provider.value for provider in result.scan.scope.providers)

    async def test_provider_outside_its_window_is_not_scanned(
        self, database: Database, clock: FakeClock
    ) -> None:
        # ``f.NOW`` — полдень UTC; окно ночное, значит 0x отдыхает.
        document = self._document(start="22:00", end="04:00", timezone="UTC")

        providers = await self._scan_providers(document, database, clock)

        assert "zero_x" not in providers
        assert "oneinch" in providers

    async def test_provider_inside_its_window_is_scanned(
        self, database: Database, clock: FakeClock
    ) -> None:
        document = self._document(start="07:00", end="19:00", timezone="UTC")

        providers = await self._scan_providers(document, database, clock)

        assert "zero_x" in providers

    async def test_provider_without_a_schedule_works_around_the_clock(
        self, database: Database, clock: FakeClock
    ) -> None:
        providers = await self._scan_providers(level1_document(), database, clock)

        assert "zero_x" in providers

    async def test_cycle_is_skipped_when_every_provider_rests(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Когда отдыхают все, цикл не нужен: пустая запись не создаётся."""
        document = level1_document()
        for provider in document["providers"]:
            provider["schedule"] = {"start": "22:00", "end": "04:00", "timezone": "UTC"}
        configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
        harness = build_harness(configuration, database, clock)

        assert not harness.scanner.has_active_providers()

    async def test_schedule_does_not_hide_a_provider_from_the_registry(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Отдых — не то же самое, что выключение: провайдер остаётся настроенным."""
        document = self._document(start="22:00", end="04:00", timezone="UTC")
        configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
        harness = build_harness(configuration, database, clock)

        assert harness.providers.is_enabled(ProviderId.ZERO_X)
        assert not harness.providers.is_active(ProviderId.ZERO_X, clock.now())


class TestHonestCounters:
    """Отказ предохранителя и отказ провайдера — разные вещи.

    Запрос, который не покинул Monik, не говорит ничего о работе
    агрегатора. Если считать его неудачной котировкой, открытый circuit
    breaker выглядит как отказ провайдера: успешность цикла падает, хотя
    все ответившие агрегаторы ответили нормально.
    """

    async def test_unsent_requests_do_not_count_as_provider_failures(
        self, database: Database, clock: FakeClock
    ) -> None:
        configuration = parse_configuration(level1_document(), environ=dict(VALID_ENV)).config
        adapters = {
            ProviderId.ONEINCH: FakeAdapter(
                ProviderId.ONEINCH, clock, output_rule=arbitrage_rule("0.050", "20.00")
            ),
            ProviderId.ZERO_X: FakeAdapter(
                ProviderId.ZERO_X,
                clock,
                error=ResourceError(
                    "circuit breaker is open for zero_x/polygon/quote_buy",
                    code="resource_circuit_open",
                ),
            ),
        }
        harness = build_harness(configuration, database, clock, adapters=adapters)

        statistics = (await harness.scanner.scan_all())[0].scan.statistics

        assert statistics.refused_requests > 0
        assert statistics.failed_quotes == 0
        # Отправленные запросы и ответы на них сходятся между собой.
        assert statistics.quote_requests == statistics.successful_quotes
        assert statistics.successful_quotes > 0

    async def test_provider_refusal_still_counts_as_a_failure(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Ответ агрегатора «нет маршрута» остаётся его отказом."""
        configuration = parse_configuration(level1_document(), environ=dict(VALID_ENV)).config
        adapters = {
            ProviderId.ONEINCH: FakeAdapter(
                ProviderId.ONEINCH, clock, output_rule=arbitrage_rule("0.050", "20.00")
            ),
            ProviderId.ZERO_X: FakeAdapter(
                ProviderId.ZERO_X,
                clock,
                error=NoRouteError("no liquidity", code="provider_no_route"),
            ),
        }
        harness = build_harness(configuration, database, clock, adapters=adapters)

        statistics = (await harness.scanner.scan_all())[0].scan.statistics

        assert statistics.failed_quotes > 0
        assert statistics.refused_requests == 0

    async def test_cycle_with_unsent_requests_is_not_reported_as_complete(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Часть комбинаций не посчитана, и цикл это признаёт."""
        configuration = parse_configuration(level1_document(), environ=dict(VALID_ENV)).config
        adapters = {
            ProviderId.ONEINCH: FakeAdapter(
                ProviderId.ONEINCH, clock, output_rule=arbitrage_rule("0.050", "20.00")
            ),
            ProviderId.ZERO_X: FakeAdapter(
                ProviderId.ZERO_X,
                clock,
                error=ResourceError("circuit breaker is open", code="resource_circuit_open"),
            ),
        }
        harness = build_harness(configuration, database, clock, adapters=adapters)

        assert (await harness.scanner.scan_all())[0].scan.status is ScanStatus.PARTIAL


class TestBestCombination:
    """Лучший результат цикла сохраняется независимо от порога.

    Комбинация, не дошедшая до порога, отбрасывается, и по записи «ноль
    возможностей» нельзя понять, не хватило ли десятой доли процента или
    доходность была отрицательной. Без этого длительное наблюдение
    отвечает только на вопрос «нашли или нет».
    """

    async def test_best_is_recorded_even_without_opportunities(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Порог заведомо недостижим, но лучший результат записан."""
        document = level1_document()
        document["profitability"] = {
            "threshold_metric": "net_roi",
            "preliminary_threshold_percent": "999",
            "final_threshold_percent": "999",
        }
        configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
        harness = build_harness(configuration, database, clock)

        result = (await harness.scanner.scan_all())[0]
        assert result.opportunities == ()
        statistics = result.scan.statistics
        assert statistics.evaluated_combinations > 0
        assert statistics.best_combination is not None

    async def test_best_names_the_combination(self, harness: Level1Harness) -> None:
        """Доходность без указания комбинации бесполезна."""
        result = (await harness.scanner.scan_all())[0]
        best = result.scan.statistics.best_combination
        assert best is not None
        assert best.buy_provider in harness.adapters
        assert best.sell_provider in harness.adapters
        assert best.token.network_id == f.POLYGON

    async def test_best_is_the_maximum(self, harness: Level1Harness) -> None:
        """Записывается именно лучшая, а не первая попавшаяся."""
        result = (await harness.scanner.scan_all())[0]
        best = result.scan.statistics.best_combination
        assert best is not None
        assert result.scan.statistics.evaluated_combinations >= 1

    async def test_volatile_best_ignores_stable_tokens(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Стабильная пара выигрывает почти всегда и скрывает остальных.

        У круга между стабильными токенами нет спреда, поэтому он теряет
        меньше других и занимает запись лучшей комбинации. Второе
        значение отвечает на вопрос, ради которого сканирование и
        ведётся: насколько близко были волатильные токены.
        """
        document = level1_document()
        stable_address = "0x3c499c542cEf5E3811e1192ce70d8cC03d5c3359"
        document["tokens"].append(
            {
                "network_id": "polygon",
                "address": stable_address,
                "symbol": "USDC",
                "decimals": 6,
                "rank": 3,
                "usd_stable": True,
            }
        )
        configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
        harness = build_harness(configuration, database, clock)

        statistics = (await harness.scanner.scan_all())[0].scan.statistics

        best_volatile = statistics.best_volatile_combination
        assert best_volatile is not None
        assert best_volatile.token.address.lower() != stable_address.lower()
        assert statistics.best_combination is not None
        assert best_volatile.net_roi.value <= statistics.best_combination.net_roi.value

    async def test_volatile_best_matches_the_overall_best_without_stable_tokens(
        self, harness: Level1Harness
    ) -> None:
        """Без стабильных токенов отбирать нечего: значения совпадают."""
        statistics = (await harness.scanner.scan_all())[0].scan.statistics

        assert statistics.best_volatile_combination == statistics.best_combination

    async def test_counter_ignores_incomplete_calculations(self, harness: Level1Harness) -> None:
        """Незавершённый расчёт в сравнении не участвует."""
        result = (await harness.scanner.scan_all())[0]
        statistics = result.scan.statistics
        assert statistics.evaluated_combinations <= statistics.successful_quotes


class TestStableScan:
    """Учащённый проход по стабильным токенам.

    Круг между стабильными токенами стоит почти ничего, поэтому
    прибыльным становится любое заметное отклонение от паритета. Живёт
    оно минуты, и обычный десятиминутный цикл его не застаёт.
    """

    def _document(self, **overrides: Any) -> dict[str, Any]:
        document = level1_document()
        for token in document["tokens"]:
            if token["symbol"] == "USDT":
                token["usd_stable"] = True
        document["tokens"].append(
            {
                "network_id": "polygon",
                "address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
                "symbol": "USDC",
                "decimals": 6,
                "rank": 3,
                "usd_stable": True,
            }
        )
        document["scanner"].setdefault("level1", {})
        document["scanner"]["level1"].update(overrides)
        return document

    def _builder(self, document: dict[str, Any], database: Database, clock: FakeClock):  # noqa: ANN202
        configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
        return build_harness(configuration, database, clock)

    async def test_scope_contains_only_stable_tokens(
        self, database: Database, clock: FakeClock
    ) -> None:
        harness = self._builder(self._document(), database, clock)

        scopes = harness.scanner.stable_scopes()

        assert len(scopes) == 1
        scope = scopes[0]
        symbols = {harness.tokens.require(key).symbol for key in scope.tokens}
        assert symbols == {"USDC"}

    async def test_provider_outside_the_fast_scan_is_excluded(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Провайдер с суточной квотой в частый проход не берётся."""
        document = self._document()
        for provider in document["providers"]:
            if provider["provider_id"] == "zero_x":
                provider["fast_scan"] = False
        harness = self._builder(document, database, clock)

        scopes = harness.scanner.stable_scopes()

        assert len(scopes) == 1
        scope = scopes[0]
        assert ProviderId.ZERO_X not in scope.providers
        assert ProviderId.ONEINCH in scope.providers

    async def test_without_stable_tokens_there_is_no_scope(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Пустой цикл не создаётся: он только засорял бы историю."""
        harness = self._builder(level1_document(), database, clock)

        assert harness.scanner.stable_scopes() == ()

    async def test_without_participating_providers_there_is_no_scope(
        self, database: Database, clock: FakeClock
    ) -> None:
        document = self._document()
        for provider in document["providers"]:
            provider["fast_scan"] = False
        harness = self._builder(document, database, clock)

        assert harness.scanner.stable_scopes() == ()

    async def test_fast_scan_runs_the_same_level1(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Второй реализации сканера не создаётся: тот же цикл, другой scope."""
        harness = self._builder(self._document(), database, clock)
        scopes = harness.scanner.stable_scopes()
        assert len(scopes) == 1
        scope = scopes[0]

        result = await harness.scanner.scan(scope)

        assert result.scan.scope.tokens == scope.tokens
        assert result.scan.statistics.quote_requests > 0


class TestStableThreshold:
    """Отдельный порог для круга между стабильными токенами.

    Стоимость такого круга почти нулевая, и общий порог отсекал бы ровно
    те отклонения от паритета, ради которых стабильные пары и
    опрашиваются часто.
    """

    def test_stable_pair_uses_its_own_threshold(self) -> None:
        document = level1_document()
        document["profitability"] = {
            "preliminary_threshold_percent": "0.05",
            "final_threshold_percent": "0.05",
            "stable_threshold_percent": "0.01",
        }
        profitability = parse_configuration(document, environ=dict(VALID_ENV)).config.profitability

        assert profitability.threshold_for(stable=True, final=False) == Decimal("0.01")
        assert profitability.threshold_for(stable=True, final=True) == Decimal("0.01")

    def test_volatile_pair_keeps_the_common_threshold(self) -> None:
        document = level1_document()
        document["profitability"] = {
            "preliminary_threshold_percent": "0.05",
            "final_threshold_percent": "0.08",
            "stable_threshold_percent": "0.01",
        }
        profitability = parse_configuration(document, environ=dict(VALID_ENV)).config.profitability

        assert profitability.threshold_for(stable=False, final=False) == Decimal("0.05")
        assert profitability.threshold_for(stable=False, final=True) == Decimal("0.08")

    def test_without_the_setting_nothing_changes(self) -> None:
        """Без настройки поведение прежнее: один порог на всех."""
        document = level1_document()
        document["profitability"] = {
            "preliminary_threshold_percent": "0.05",
            "final_threshold_percent": "0.05",
        }
        profitability = parse_configuration(document, environ=dict(VALID_ENV)).config.profitability

        assert profitability.threshold_for(stable=True, final=False) == Decimal("0.05")

    async def test_stable_combination_passes_the_lower_threshold(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Комбинация между двумя стабильными токенами судится мягче."""
        document = level1_document()
        for token in document["tokens"]:
            if token["symbol"] in {"USDT", "AAVE"}:
                token["usd_stable"] = True
        document["profitability"] = {
            "preliminary_threshold_percent": "999",
            "final_threshold_percent": "999",
            "stable_threshold_percent": "-100",
        }
        configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
        harness = build_harness(configuration, database, clock)

        result = (await harness.scanner.scan_all())[0]
        # Общий порог недостижим, стабильный — достижим: значит для этой
        # пары применён именно стабильный.
        assert result.opportunities
