"""Конфигурация aggregator providers."""

from __future__ import annotations

from datetime import time
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from monik.config.base import ConfigSection
from monik.config.secrets import SecretRef
from monik.domain.enums.providers import ProviderId
from monik.domain.value_objects.identity import NetworkId
from monik.domain.value_objects.schedule import DailyWindow

__all__ = ["ProviderConfig", "ProviderScheduleConfig"]

#: Время в формате ``HH:MM``. Тот же формат, что у задач планировщика:
#: второй записи времени в конфигурации быть не должно.
_HH_MM = r"^([01]\d|2[0-3]):[0-5]\d$"


class ProviderScheduleConfig(ConfigSection):
    """Часы работы провайдера.

    Время задаётся в формате ``HH:MM``. Окно может пересекать полночь:
    ``start: "22:00"`` с ``end: "04:00"`` означает вечер и ночь.

    Часовой пояс по умолчанию — пояс приложения (``application.timezone``):
    оператор задаёт расписание в том же времени, в каком читает логи.
    """

    start: str = Field(pattern=_HH_MM)
    end: str = Field(pattern=_HH_MM)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.start == self.end:
            raise ValueError("provider schedule start and end must differ")
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise ValueError(f"unknown timezone {self.timezone}") from error
        return self

    def window(self, default_timezone: str) -> DailyWindow:
        """Окно работы; пояс берётся из настройки или у приложения."""
        return DailyWindow(
            start=time.fromisoformat(self.start),
            end=time.fromisoformat(self.end),
            timezone=self.timezone or default_timezone,
        )


class ProviderConfig(ConfigSection):
    """Параметры одного провайдера (``17_CONFIGURATION.md`` §25-27).

    Credentials задаются только ссылкой на environment
    (``17_CONFIGURATION.md`` §26): реальный ключ в repository не попадает.
    Disabled provider не получает запросов (``17_CONFIGURATION.md`` §27).
    """

    provider_id: ProviderId
    enabled: bool = False
    base_url: str | None = Field(default=None, max_length=512)
    api_key: SecretRef | None = None
    supported_networks: tuple[NetworkId, ...] = ()
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    max_concurrent_requests: int = Field(default=4, ge=1, le=128)
    requests_per_second: float = Field(default=5.0, gt=0, le=1000)
    #: Наименьшая пауза между двумя запросами к этому агрегатору. Заменяет
    #: общее значение ``resources.provider_min_interval_seconds`` только
    #: для него: требования у агрегаторов разные, и замедлять остальных
    #: из-за одного нельзя. ``None`` означает «как у всех».
    min_interval_seconds: float | None = Field(default=None, ge=0, le=10)
    allow_same_provider_round_trip: bool = False
    #: Значок агрегатора в уведомлении. Свойство конкретного провайдера,
    #: поэтому описано здесь, а не в общем формате сообщения.
    emoji: str | None = Field(default=None, min_length=1, max_length=8)
    #: Страница агрегатора, на которой оператор подключает кошелёк и
    #: совершает обмен. В уведомлении подставляется вместо названия.
    ui_url: str | None = Field(default=None, max_length=512)
    #: Часы работы провайдера. Вне окна Level 1 его не опрашивает.
    #:
    #: Нужно там, где у провайдера своя дневная квота: расход ограничивают
    #: не только частотой, но и временем работы. Особенность описывается у
    #: самого провайдера, поэтому общая логика цикла о ней не знает.
    #:
    #: ``None`` — круглосуточно.
    schedule: ProviderScheduleConfig | None = None
    #: Участвует ли провайдер в учащённом проходе по стабильным токенам.
    #:
    #: Частый проход стоит сотен запросов в час, и провайдеру с суточной
    #: квотой он её исчерпывает. Ограничение принадлежит провайдеру,
    #: поэтому описано здесь, а не в настройках прохода.
    fast_scan: bool = True
    options: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.base_url is not None and not self.base_url.startswith("https://"):
            raise ValueError("provider base_url must use https")
        if self.ui_url is not None and not self.ui_url.startswith("https://"):
            raise ValueError("provider ui_url must use https")
        if self.enabled and not self.supported_networks:
            raise ValueError(
                f"provider {self.provider_id.value} is enabled but declares no supported networks"
            )
        return self

    def option(self, name: str) -> str | None:
        """Значение provider-specific параметра или ``None``.

        Механизм общий, а смысл конкретного параметра знает только адаптер
        соответствующего агрегатора: core не должен содержать
        aggregator-specific настроек (``CLAUDE.md`` §7).

        Секреты здесь не хранятся: для них существуют ``api_key`` и
        ``SecretRef`` (``17_CONFIGURATION.md`` §26).
        """
        value = self.options.get(name)
        if value is None:
            return None
        return value.strip() or None
