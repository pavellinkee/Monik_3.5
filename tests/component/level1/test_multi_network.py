"""Сканирование нескольких сетей.

Круг всегда замыкается внутри одной сети: BUY и SELL одной возможности
обязаны относиться к одной сети, а межсетевой арбитраж в текущий workflow
не входит (``10_LEVEL_1_SCANNER.md`` §38). Проверяется именно это: при
двух включённых сетях получается два независимых цикла, а не один общий,
в котором могла бы возникнуть межсетевая комбинация.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from monik.config import Configuration, parse_configuration
from monik.domain.enums.modes import ScanMode
from monik.domain.enums.operations import RoutingMode
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import ConfigurationError
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.db import Database
from monik.infrastructure.providers.contract import AdapterCapabilities
from monik.infrastructure.providers.fake import FakeAdapter
from monik.services.observability import FakeClock
from monik.services.registries import ProviderRegistry, TokenRegistry
from tests.component.level1.conftest import Level1Harness, arbitrage_rule, build_harness
from tests.unit.config.conftest import VALID_ENV, base_document

POLYGON = NetworkId("polygon")
ARBITRUM = NetworkId("arbitrum")

#: Канонические контракты Arbitrum. Публичные константы сети.
ARB_USDT = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"
ARB_WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
ARB_USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
POLYGON_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"


def two_network_document(**scanner_overrides: Any) -> dict[str, Any]:
    """Конфигурация с Polygon и Arbitrum.

    Базовый токен у сетей разный по адресу, хотя символ совпадает:
    одинаковый symbol не означает одинаковый контракт
    (``01_PROJECT_REQUIREMENTS.md`` §11).
    """
    document = copy.deepcopy(base_document())
    document["networks"].append(
        {
            "network_id": "arbitrum",
            "name": "Arbitrum",
            "chain_id": 42161,
            "native_token_symbol": "ETH",
            "wrapped_native_address": ARB_WETH,
            "base_token_address": ARB_USDT,
            "enabled": True,
        }
    )
    document["tokens"].extend(
        [
            {
                "network_id": "arbitrum",
                "address": ARB_USDT,
                "symbol": "USDT",
                "decimals": 6,
                "usd_stable": True,
                "rank": 1,
            },
            {
                "network_id": "arbitrum",
                "address": ARB_USDC,
                "symbol": "USDC",
                "decimals": 6,
                "usd_stable": True,
                "rank": 2,
            },
            {
                "network_id": "arbitrum",
                "address": ARB_WETH,
                "symbol": "WETH",
                "decimals": 18,
                "rank": 3,
            },
        ]
    )
    for provider in document["providers"]:
        provider["supported_networks"] = ["polygon", "arbitrum"]
    document["scanner"]["amounts"] = ["100"]
    document["scanner"].update(scanner_overrides)
    return document


def two_network_configuration(**scanner_overrides: Any) -> Configuration:
    return parse_configuration(
        two_network_document(**scanner_overrides), environ=dict(VALID_ENV)
    ).config


def _adapters(clock: FakeClock) -> dict[ProviderId, FakeAdapter]:
    """Адаптеры, заявляющие обе сети."""
    capabilities = {
        provider_id: AdapterCapabilities(
            provider_id=provider_id,
            supported_networks=frozenset({POLYGON, ARBITRUM}),
            routing_modes=frozenset({RoutingMode.CLASSIC}),
            supports_fixed_route=True,
            supports_fee_discovery=True,
            supports_gas_estimate=True,
        )
        for provider_id in (ProviderId.ONEINCH, ProviderId.ZERO_X)
    }
    return {
        ProviderId.ONEINCH: FakeAdapter(
            ProviderId.ONEINCH,
            clock,
            output_rule=arbitrage_rule("0.050", "20.00"),
            capabilities=capabilities[ProviderId.ONEINCH],
        ),
        ProviderId.ZERO_X: FakeAdapter(
            ProviderId.ZERO_X,
            clock,
            output_rule=arbitrage_rule("0.049", "20.30"),
            capabilities=capabilities[ProviderId.ZERO_X],
        ),
    }


def _harness(document: dict[str, Any], database: Database, clock: FakeClock) -> Level1Harness:
    configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
    return build_harness(configuration, database, clock, adapters=_adapters(clock))


class TestScopePerNetwork:
    """Каждая сеть получает собственный scope."""

    def test_every_enabled_network_is_scanned(self, database: Database, clock: FakeClock) -> None:
        harness = _harness(two_network_document(), database, clock)

        scopes = harness.scanner.scopes(ScanMode.UR)

        assert [scope.networks for scope in scopes] == [(POLYGON,), (ARBITRUM,)]

    def test_scope_never_mixes_networks(self, database: Database, clock: FakeClock) -> None:
        """Межсетевая комбинация не может возникнуть даже случайно."""
        harness = _harness(two_network_document(), database, clock)

        for scope in harness.scanner.scopes(ScanMode.UR):
            assert len(scope.networks) == 1
            network_id = scope.networks[0]
            assert all(key.network_id == network_id for key in scope.tokens)

    def test_disabled_network_is_not_scanned(self, database: Database, clock: FakeClock) -> None:
        """Выключенная сеть не участвует в scan (``02`` §72)."""
        document = two_network_document()
        document["networks"][1]["enabled"] = False
        document["tokens"] = [
            token for token in document["tokens"] if token["network_id"] != "arbitrum"
        ]
        harness = _harness(document, database, clock)

        assert [scope.networks for scope in harness.scanner.scopes(ScanMode.UR)] == [(POLYGON,)]

    def test_provider_is_taken_only_for_the_networks_it_declares(
        self, database: Database, clock: FakeClock
    ) -> None:
        document = two_network_document()
        document["providers"][0]["supported_networks"] = ["polygon"]
        harness = _harness(document, database, clock)

        by_network = {
            scope.networks[0]: scope.providers for scope in harness.scanner.scopes(ScanMode.UR)
        }

        assert ProviderId.ONEINCH in by_network[POLYGON]
        assert ProviderId.ONEINCH not in by_network[ARBITRUM]


class TestBaseTokenBelongsToNetwork:
    """Базовый токен — свойство сети, а не сканера."""

    def test_each_network_has_its_own_base_token(self) -> None:
        configuration = two_network_configuration()
        tokens = TokenRegistry(configuration)

        polygon_base = tokens.base_token(POLYGON)
        arbitrum_base = tokens.base_token(ARBITRUM)

        assert polygon_base.symbol == arbitrum_base.symbol == "USDT"
        assert polygon_base.address != arbitrum_base.address

    def test_base_token_is_excluded_from_its_own_network_only(self) -> None:
        configuration = two_network_configuration()
        tokens = TokenRegistry(configuration)

        arbitrum_symbols = {token.symbol for token in tokens.scan_tokens(ARBITRUM)}

        assert "USDT" not in arbitrum_symbols
        assert arbitrum_symbols == {"USDC", "WETH"}

    def test_top_n_applies_to_each_network_separately(self) -> None:
        """Ограничение говорит, сколько токенов проверять в цикле."""
        configuration = two_network_configuration(level1={"top_tokens": 1})
        tokens = TokenRegistry(configuration)

        assert len(tokens.scan_tokens(POLYGON)) == 1
        assert len(tokens.scan_tokens(ARBITRUM)) == 1


class TestSweep:
    """Проход по всем сетям."""

    async def test_scan_all_runs_one_cycle_per_network(
        self, database: Database, clock: FakeClock
    ) -> None:
        harness = _harness(two_network_document(), database, clock)

        results = await harness.scanner.scan_all(ScanMode.UR)

        assert [result.scan.scope.networks[0] for result in results] == [POLYGON, ARBITRUM]
        assert all(result.scan.statistics.quote_requests > 0 for result in results)

    async def test_stable_pass_covers_every_network(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Учащённый проход идёт по каждой сети, а не по одной."""
        document = two_network_document()
        document["tokens"].append(
            {
                "network_id": "polygon",
                "address": POLYGON_USDC,
                "symbol": "USDC",
                "decimals": 6,
                "usd_stable": True,
                "rank": 3,
            }
        )
        harness = _harness(document, database, clock)

        scopes = harness.scanner.scopes(ScanMode.FEST)

        assert [scope.networks for scope in scopes] == [(POLYGON,), (ARBITRUM,)]
        assert all(
            harness.tokens.require(key).usd_stable for scope in scopes for key in scope.tokens
        )

    async def test_network_without_stable_tokens_gets_no_fast_pass(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Пустой цикл не создаётся: он только засорял бы историю."""
        harness = _harness(two_network_document(), database, clock)

        scopes = harness.scanner.scopes(ScanMode.FEST)

        assert [scope.networks for scope in scopes] == [(ARBITRUM,)]

    async def test_failing_network_does_not_cancel_the_other(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Сети независимы: отказ одной не отменяет поиск во второй."""
        harness = _harness(two_network_document(), database, clock)
        broken = harness.scanner.scopes(ScanMode.UR)[0].networks[0]
        original = harness.scanner.scan

        async def failing(scope, *args, **kwargs):  # noqa: ANN001, ANN202
            if scope.networks[0] == broken:
                raise RuntimeError("network is unreachable")
            return await original(scope, *args, **kwargs)

        harness.scanner.scan = failing  # type: ignore[method-assign]

        results = await harness.scanner.scan_all(ScanMode.UR)

        assert [result.scan.scope.networks[0] for result in results] == [ARBITRUM]


class TestCycleRecord:
    """Запись цикла называет свою сеть."""

    async def test_scan_record_names_its_network(
        self, database: Database, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Без этого записи разных сетей в журнале неотличимы."""
        harness = _harness(two_network_document(), database, clock)

        with caplog.at_level("INFO", logger="monik.services.level1.scanner"):
            await harness.scanner.scan_all(ScanMode.UR)

        networks = [
            record.monik_fields["network"]
            for record in caplog.records
            if record.getMessage() == "level 1 scan finished"
        ]
        assert networks == ["polygon", "arbitrum"]


class TestConfigurationGuards:
    """Непригодная сеть выключается явно, а не пропускается молча."""

    def test_enabled_network_without_a_provider_is_rejected(self) -> None:
        document = two_network_document()
        for provider in document["providers"]:
            provider["supported_networks"] = ["polygon"]
        with pytest.raises(ConfigurationError, match="no enabled provider supports it"):
            parse_configuration(document, environ=dict(VALID_ENV))

    def test_network_without_its_base_token_is_rejected(self) -> None:
        document = two_network_document()
        document["tokens"] = [
            token
            for token in document["tokens"]
            if not (token["network_id"] == "arbitrum" and token["symbol"] == "USDT")
        ]
        with pytest.raises(ConfigurationError, match="unknown or disabled"):
            parse_configuration(document, environ=dict(VALID_ENV))

    def test_network_without_tradable_tokens_is_rejected(self) -> None:
        document = two_network_document()
        document["tokens"] = [
            token
            for token in document["tokens"]
            if token["network_id"] != "arbitrum" or token["symbol"] == "USDT"
        ]
        with pytest.raises(ConfigurationError, match="besides the base token"):
            parse_configuration(document, environ=dict(VALID_ENV))


class TestExchangeLink:
    """Ссылка на обмен может зависеть от сети."""

    def test_network_specific_link_wins_over_the_common_one(self) -> None:
        document = two_network_document()
        document["providers"][0]["ui_url"] = "https://example.org/swap"
        document["providers"][0]["ui_urls"] = {"arbitrum": "https://example.org/swap/arbitrum"}
        providers = ProviderRegistry(parse_configuration(document, environ=dict(VALID_ENV)).config)

        assert providers.ui_url(ProviderId.ONEINCH, ARBITRUM) == "https://example.org/swap/arbitrum"
        assert providers.ui_url(ProviderId.ONEINCH, POLYGON) == "https://example.org/swap"

    def test_link_for_an_unsupported_network_is_rejected(self) -> None:
        document = two_network_document()
        document["providers"][0]["supported_networks"] = ["polygon"]
        document["providers"][0]["ui_urls"] = {"arbitrum": "https://example.org/swap"}
        with pytest.raises(ConfigurationError, match="does not support"):
            parse_configuration(document, environ=dict(VALID_ENV))
