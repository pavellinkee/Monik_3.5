"""Политика прибыльности."""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from monik.config.base import ConfigSection
from monik.domain.enums.calculation import ThresholdMetric
from monik.domain.enums.modes import ScanMode
from monik.domain.value_objects.numeric import SignedDecimal

__all__ = ["ProfitabilityConfig"]


class ProfitabilityConfig(ConfigSection):
    """Пороги и правила подтверждения прибыльности.

    Формулы принадлежат Profit Calculator; здесь задаются только параметры
    политики (``17_CONFIGURATION.md`` §36-37). Дублировать пороги внутри
    scanner-модулей запрещено.

    Default порога — 1 % net ROI (``09_PROFIT_CALCULATOR.md`` §24), сравнение
    выполняется как ``>=``, поэтому ровно 1.00 % проходит порог
    (``09_PROFIT_CALCULATOR.md`` §26).
    """

    threshold_metric: ThresholdMetric = ThresholdMetric.NET_ROI
    #: Порог доходности каждого режима сканирования.
    #:
    #: Порог **один на оба уровня**: Level 1 ищет и Level 2 подтверждает
    #: по одной планке. Две отдельные настройки означали бы, что Level 1
    #: находит возможность, которую Level 2 заведомо отвергнет, а работа
    #: обоих уровней тратится впустую; путаница между ними уже дважды
    #: приводила к неверно понятой конфигурации
    #: (``the_main_rules.md``, правило 10).
    #:
    #: Порог принадлежит режиму, а не классу токенов: частый проход по
    #: стейблкоинам судится своей планкой просто потому, что это другой
    #: режим.
    thresholds: dict[ScanMode, SignedDecimal] = Field(
        default_factory=lambda: {ScanMode.UR: Decimal("1.00"), ScanMode.FEST: Decimal("1.00")}
    )
    treat_unknown_cost_as_blocking: bool = True

    def threshold_for(self, mode: ScanMode) -> Decimal:
        """Порог режима. Одна планка и для поиска, и для подтверждения."""
        return self.thresholds[mode]

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Неизвестный расход не может считаться нулевым.

        Отключение этой защиты сделало бы возможным подтверждение на
        недостоверных данных (``CLAUDE.md`` §12, §55).
        """
        if not self.treat_unknown_cost_as_blocking:
            raise ValueError(
                "treat_unknown_cost_as_blocking cannot be disabled: an unknown mandatory "
                "cost must never be treated as zero"
            )
        missing = [mode.value for mode in ScanMode if mode not in self.thresholds]
        if missing:
            raise ValueError(
                f"profitability threshold is not set for scan modes: {', '.join(missing)}"
            )
        return self
