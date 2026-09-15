"""Единый формат сообщения об Opportunity.

Формат централизован (``15_NOTIFICATION_SYSTEM.md`` §47): разные части
приложения не создают собственных hard-coded форматов.

Formatter **ничего не пересчитывает** (§14, §50): он берёт значения из
готового снимка подтверждения и только форматирует их. Округление
выполняется исключительно для отображения и не влияет на исходный
финансовый результат (§49).

Level 2 ID показывается сверху, а к каждому уведомлению прикладывается
кнопка ``об`` (``CLAUDE.md`` §35).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from zoneinfo import ZoneInfo

from monik.config.sections.notifications import NotificationConfig
from monik.domain.enums.lifecycle import AmountConfirmationStatus
from monik.domain.enums.providers import ProviderId
from monik.domain.models.confirmation import AmountSnapshot, ConfirmationSnapshot
from monik.domain.models.token import TokenKey
from monik.domain.value_objects.identity import NetworkId
from monik.services.notifications.reasons import describe_reason
from monik.services.registries.networks import NetworkRegistry
from monik.services.registries.providers import ProviderRegistry
from monik.services.registries.tokens import TokenRegistry

__all__ = ["DETAILS_BUTTON_LABEL", "MESSAGE_PARSE_MODE", "MessageFormatter"]

#: Подпись кнопки, обязательной для каждого уведомления (``CLAUDE.md`` §35).
DETAILS_BUTTON_LABEL = "об"

#: Разметка, которой формируется текст уведомления о возможности. Объявлена
#: здесь, а не в конфигурации: разметку выбирает тот, кто составляет текст,
#: и рассогласовать их невозможно.
MESSAGE_PARSE_MODE = "HTML"

#: Сколько знаков после запятой показывается в процентах и суммах прибыли.
_MONEY_PLACES = 2

#: Разделительная линия перед временем завершения проверки.
_SEPARATOR = "-------------"

#: Отметка суммы, не получившей подтверждения.
_PARTIAL_MARK = "**partial"


class MessageFormatter:
    """Формирует текст уведомления и текст кнопки ``об`` из снимка."""

    def __init__(
        self,
        config: NotificationConfig,
        tokens: TokenRegistry,
        *,
        providers: ProviderRegistry,
        networks: NetworkRegistry,
        timezone: str | None = None,
    ) -> None:
        self._config = config
        self._tokens = tokens
        self._providers = providers
        self._networks = networks
        #: Пояс, в котором оператор читает время. Без него время осталось
        #: бы в UTC и расходилось бы с журналом и его часами.
        self._timezone = ZoneInfo(timezone) if timezone else None

    def render(self, snapshot: ConfirmationSnapshot) -> tuple[str, str]:
        """Вернуть основной текст и текст кнопки ``об``."""
        return (self.render_message(snapshot), self.render_details(snapshot))

    def render_message(self, snapshot: ConfirmationSnapshot) -> str:
        """Основное сообщение об Opportunity.

        Порядок строк задан оператором: идентификатор, сеть, направление
        обмена, страницы обеих ног, затем по блоку на каждую сумму и время
        завершения проверки. Значения берутся из снимка как есть —
        пересчёта здесь нет (``15_NOTIFICATION_SYSTEM.md`` §14, §50).
        """
        emoji = self._config.emoji
        lines = [
            # Level 2 ID располагается сверху (``CLAUDE.md`` §35).
            escape(str(snapshot.k_id).lower()),
            "",
            self._network_line(snapshot),
            f"{emoji.pair} {escape(self._pair(snapshot))}",
            "-",
            "BUY",
            self._provider_line(snapshot.buy_provider_id, snapshot.network_id),
            "SELL",
            self._provider_line(snapshot.sell_provider_id, snapshot.network_id),
            "•••",
        ]
        highlights = _highlights(snapshot.amounts)
        for amount in snapshot.amounts:
            lines.append("")
            lines.extend(self._amount_lines(snapshot, amount, highlights))
        lines.extend(("", _SEPARATOR, f"{emoji.time} {self._local_time(snapshot)}"))
        return "\n".join(lines)

    def render_details(self, snapshot: ConfirmationSnapshot) -> str:
        """Текст кнопки ``об``.

        Он формируется заранее и сохраняется вместе с уведомлением, поэтому
        нажатие кнопки не выполняет ни одного внешнего запроса
        (``CLAUDE.md`` §35).
        """
        lines = [
            f"{snapshot.k_id} — детали",
            f"Сеть: {snapshot.network_id}",
            f"Маршрут BUY: {self._route_line(snapshot, buy=True)}",
            f"Маршрут SELL: {self._route_line(snapshot, buy=False)}",
            f"Отпечаток BUY: {snapshot.routes.buy_route.fingerprint}",
            f"Отпечаток SELL: {snapshot.routes.sell_route.fingerprint}",
            f"Версия расчёта: {snapshot.formula_version}",
        ]
        for amount in snapshot.amounts:
            lines.extend(self._amount_details(snapshot, amount))
        return "\n".join(lines)

    # --- внутреннее -------------------------------------------------------

    def _network_line(self, snapshot: ConfirmationSnapshot) -> str:
        """Строка сети: её значок и название из конфигурации."""
        emoji = self._networks.emoji(snapshot.network_id)
        name = escape(self._networks.display_name(snapshot.network_id))
        return f"{emoji} {name}" if emoji else name

    def _provider_line(self, provider_id: ProviderId, network_id: NetworkId) -> str:
        """Строка агрегатора: его значок и страница обмена.

        Название подставляется только тогда, когда страница не задана:
        оператору нужна ссылка, по которой он совершит обмен, а не имя.
        Ссылка спрашивается вместе с сетью: у части агрегаторов сеть
        зашита в адрес страницы.
        """
        emoji = self._providers.emoji(provider_id)
        target = self._providers.ui_url(provider_id, network_id) or provider_id.value
        return f"{emoji} {escape(target)}" if emoji else escape(target)

    def _local_time(self, snapshot: ConfirmationSnapshot) -> str:
        """Время завершения проверки в поясе оператора."""
        moment = snapshot.confirmed_at
        if self._timezone is not None:
            moment = moment.astimezone(self._timezone)
        return moment.strftime("%H:%M:%S")

    def _pair(self, snapshot: ConfirmationSnapshot) -> str:
        """Тройка токенов цикла (``15_NOTIFICATION_SYSTEM.md`` §38)."""
        return (
            f"{self._symbol(snapshot.input_token)} → "
            f"{self._symbol(snapshot.intermediate_token)} → "
            f"{self._symbol(snapshot.output_token)}"
        )

    def _symbol(self, token: TokenKey) -> str:
        """Символ токена из реестра; идентичность остаётся канонической.

        Символ идентификатором не является (``36_DATA_MODELS.md`` §10):
        он используется только для отображения.
        """
        found = self._tokens.get(token)
        return found.symbol if found is not None else str(token)

    def _route_line(self, snapshot: ConfirmationSnapshot, *, buy: bool) -> str:
        route = snapshot.routes.buy_route if buy else snapshot.routes.sell_route
        steps = " → ".join(step.protocol for step in route.steps) or route.routing_mode.value
        return f"{route.provider_id.value} [{route.routing_mode.value}] {steps}"

    def _amount_lines(
        self,
        snapshot: ConfirmationSnapshot,
        amount: AmountSnapshot,
        highlights: _Highlights,
    ) -> list[str]:
        """Блок одной суммы.

        Доходность и прибыль показываются **всегда**, независимо от того,
        подтверждена сумма или нет: отсутствие подтверждения — причина
        пометки, а не причина скрывать числа.
        """
        emoji = self._config.emoji
        symbol = self._symbol(snapshot.input_token)
        marks = []
        confirmed = amount.confirmation_status is AmountConfirmationStatus.CONFIRMED
        if not confirmed:
            marks.append(emoji.partial)
        if amount is highlights.best_roi:
            marks.append(emoji.best_roi)
        if amount is highlights.best_profit:
            marks.append(emoji.best_profit)
        prefix = "".join(f"{mark} " for mark in marks)
        suffix = "" if confirmed else f" {_PARTIAL_MARK}"
        lines = [
            f"{prefix}{_amount_label(amount)} {escape(symbol)}{suffix}",
            _signed(amount.net_roi.value if amount.net_roi is not None else None, suffix="%"),
            _signed(amount.net_profit, suffix=f" {escape(symbol)}"),
        ]
        reason = describe_reason(amount.rejection_reason)
        if reason is not None:
            # Пояснение — второстепенное: показывается мельче основного
            # текста, насколько это позволяет Telegram.
            lines.append(f"<i>({escape(reason)})</i>")
        return lines

    def _amount_details(self, snapshot: ConfirmationSnapshot, amount: AmountSnapshot) -> list[str]:
        """Разбивка одной суммы для кнопки ``об`` (§44-45)."""
        input_symbol = self._symbol(snapshot.input_token)
        lines = [
            f"— {self._decimal(amount.input_amount.as_decimal)} {input_symbol}"
            f" [{amount.status.value}]",
        ]
        if amount.buy_output is not None:
            lines.append(
                f"  BUY output: {self._decimal(amount.buy_output.as_decimal)} "
                f"{self._symbol(snapshot.intermediate_token)}"
            )
        if amount.sell_output is not None:
            lines.append(
                f"  SELL output: {self._decimal(amount.sell_output.as_decimal)} {input_symbol}"
            )
        costs = amount.costs
        if costs is not None:
            lines.extend(
                [
                    f"  Комиссии: {self._decimal(costs.total_fees)} {input_symbol}",
                    f"  Gas: {self._decimal(costs.gas_cost)} {input_symbol}",
                    f"  Прочие расходы: {self._decimal(costs.other_costs)} {input_symbol}",
                    f"  Rebate: {self._decimal(costs.rebates)} {input_symbol}",
                ]
            )
            if costs.unknown_components:
                lines.append(f"  Неизвестные компоненты: {', '.join(costs.unknown_components)}")
        gas = amount.gas
        if gas is not None:
            lines.append(f"  Gas статус: {gas.status.value}")
        lines.append(f"  Валовая прибыль: {self._optional(amount.gross_profit)} {input_symbol}")
        lines.append(f"  Чистая прибыль: {self._optional(amount.net_profit)} {input_symbol}")
        lines.append(f"  Чистый ROI: {self._roi(amount)}")
        if amount.threshold is not None:
            lines.append(f"  Порог: {self._decimal(amount.threshold)}%")
        if amount.rejection_reason:
            lines.append(f"  Причина: {amount.rejection_reason}")
        return lines

    def _roi(self, amount: AmountSnapshot) -> str:
        if amount.net_roi is None:
            return "n/a"
        return f"{self._decimal(amount.net_roi.value)}%"

    def _optional(self, value: Decimal | None) -> str:
        """Отсутствующее значение показывается как ``n/a``, а не как ноль."""
        return "n/a" if value is None else self._decimal(value)

    def _decimal(self, value: Decimal) -> str:
        """Округление только для отображения (``15_NOTIFICATION_SYSTEM.md`` §49-50)."""
        quantum = Decimal(1).scaleb(-self._config.decimal_places)
        return f"{value.quantize(quantum)}"


@dataclass(frozen=True, slots=True)
class _Highlights:
    """Суммы, выигравшие по доходности и по прибыли.

    Это два разных вопроса: какая сумма выгоднее в процентах и какая
    приносит больше денег. Часто они не совпадают, и оператору важны обе.
    """

    best_roi: AmountSnapshot | None = None
    best_profit: AmountSnapshot | None = None


def _highlights(amounts: tuple[AmountSnapshot, ...]) -> _Highlights:
    """Найти лучшую по проценту и лучшую по прибыли.

    Сравниваются все суммы, включая неподтверждённые: пометка сообщает о
    величине результата, а не о его статусе. При равенстве отмечается
    большая сумма — выбор детерминирован и не зависит от порядка.
    """

    def roi(item: AmountSnapshot) -> Decimal | None:
        return None if item.net_roi is None else item.net_roi.value

    return _Highlights(
        best_roi=_leader(amounts, roi),
        best_profit=_leader(amounts, lambda item: item.net_profit),
    )


def _leader(
    amounts: tuple[AmountSnapshot, ...],
    value: Callable[[AmountSnapshot], Decimal | None],
) -> AmountSnapshot | None:
    """Сумма с наибольшим значением; при равенстве — большая из сумм."""
    best: AmountSnapshot | None = None
    best_value: Decimal | None = None
    for amount in amounts:
        current = value(amount)
        if current is None:
            continue
        if best_value is None or current > best_value:
            best, best_value = amount, current
        elif current == best_value and best is not None:
            if amount.input_amount.as_decimal > best.input_amount.as_decimal:
                best = amount
    return best


def _amount_label(amount: AmountSnapshot) -> str:
    """Сумма без лишних нулей: ``50``, а не ``50.00``."""
    value = amount.input_amount.as_decimal
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal(1)))
    return f"{normalized:f}"


def _signed(value: Decimal | None, *, suffix: str) -> str:
    """Значение со знаком и двумя знаками после запятой.

    Неизвестное значение не превращается в ноль (``CLAUDE.md`` §12):
    вместо числа показывается прочерк.
    """
    if value is None:
        return f"—{suffix}"
    return f"{value:+.{_MONEY_PLACES}f}{suffix}"
