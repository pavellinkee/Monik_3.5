"""Конфигурация Level 1 и Level 2."""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from monik.config.base import ConfigSection
from monik.domain.enums.scheduler import OverlapPolicy
from monik.domain.value_objects.numeric import PositiveDecimal

__all__ = [
    "Level1Config",
    "Level2Config",
    "NoRouteMemoryConfig",
    "ScannerConfig",
    "StableScanConfig",
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


class StableScanConfig(ConfigSection):
    """Учащённый проход по стабильным токенам.

    Стоимость круга между стабильными токенами почти нулевая — по
    измерению 0,0013 % против 0,065 % у WETH, — поэтому прибыльным
    становится любое заметное отклонение от паритета. Но живёт такое
    отклонение минуты, и десятиминутный цикл его не застаёт.

    Поэтому стабильные токены опрашиваются отдельным, частым проходом.
    Набор определяется меткой ``usd_stable`` у токена, а не списком имён:
    новый стабильный токен попадает в проход, как только получит метку.
    """

    enabled: bool = False
    interval_seconds: int = Field(default=30, ge=5, le=3_600)


class Level1Config(ConfigSection):
    """Параметры Level 1 (``17_CONFIGURATION.md`` §32-34).

    Интервал сканирования по умолчанию — 5 минут
    (``02_LEVEL1_SCANNER.md`` §64). При наложении запусков применяется
    ``SKIP`` (``02_LEVEL1_SCANNER.md`` §65).
    """

    enabled: bool = True
    #: Сумма, которой Level 1 ищет возможности. Она одна: поиск ведётся
    #: одной суммой, а проверка Level 2 подставляет остальные в уже
    #: найденную возможность. Так число запросов к агрегаторам на этапе
    #: поиска не зависит от того, сколько сумм проверяется.
    #:
    #: Если не задана, берётся наименьшая из ``scanner.amounts``.
    amount: PositiveDecimal | None = None
    interval_seconds: int = Field(default=300, ge=1, le=86_400)
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP
    no_route_memory: NoRouteMemoryConfig = NoRouteMemoryConfig()
    scan_timeout_seconds: int = Field(default=240, ge=1, le=86_400)
    top_tokens: int = Field(default=30, ge=1, le=500)
    #: Отдельный частый проход по стабильным токенам.
    stable_scan: StableScanConfig = StableScanConfig()
    max_opportunities_per_scan: int = Field(default=50, ge=1, le=1000)
    max_concurrent_requests: int = Field(default=8, ge=1, le=256)
    quote_max_age_seconds: int = Field(default=30, ge=1, le=3600)
    opportunity_ttl_seconds: int = Field(default=120, ge=1, le=3600)
    deduplication_window_seconds: int = Field(default=300, ge=0, le=86_400)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.scan_timeout_seconds > self.interval_seconds:
            raise ValueError(
                "scan_timeout_seconds must not exceed interval_seconds, "
                "otherwise scans would overlap by design"
            )
        return self


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
    level1: Level1Config = Level1Config()
    level2: Level2Config = Level2Config()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if len(set(self.amounts)) != len(self.amounts):
            raise ValueError("scanner amounts must be unique")
        if any(amount <= Decimal(0) for amount in self.amounts):
            raise ValueError("scanner amounts must be positive")
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
