"""Конфигурация Level 1 и Level 2."""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from monik.config.base import ConfigSection
from monik.domain.enums.modes import ScanMode
from monik.domain.enums.scheduler import OverlapPolicy
from monik.domain.value_objects.numeric import PositiveDecimal

__all__ = [
    "Level1Config",
    "Level2Config",
    "NoRouteMemoryConfig",
    "ScannerConfig",
    "ScanModeConfig",
    "ScanModesConfig",
]


class NoRouteMemoryConfig(ConfigSection):
    """Пауза запросов по комбинациям, которые не дают маршрута.

    Отсутствие маршрута не является отсутствием поддержки и решением
    Capability Registry не становится (``06_AGGREGATOR_ADAPTERS.md``
    §75-77): ликвидность может появиться в любой момент, поэтому пауза
    временная и сама истекает.
    """

    enabled: bool = True
    #: Сколько отрицательных ответов подряд означают, что маршрута нет.
    failure_threshold: int = Field(default=3, ge=1, le=100)
    #: Через сколько часов комбинация проверяется снова.
    recheck_after_hours: int = Field(default=24, ge=1, le=8760)


class ScanModeConfig(ConfigSection):
    """Настройки одного режима сканирования.

    Режим — именованный проход Level 1 со своим темпом. Порог режима
    живёт в разделе ``profitability``: политика прибыльности задаётся
    централизованно и не дублируется в модулях сканера
    (``17_CONFIGURATION.md`` §36-37).
    """

    #: Упомянутый в конфигурации режим считается нужным: чтобы выключить
    #: его, ``enabled`` пишется явно. Иначе правка одного только интервала
    #: молча гасила бы режим.
    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=5, le=86_400)


class ScanModesConfig(ConfigSection):
    """Все режимы сканирования.

    Набор режимов закрыт и задан :class:`ScanMode`: добавление режима —
    это изменение кода, а не конфигурации, потому что у каждого режима
    своя логика отбора токенов.
    """

    #: Основной проход: весь набор токенов, все включённые агрегаторы.
    ur: ScanModeConfig = ScanModeConfig(interval_seconds=300)
    #: Частый проход по стабильным токенам. Стоимость круга между ними
    #: почти нулевая, поэтому прибыльным становится любое заметное
    #: отклонение от паритета — но живёт оно минуты, и медленный проход
    #: его не застаёт. Набор определяется меткой ``usd_stable``, а не
    #: списком имён.
    fest: ScanModeConfig = ScanModeConfig(enabled=False, interval_seconds=30)

    def for_mode(self, mode: ScanMode) -> ScanModeConfig:
        """Настройки режима."""
        return self.ur if mode is ScanMode.UR else self.fest

    def enabled_modes(self) -> tuple[ScanMode, ...]:
        """Режимы, включённые оператором, в порядке объявления."""
        return tuple(mode for mode in ScanMode if self.for_mode(mode).enabled)


class Level1Config(ConfigSection):
    """Технические параметры Level 1 (``17_CONFIGURATION.md`` §32-34).

    Темп, планка и состав каждого прохода задаются его режимом
    (:class:`ScanModesConfig`). Здесь остаётся то, что одинаково для всех
    режимов: пределы параллельности, сроки жизни, память отказов. При
    наложении запусков применяется ``SKIP`` (``02_LEVEL1_SCANNER.md`` §65).
    """

    #: Сумма, которой Level 1 ищет возможности. Она одна: поиск ведётся
    #: одной суммой, а проверка Level 2 подставляет остальные в уже
    #: найденную возможность. Так число запросов к агрегаторам на этапе
    #: поиска не зависит от того, сколько сумм проверяется.
    #:
    #: Если не задана, берётся наименьшая из ``scanner.amounts``.
    amount: PositiveDecimal | None = None
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP
    no_route_memory: NoRouteMemoryConfig = NoRouteMemoryConfig()
    scan_timeout_seconds: int = Field(default=240, ge=1, le=86_400)
    top_tokens: int = Field(default=30, ge=1, le=500)
    max_opportunities_per_scan: int = Field(default=50, ge=1, le=1000)
    max_concurrent_requests: int = Field(default=8, ge=1, le=256)
    quote_max_age_seconds: int = Field(default=30, ge=1, le=3600)
    opportunity_ttl_seconds: int = Field(default=120, ge=1, le=3600)
    deduplication_window_seconds: int = Field(default=300, ge=0, le=86_400)


