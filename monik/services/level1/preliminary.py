"""Предварительная оценка прибыльности кандидата.

Level 1 не реализует собственную финансовую формулу
(``10_LEVEL_1_SCANNER.md`` §46, ``02_LEVEL1_SCANNER.md`` §3): здесь только
собираются нормализованные входные данные, а расчёт выполняет
:class:`~monik.services.calculator.ProfitCalculator`.

Gas не игнорируется ради скорости (``02_LEVEL1_SCANNER.md`` §31), а
неизвестная обязательная комиссия не считается нулём (§32): такой кандидат
не пройдёт порог и не станет Opportunity.
"""

from __future__ import annotations

from monik.config.sections.profitability import ProfitabilityConfig
from monik.domain.models.conversion import ConversionRate
from monik.domain.models.fee import Fee
from monik.domain.models.gas import Gas
from monik.domain.models.profit import ProfitCalculationInput, ProfitResult
from monik.domain.models.quote import Quote
from monik.domain.models.token import TokenKey
from monik.services.calculator.profit import ProfitCalculator
from monik.services.fees.context import FeeContext
from monik.services.level1.ports import FeeSource, GasSource, RateSource
from monik.services.prices.quoted import gas_rate_from_quotes
from monik.services.registries.networks import NetworkRegistry
from monik.services.registries.tokens import TokenRegistry

__all__ = ["PreliminaryEvaluator"]


class PreliminaryEvaluator:
    """Собирает вход расчёта и получает предварительный результат."""

    def __init__(
        self,
        calculator: ProfitCalculator,
        *,
        fees: FeeSource,
        gas: GasSource,
        rates: RateSource,
        tokens: TokenRegistry,
        networks: NetworkRegistry,
        profitability: ProfitabilityConfig,
    ) -> None:
        self._calculator = calculator
        self._fees = fees
        self._gas = gas
        self._rates = rates
        self._tokens = tokens
        self._networks = networks
        self._profitability = profitability

    async def evaluate(self, buy_quote: Quote, sell_quote: Quote) -> ProfitResult:
        """Предварительный результат для одной суммы."""
        fees = await self._collect_fees(buy_quote, sell_quote)
        gas = await self._gas.estimate(
            buy_quote.network_id,
            gas_units=_total_gas_units(buy_quote, sell_quote),
            quoted_price_wei=_quoted_gas_price(buy_quote, sell_quote),
            # Этап поиска не делает ради газа ни одного лишнего запроса:
            # берётся только то, что уже пришло вместе с котировкой.
            # Полная стоимость сети проверяется на этапе подтверждения.
            allow_remote_lookup=False,
            source="level1_preliminary",
        )
        gas_rate = await self._gas_conversion_rate(buy_quote, sell_quote, gas=gas)
        return self._calculator.calculate(
            ProfitCalculationInput(
                input_amount=buy_quote.input_amount,
                input_token=buy_quote.input_token,
                buy_output=buy_quote.output_amount,
                intermediate_token=buy_quote.output_token,
                sell_output=sell_quote.output_amount,
                output_token=sell_quote.output_token,
                fees=fees,
                gas=gas,
                conversion_rates=() if gas_rate is None else (gas_rate,),
                threshold=self._profitability.threshold_for(
                    stable=self._is_stable(buy_quote.output_token), final=False
                ),
                threshold_metric=self._profitability.threshold_metric,
            )
        )

    def _is_stable(self, token: TokenKey) -> bool:
        """Помечен ли промежуточный токен как стабильный.

        Круг определяется промежуточным токеном: базовый и без того
        стабилен, иначе стабильных пар не существовало бы вовсе.
        """
        found = self._tokens.get(token)
        return found is not None and found.usd_stable

    async def _collect_fees(self, buy_quote: Quote, sell_quote: Quote) -> tuple[Fee, ...]:
        """Комиссии обеих ног цикла.

        Дублирующий запрос ради текущего цикла не выполняется: свежесть
        снимка обеспечивает Fee System (``02_LEVEL1_SCANNER.md`` §30).
        """
        buy_fees = await self._fees.fees_for(_fee_context(buy_quote))
        sell_fees = await self._fees.fees_for(_fee_context(sell_quote))
        return buy_fees + sell_fees

    async def _gas_conversion_rate(
        self, buy_quote: Quote, sell_quote: Quote, *, gas: Gas
    ) -> ConversionRate | None:
        """Курс native token сети в валюту расчёта (решение D-4).

        Сначала пробуем вывести курс из уже полученных котировок: часть
        агрегаторов присылает стоимость газа в долларах, и отдельный
        запрос курса тогда не нужен. Если вывести нельзя, спрашиваем
        обычный источник.

        Отсутствие курса делает стоимость газа неизвестной, а не нулевой:
        подставлять ноль запрещено (``09_PROFIT_CALCULATOR.md`` §16).
        """
        native_key = self._networks.wrapped_native_token(buy_quote.network_id)
        native = self._tokens.get(native_key)
        target = self._tokens.get(sell_quote.output_token)
        if native is None or target is None or native.key == target.key:
            return None
        quoted = gas_rate_from_quotes(
            gas, (buy_quote, sell_quote), target=target, now=gas.observed_at
        )
        if quoted is not None:
            return quoted
        return await self._rates.rate(native, target)


def _fee_context(quote: Quote) -> FeeContext:
    """Контекст комиссий, соответствующий конкретной ноге цикла."""
    return FeeContext(
        provider_id=quote.provider_id,
        network_id=quote.network_id,
        operation=quote.operation,
        input_token=quote.input_token,
        output_token=quote.output_token,
        input_amount=quote.input_amount,
        route_fingerprint=quote.route.fingerprint,
    )


def _quoted_gas_price(buy_quote: Quote, sell_quote: Quote) -> int | None:
    """Цена газа из котировок обеих ног.

    Цена относится к сети, а не к провайдеру, поэтому достаточно, чтобы
    её сообщил хотя бы один из них: значение уже получено вместе с
    котировкой, и отдельный запрос к узлу сети не нужен.
    """
    for quote in (buy_quote, sell_quote):
        if quote.estimated_gas_price_wei is not None:
            return quote.estimated_gas_price_wei
    return None


def _total_gas_units(buy_quote: Quote, sell_quote: Quote) -> int | None:
    """Суммарная оценка газа round-trip.

    Если хотя бы одна нога не сообщила оценку, суммарное значение
    неизвестно — достраивать его нельзя.
    """
    if buy_quote.estimated_gas_units is None or sell_quote.estimated_gas_units is None:
        return None
    return buy_quote.estimated_gas_units + sell_quote.estimated_gas_units
