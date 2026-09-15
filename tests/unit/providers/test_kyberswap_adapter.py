"""Переходники KyberSwap: формы отказа и разбор ответа.

Формы сняты с живого API 2026-09-13, коды подтверждены документацией
``docs.kyberswap.com``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from monik.domain.enums.errors import ErrorCategory
from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import DataError, MonikError, NoRouteError
from monik.infrastructure.providers import QuoteRequest
from monik.infrastructure.providers.kyberswap import KyberSwapAdapter
from monik.services.observability import FakeClock
from tests import factories as f
from tests.contract.test_kyberswap_contract import ROUTES_PAYLOAD
from tests.unit.providers.support import (
    http_returning,
    provider_config,
    resource_manager,
    secret,
)


def _clock() -> FakeClock:
    return FakeClock(f.NOW)


def _adapter(clock: FakeClock, payload: object, status: int = 200) -> KyberSwapAdapter:
    return KyberSwapAdapter(
        provider_config(ProviderId.KYBERSWAP),
        http=http_returning(payload, status),
        resources=resource_manager(clock),
        clock=clock,
        api_key=secret(),
    )


def _request(**overrides: object) -> QuoteRequest:
    base: dict[str, object] = {
        "network_id": f.POLYGON,
        "operation": OperationType.BUY,
        "input_token": f.USDT,
        "output_token": f.AAVE,
        "input_amount": f.USDT.amount_from_base_units(100_000_000),
        "request_id": f.RequestId.generate(),
    }
    base.update(overrides)
    return QuoteRequest(**base)  # type: ignore[arg-type]


class TestSuccessfulQuote:
    async def test_output_and_gas_come_from_the_route_summary(self) -> None:
        """Газ приходит вместе с котировкой: узел сети спрашивать не нужно."""
        adapter = _adapter(_clock(), ROUTES_PAYLOAD)

        quote = await adapter.get_quote(_request())

        assert quote.output_amount.raw == 8_899_446_567_405_885_440
        assert quote.estimated_gas_units == 629_502
        assert quote.estimated_gas_price_wei == 279_394_223_245
        assert quote.estimated_gas_cost_usd == Decimal("0.0168811")

    async def test_route_names_the_liquidity_sources(self) -> None:
        adapter = _adapter(_clock(), ROUTES_PAYLOAD)

        quote = await adapter.get_quote(_request())

        assert quote.route.steps[0].protocol == "uniswapv3"

    async def test_quote_for_another_amount_is_rejected(self) -> None:
        """Ответ о другой сумме — испорченные данные, а не котировка."""
        summary = {**ROUTES_PAYLOAD["data"]["routeSummary"], "amountIn": "1"}  # type: ignore[dict-item]
        adapter = _adapter(_clock(), {"data": {"routeSummary": summary}})

        with pytest.raises(DataError, match="different source amount"):
            await adapter.get_quote(_request())

    async def test_missing_route_summary_is_a_data_error(self) -> None:
        adapter = _adapter(_clock(), {"code": 0, "data": {}})

        with pytest.raises(DataError):
            await adapter.get_quote(_request())


class TestErrorForms:
    """Каждая распознанная форма отказа проверяется отдельно."""

    @pytest.mark.parametrize(
        ("code", "fragment"),
        [
            (4008, "no route"),
            (4010, "no eligible pool"),
            (4011, "does not know one of the requested tokens"),
        ],
    )
    async def test_documented_codes_mean_no_route(self, code: int, fragment: str) -> None:
        adapter = _adapter(_clock(), {"code": code, "message": "route not found"}, status=400)

        with pytest.raises(NoRouteError, match=fragment):
            await adapter.get_quote(_request())

    async def test_no_route_is_not_retried(self) -> None:
        """Отсутствие маршрута — штатный ответ: повтор ничего не изменит."""
        adapter = _adapter(_clock(), {"code": 4008, "message": "route not found"}, status=400)

        with pytest.raises(NoRouteError) as error:
            await adapter.get_quote(_request())

        assert error.value.info.category is ErrorCategory.NO_ROUTE

    async def test_unknown_code_stays_a_data_error(self) -> None:
        """Неизвестное не превращается в штатный отрицательный результат."""
        adapter = _adapter(_clock(), {"code": 4000, "message": "bad request"}, status=400)

        with pytest.raises(MonikError) as error:
            await adapter.get_quote(_request())

        assert error.value.info.category is not ErrorCategory.NO_ROUTE

    async def test_error_detail_names_the_provider_code(self) -> None:
        """Без кода и сообщения отказ 400 не объясняет причину."""
        adapter = _adapter(_clock(), {"code": 4000, "message": "bad request"}, status=400)

        with pytest.raises(MonikError) as error:
            await adapter.get_quote(_request())

        assert "4000" in (error.value.info.message or "")


class TestHeaders:
    def test_client_id_is_sent_instead_of_an_api_key(self) -> None:
        """Ключ API не нужен; заголовок лишь называет приложение."""
        adapter = _adapter(_clock(), ROUTES_PAYLOAD)

        headers = adapter.auth_headers()

        assert set(headers) == {"x-client-id"}
        assert "authorization" not in {name.lower() for name in headers}

    def test_client_id_falls_back_to_the_application_name(self) -> None:
        clock = _clock()
        adapter = KyberSwapAdapter(
            provider_config(ProviderId.KYBERSWAP),
            http=http_returning(ROUTES_PAYLOAD),
            resources=resource_manager(clock),
            clock=clock,
            api_key=None,
        )

        assert adapter.auth_headers() == {"x-client-id": "monik"}