class Level2Config(ConfigSection):
    """Параметры Level 2 (``17_CONFIGURATION.md`` §35).

    ``max_parallel`` по умолчанию 20 (``CLAUDE.md`` §18,
    ``04_SCHEDULER.md`` §21) и никогда не превышается.
    """

    enabled: bool = True
    max_parallel: int = Field(default=20, ge=1, le=200)
    queue_capacity: int = Field(default=200, ge=1, le=10_000)
    job_ttl_seconds: int = Field(default=120, ge=1, le=3600)
    confirmation_timeout_seconds: int = Field(default=60, ge=1, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    quote_max_age_seconds: int = Field(default=15, ge=1, le=3600)
    require_route_confirmation: bool = True

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Проверка маршрута обязательна.

        Без подтверждения маршрута Level 2 подтверждал бы возможность на
        маршруте, отличном от найденного Level 1
        (``11_LEVEL_2_SCANNER.md`` §18, §24).
        """
        if not self.require_route_confirmation:
            raise ValueError(
                "require_route_confirmation cannot be disabled: Level 2 must verify the "
                "exact route fixed by Level 1"
            )
        if self.confirmation_timeout_seconds > self.job_ttl_seconds:
            raise ValueError("confirmation_timeout_seconds must not exceed job_ttl_seconds")
        return self


class ScannerConfig(ConfigSection):
    """Общие параметры сканирования (``17_CONFIGURATION.md`` §21-22).

    Суммы задаются только конфигурацией: hard-code сумм в коде запрещён
    (``01_PROJECT_REQUIREMENTS.md`` §22).

    Этапы используют суммы по-разному. Level 1 ищет возможности **одной**
    суммой ``level1.amount``: поиск обходится тем же числом запросов
    независимо от того, сколько сумм предстоит проверить. Level 2
    проверяет найденную возможность всеми суммами :attr:`amounts`,
    подставляя каждую в уже зафиксированный маршрут. Количество сумм
    произвольно и задаётся оператором.
    """

    #: Сканируемые сети и базовый токен каждой из них задаются в разделе
    #: ``networks``: круг всегда замыкается внутри одной сети, а базовый
    #: токен — её свойство (``17_CONFIGURATION.md`` §24). Отдельной
    #: настройки «базовая сеть» у сканера нет: сканируются все включённые
    #: сети, и сеть выключается собственным флагом ``enabled``
    #: (``02_LEVEL1_SCANNER.md`` §72).
    #:
    #: Суммы, которыми Level 2 проверяет найденную возможность.
    amounts: tuple[PositiveDecimal, ...] = Field(min_length=1)
    #: Режимы сканирования: темп, планка и состав каждого прохода.
    modes: ScanModesConfig = ScanModesConfig()
    level1: Level1Config = Level1Config()
    level2: Level2Config = Level2Config()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if len(set(self.amounts)) != len(self.amounts):
            raise ValueError("scanner amounts must be unique")
        if any(amount <= Decimal(0) for amount in self.amounts):
            raise ValueError("scanner amounts must be positive")
        if not self.modes.enabled_modes():
            raise ValueError(
                "at least one scan mode must be enabled: Level 1 would never run otherwise"
            )
        # Таймаут цикла проверяется по самому частому включённому режиму:
        # иначе его проходы накладывались бы по построению.
        fastest = min(
            self.modes.for_mode(mode).interval_seconds for mode in self.modes.enabled_modes()
        )
        if self.level1.scan_timeout_seconds > fastest:
            raise ValueError(
                f"scan_timeout_seconds ({self.level1.scan_timeout_seconds}) must not exceed the "
                f"shortest enabled mode interval ({fastest}), otherwise scans would overlap"
            )
        return self

    @property
    def level1_amount(self) -> PositiveDecimal:
        """Сумма поиска Level 1.

        Явно заданная либо наименьшая из проверяемых: искать возможность
        суммой большей, чем самая маленькая проверяемая, незачем.
        """
        if self.level1.amount is not None:
            return self.level1.amount
        return min(self.amounts)
