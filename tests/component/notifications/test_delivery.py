"""Notification System: формат, порядок, retry, fan-out и recovery.

Покрывает обязательный список ``15_NOTIFICATION_SYSTEM.md`` §76.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from monik.domain.enums.lifecycle import (
    AmountConfirmationStatus,
    NotificationStatus,
    OpportunityStatus,
)
from monik.domain.enums.notifications import DeliveryErrorKind, NotificationMode
from monik.domain.models.confirmation import AmountSnapshot, ConfirmationSnapshot
from monik.domain.value_objects.amounts import Percentage
from monik.infrastructure.db import Database
from monik.infrastructure.telegram import FakeTransport
from monik.services.notifications import (
    DETAILS_BUTTON_LABEL,
    DeliveryReceipt,
    OutgoingMessage,
    details_callback_data,
    mode_decision,
)
from monik.services.observability import FakeClock
from monik.services.opportunity import DeliveryOutcome
from tests.component.notifications.conftest import (
    SECOND,
    TELEGRAM,
    NotificationHarness,
    build_notifications,
    configured,
)


@pytest.fixture
async def harness(database: Database, clock: FakeClock) -> NotificationHarness:
    return await build_notifications(database, clock)


# --- формат сообщения -----------------------------------------------------


async def test_message_starts_with_level2_id(harness: NotificationHarness) -> None:
    """Level 2 ID показывается сверху (``CLAUDE.md`` §35)."""
    text = harness.formatter.render_message(harness.snapshot)
    assert text.splitlines()[0] == str(harness.result.k_id).lower()


async def test_message_contains_required_fields(harness: NotificationHarness) -> None:
    """Сеть, пара токенов, обе ноги, суммы и время (§37-43)."""
    text = harness.formatter.render_message(harness.snapshot)
    lines = text.splitlines()

    assert lines[1] == ""
    assert "Polygon" in lines[2]
    assert lines[3].endswith("USDT → AAVE → USDT")
    assert lines[4] == "-"
    assert lines[5] == "BUY"
    assert harness.snapshot.buy_provider_id.value in lines[6]
    assert lines[7] == "SELL"
    assert harness.snapshot.sell_provider_id.value in lines[8]
    assert lines[9] == "•••"
    assert "-------------" in text
    # Время завершения проверки — последней строкой.
    assert lines[-1].endswith(harness.snapshot.confirmed_at.strftime("%H:%M:%S"))


async def test_details_text_contains_route_and_costs(harness: NotificationHarness) -> None:
    """Кнопка ``об`` показывает маршрут, комиссии, gas и версию расчёта (§44-46)."""
    details = harness.formatter.render_details(harness.snapshot)
    assert "Маршрут BUY" in details
    assert "Маршрут SELL" in details
    assert str(harness.snapshot.routes.buy_route.fingerprint) in details
    assert "Комиссии" in details
    assert "Gas" in details
    assert "Версия расчёта: 1" in details


async def test_formatter_does_not_recalculate(harness: NotificationHarness) -> None:
    """Formatter переносит значения снимка без пересчёта (§14, §50)."""
    amount = harness.snapshot.amounts[0]
    assert amount.net_profit is not None and amount.net_roi is not None
    text = harness.formatter.render_message(harness.snapshot)

    # Формат оператора: знак и два знака после запятой.
    assert f"{amount.net_profit:+.2f}" in text
    assert f"{amount.net_roi.value:+.2f}" in text
    # Округление выполнено только для отображения: снимок не изменился.
    assert harness.snapshot.amounts[0].net_profit == amount.net_profit


async def test_display_precision_is_configurable(database: Database, clock: FakeClock) -> None:
    """Точность разбивки задаётся конфигурацией (§49).

    Основное сообщение показывает две цифры после запятой по решению
    оператора, а подробная раскладка кнопки ``об`` остаётся настраиваемой.
    """
    harness = await build_notifications(database, clock, configuration=configured(decimal_places=4))
    details = harness.formatter.render_details(harness.snapshot)
    amount = harness.snapshot.amounts[0]
    assert amount.net_profit is not None
    assert f"{amount.net_profit:.4f}" in details


# --- очередь и доставка ---------------------------------------------------


async def test_pending_notification_is_delivered(harness: NotificationHarness) -> None:
    """Уведомление доставляется и получает SENT (§62)."""
    report = await harness.dispatcher.dispatch_pending()

    assert len(report.delivered) == 1
    stored = await harness.notifications.list_for_opportunity(harness.opportunity.opportunity_id)
    assert stored[0].status is NotificationStatus.SENT


async def test_delivery_stores_the_external_message_id(
    database: Database, clock: FakeClock
) -> None:
    """Telegram message ID сохраняется (§63)."""
    transport = FakeTransport(receipt=DeliveryReceipt(delivered=True, external_message_id="4242"))
    harness = await build_notifications(database, clock, transport=transport)
    await harness.dispatcher.dispatch_pending()

    stored = await harness.notifications.list_for_opportunity(harness.opportunity.opportunity_id)
    attempts = await harness.notifications.list_attempts(stored[0].notification_id)
    assert attempts[-1].external_message_id == "4242"


async def test_every_notification_carries_the_details_button(
    harness: NotificationHarness,
) -> None:
    """Кнопка ``об`` присутствует в каждом уведомлении (``CLAUDE.md`` §35)."""
    await harness.dispatcher.dispatch_pending()

    assert harness.transport.sent
    for message in harness.transport.sent:
        assert message.details_label == DETAILS_BUTTON_LABEL
        assert message.details_callback is not None
        assert message.details_callback.startswith("details:")


async def test_details_button_uses_stored_text_only(harness: NotificationHarness) -> None:
    """Данные кнопки берутся из сохранённого снимка, без новых запросов."""
    await harness.dispatcher.dispatch_pending()
    stored = await harness.notifications.list_for_opportunity(harness.opportunity.opportunity_id)
    notification_id = stored[0].notification_id
    message, details = await harness.notifications.load_texts(notification_id)

    assert details is not None and str(harness.result.k_id) in details
    assert harness.transport.sent[0].details_callback == details_callback_data(notification_id)
    assert message == harness.transport.sent[0].text


async def test_ordering_follows_creation_not_profit(database: Database, clock: FakeClock) -> None:
    """Порядок задаётся ``created_at`` + sequence (``CLAUDE.md`` §37)."""
    harness = await build_notifications(database, clock, destinations=(TELEGRAM, SECOND))
    await harness.dispatcher.dispatch_pending()

    assert [message.destination.destination_id for message in harness.transport.sent] == [
        TELEGRAM.destination_id,
        SECOND.destination_id,
    ]


# --- retry и ошибки -------------------------------------------------------


async def test_temporary_error_is_retried_with_backoff(
    database: Database, clock: FakeClock
) -> None:
    """Временная ошибка ставит уведомление в RETRY_WAIT (§25-26, §66)."""
    transport = FakeTransport(
        receipt=DeliveryReceipt(
            delivered=False, error_kind=DeliveryErrorKind.NETWORK_ERROR, error_message="down"
        )
    )
    harness = await build_notifications(database, clock, transport=transport)
    report = await harness.dispatcher.dispatch_pending()

    assert report.retried and not report.failed
    stored = await harness.notifications.list_for_opportunity(harness.opportunity.opportunity_id)
    assert stored[0].status is NotificationStatus.RETRY_WAIT
    assert stored[0].next_attempt_at is not None
    assert stored[0].next_attempt_at > harness.clock.now()


async def test_rate_limit_honours_retry_after(database: Database, clock: FakeClock) -> None:
    """``Retry-After`` имеет приоритет над расчётной задержкой (``CLAUDE.md`` §32)."""
    transport = FakeTransport(
        receipt=DeliveryReceipt(
            delivered=False,
            error_kind=DeliveryErrorKind.RATE_LIMIT,
            error_message="429",
            retry_after_seconds=45,
        )
    )
    harness = await build_notifications(database, clock, transport=transport)
    await harness.dispatcher.dispatch_pending()

    stored = await harness.notifications.list_for_opportunity(harness.opportunity.opportunity_id)
    assert stored[0].next_attempt_at == harness.clock.now() + timedelta(seconds=45)


async def test_permanent_error_is_not_retried(database: Database, clock: FakeClock) -> None:
    """Неверные credentials не повторяются бесконечно (§65, §67)."""
    transport = FakeTransport(
        receipt=DeliveryReceipt(
            delivered=False, error_kind=DeliveryErrorKind.AUTH_ERROR, error_message="401"
        )
    )
    harness = await build_notifications(database, clock, transport=transport)
    report = await harness.dispatcher.dispatch_pending()

    assert report.failed and not report.retried
    stored = await harness.notifications.list_for_opportunity(harness.opportunity.opportunity_id)
    assert stored[0].status is NotificationStatus.FAILED
    assert len(transport.sent) == 1


async def test_retry_limit_finalises_as_failed(database: Database, clock: FakeClock) -> None:
    """После лимита попыток уведомление окончательно FAILED (§27)."""
    transport = FakeTransport(
        receipt=DeliveryReceipt(
            delivered=False, error_kind=DeliveryErrorKind.NETWORK_ERROR, error_message="down"
        )
    )
    harness = await build_notifications(
        database, clock, transport=transport, configuration=configured(max_attempts=2)
    )

    await harness.dispatcher.dispatch_pending()
    clock.advance(timedelta(minutes=10))
    report = await harness.dispatcher.dispatch_pending()

    assert report.failed
    stored = await harness.notifications.list_for_opportunity(harness.opportunity.opportunity_id)
    assert stored[0].status is NotificationStatus.FAILED
    assert stored[0].attempt_count == 2


async def test_delivery_failure_keeps_opportunity_confirmed(
    database: Database, clock: FakeClock
) -> None:
    """Ошибка Telegram не отменяет подтверждённую возможность (§24)."""
    transport = FakeTransport(
        receipt=DeliveryReceipt(
            delivered=False, error_kind=DeliveryErrorKind.AUTH_ERROR, error_message="401"
        )
    )
    harness = await build_notifications(database, clock, transport=transport)
    await harness.dispatcher.dispatch_pending()

    stored = await harness.opportunities.get(harness.opportunity.opportunity_id)
    assert stored is not None and stored.status is OpportunityStatus.CONFIRMED


async def test_successful_notification_is_not_resent(harness: NotificationHarness) -> None:
    """Уже доставленное уведомление повторно не отправляется (§60)."""
    await harness.dispatcher.dispatch_pending()
    await harness.dispatcher.dispatch_pending()

    assert len(harness.transport.sent) == 1


# --- fan-out --------------------------------------------------------------


async def test_destination_failure_is_isolated(database: Database, clock: FakeClock) -> None:
    """Сбой одного назначения не блокирует остальные (§55-57)."""

    def rule(attempt: int, message: OutgoingMessage) -> DeliveryReceipt:
        if message.destination.destination_id == TELEGRAM.destination_id:
            return DeliveryReceipt(
                delivered=False,
                error_kind=DeliveryErrorKind.DESTINATION_ERROR,
                error_message="chat not found",
            )
        return DeliveryReceipt(delivered=True, external_message_id=str(attempt))

    harness = await build_notifications(
        database,
        clock,
        transport=FakeTransport(rule=rule),
        destinations=(TELEGRAM, SECOND),
    )
    report = await harness.dispatcher.dispatch_pending()

    assert len(report.delivered) == 1
    assert len(report.failed) == 1
    stored = await harness.notifications.list_for_opportunity(harness.opportunity.opportunity_id)
    statuses = {item.destination.destination_id: item.status for item in stored}
    assert statuses[TELEGRAM.destination_id] is NotificationStatus.FAILED
    assert statuses[SECOND.destination_id] is NotificationStatus.SENT


async def test_partial_fan_out_marks_notified_partial(database: Database, clock: FakeClock) -> None:
    """Частичная доставка отражается статусом NOTIFIED_PARTIAL (``35`` §63)."""
    harness = await build_notifications(database, clock, destinations=(TELEGRAM, SECOND))
    status = await harness.service.record_delivery(
        harness.opportunity.opportunity_id,
        (
            DeliveryOutcome(TELEGRAM.destination_id, True),
            DeliveryOutcome(SECOND.destination_id, False),
        ),
    )
    assert status is OpportunityStatus.NOTIFIED_PARTIAL


# --- recovery -------------------------------------------------------------


async def test_interrupted_sending_is_requeued(database: Database, clock: FakeClock) -> None:
    """Крах во время отправки не делает уведомление SENT (§61)."""
    harness = await build_notifications(database, clock)
    stored = await harness.notifications.list_for_opportunity(harness.opportunity.opportunity_id)
    await harness.notifications.update_delivery_state(
        stored[0].notification_id,
        NotificationStatus.SENDING,
        updated_at=clock.now(),
        attempt_count=1,
    )

    recovered = await harness.dispatcher.recover_interrupted()

    assert len(recovered) == 1
    after = await harness.notifications.get(stored[0].notification_id)
    assert after is not None and after.status is NotificationStatus.QUEUED


async def test_sent_notification_is_not_recovered(harness: NotificationHarness) -> None:
    """Уже отправленные уведомления recovery не трогает (§60)."""
    await harness.dispatcher.dispatch_pending()
    recovered = await harness.dispatcher.recover_interrupted()

    assert recovered == ()
    assert len(harness.transport.sent) == 1


# --- валидация и режимы ---------------------------------------------------


async def test_message_without_text_is_not_sent(database: Database, clock: FakeClock) -> None:
    """Некорректное сообщение не отправляется (§68-69)."""
    harness = await build_notifications(database, clock, queue=False)
    await harness.service.record_confirmation(harness.opportunity, harness.result)
    stored = await harness.notifications.list_for_opportunity(harness.opportunity.opportunity_id)
    await database.execute(
        "UPDATE notifications SET message_text = NULL WHERE notification_id = ?",
        (stored[0].notification_id,),
    )

    report = await harness.dispatcher.dispatch_pending()

    assert report.failed
    assert harness.transport.sent == []


def test_mode_rules_control_sending_only() -> None:
    """Режим влияет только на правила отправки (``CLAUDE.md`` §38)."""
    permissive = configured()
    assert permissive.notifications.rules_for(NotificationMode.A).send_partial is True

    restrictive = configured(mode_b={"send_partial": False})
    rules = restrictive.notifications.rules_for(NotificationMode.B)
    assert rules.send_partial is False
    assert rules.send_confirmed is True


async def test_mode_decision_uses_the_snapshot(harness: NotificationHarness) -> None:
    """Решение принимается по снимку и ничего не пересчитывает (§79)."""
    decision = mode_decision(
        harness.configuration.notifications, NotificationMode.A, harness.snapshot
    )
    assert decision.send is True
    assert decision.reason is None


# --- формат уведомления оператора ----------------------------------------


class TestOperatorFormat:
    """Вид сообщения задан оператором и проверяется поэлементно.

    Ссылка на страницу обмена важнее названия агрегатора: по ней оператор
    и совершает сделку, поэтому в сообщение идёт именно она.
    """

    def _render(self, harness: NotificationHarness) -> str:
        return harness.formatter.render_message(harness.snapshot)

    async def test_provider_links_replace_names(self, database: Database, clock: FakeClock) -> None:
        configuration = configured()
        for provider in configuration.providers:
            object.__setattr__(provider, "emoji", "🧩")
            object.__setattr__(provider, "ui_url", "https://example.org/swap")
        harness = await build_notifications(database, clock, configuration=configuration)

        text = self._render(harness)

        assert "🧩 https://example.org/swap" in text

    async def test_provider_without_a_link_falls_back_to_its_name(
        self, harness: NotificationHarness
    ) -> None:
        """Ссылки может не быть — сообщение остаётся осмысленным."""
        text = self._render(harness)

        assert harness.snapshot.buy_provider_id.value in text

    async def test_amounts_are_round_and_values_signed(self, harness: NotificationHarness) -> None:
        text = self._render(harness)
        amount = harness.snapshot.amounts[0]
        assert amount.net_roi is not None

        label = f"{amount.input_amount.as_decimal.normalize():f}"
        assert f"{label} USDT" in text
        assert "." not in label
        assert f"{amount.net_roi.value:+.2f}%" in text

    async def test_best_roi_and_best_profit_are_marked(self, harness: NotificationHarness) -> None:
        """Лучший процент и лучшая прибыль — разные вопросы и разные значки."""
        emoji = harness.configuration.notifications.emoji
        text = self._render(harness)

        assert emoji.best_roi in text
        assert emoji.best_profit in text

    @staticmethod
    def _with_amounts(
        harness: NotificationHarness, *amounts: AmountSnapshot
    ) -> ConfirmationSnapshot:
        """Снимок с заданным набором сумм."""
        return harness.snapshot.model_copy(update={"amounts": amounts})

    @staticmethod
    def _amount(
        template: AmountSnapshot,
        *,
        raw: int,
        roi: str,
        profit: str,
        confirmed: bool = True,
        reason: str | None = None,
    ) -> AmountSnapshot:
        """Сумма с заданными результатом и статусом."""
        return template.model_copy(
            update={
                "input_amount": template.input_amount.model_copy(update={"raw": raw}),
                "net_roi": Percentage(value=Decimal(roi)),
                "net_profit": Decimal(profit),
                "confirmation_status": (
                    AmountConfirmationStatus.CONFIRMED
                    if confirmed
                    else AmountConfirmationStatus.UNCONFIRMED
                ),
                "rejection_reason": reason,
            }
        )

    async def test_unconfirmed_amount_keeps_its_numbers(self, harness: NotificationHarness) -> None:
        """Отсутствие подтверждения — повод для пометки, а не для молчания."""
        template = harness.snapshot.amounts[0]
        snapshot = self._with_amounts(
            harness,
            self._amount(template, raw=50_000_000, roi="0.40", profit="0.20"),
            self._amount(
                template,
                raw=1_000_000_000,
                roi="-0.07",
                profit="-0.70",
                confirmed=False,
                reason="net result is below the profitability threshold",
            ),
        )

        text = harness.formatter.render_message(snapshot)

        assert "⚠️ " in text
        assert "**partial" in text
        assert "-0.07%" in text
        assert "-0.70 USDT" in text
        # Пояснение переведено и показано мельче основного текста.
        assert "<i>(итог ниже порога прибыльности)</i>" in text

    async def test_unknown_reason_is_shown_as_is(self, harness: NotificationHarness) -> None:
        """Незнакомую причину не переводим наугад."""
        template = harness.snapshot.amounts[0]
        snapshot = self._with_amounts(
            harness,
            self._amount(
                template,
                raw=50_000_000,
                roi="0.10",
                profit="0.05",
                confirmed=False,
                reason="provider says something new",
            ),
        )

        text = harness.formatter.render_message(snapshot)

        assert "provider says something new" in text

    async def test_marks_go_to_different_amounts(self, harness: NotificationHarness) -> None:
        """Лучший процент и лучшая прибыль обычно у разных сумм."""
        template = harness.snapshot.amounts[0]
        snapshot = self._with_amounts(
            harness,
            self._amount(template, raw=50_000_000, roi="0.40", profit="0.20"),
            self._amount(template, raw=1_000_000_000, roi="0.10", profit="1.00"),
        )

        lines = harness.formatter.render_message(snapshot).splitlines()
        roi_line = next(line for line in lines if "⭐" in line)
        profit_line = next(line for line in lines if "💎" in line)

        assert roi_line.endswith("50 USDT")
        assert profit_line.endswith("1000 USDT")

    async def test_tie_is_marked_on_the_larger_amount(self, harness: NotificationHarness) -> None:
        """Равные значения: отмечается большая сумма, выбор детерминирован."""
        template = harness.snapshot.amounts[0]
        snapshot = self._with_amounts(
            harness,
            self._amount(template, raw=50_000_000, roi="0.20", profit="0.50"),
            self._amount(template, raw=1_000_000_000, roi="0.20", profit="0.50"),
        )

        lines = harness.formatter.render_message(snapshot).splitlines()
        marked = [line for line in lines if "⭐" in line or "💎" in line]

        assert len(marked) == 1
        assert marked[0].endswith("1000 USDT")
