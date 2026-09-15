"""Provider Registry."""

from __future__ import annotations

from datetime import datetime

from monik.config.root import Configuration
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import ConfigurationError
from monik.domain.models.provider import Provider
from monik.domain.value_objects.identity import NetworkId
from monik.domain.value_objects.schedule import DailyWindow

__all__ = ["ProviderRegistry"]


class ProviderRegistry:
    """Реестр подключённых провайдеров (``08_CAPABILITY_REGISTRY.md`` §22-23).

    Registry отражает то, что **разрешено конфигурацией**. Фактическая
    поддержка операции определяется Capability Registry
    (``17_CONFIGURATION.md`` §66-67), а работоспособность — Health Monitoring.
    """

    def __init__(self, configuration: Configuration) -> None:
        self._providers = {
            provider.provider_id: Provider(
                provider_id=provider.provider_id,
                name=provider.provider_id.value,
                enabled=provider.enabled,
            )
            for provider in configuration.providers
        }
        self._networks = {
            provider.provider_id: set(provider.supported_networks)
            for provider in configuration.providers
        }
        self._pairs = configuration.provider_pairs()
        #: Оформление провайдера для уведомлений: значок и страница
        #: обмена. Свойства конкретного провайдера, поэтому приходят из
        #: его блока конфигурации, а не из формата сообщения.
        self._presentation = {
            provider.provider_id: (provider.emoji, provider.ui_url)
            for provider in configuration.providers
        }
        #: Участвует ли провайдер в учащённом проходе. Ограничение
        #: принадлежит провайдеру: суточную квоту расходует именно он.
        self._fast_scan = {
            provider.provider_id: provider.fast_scan for provider in configuration.providers
        }
        # Часы работы — особенность конкретного провайдера, поэтому она
        # описана у него в конфигурации и превращается здесь в общее
        # понятие «окно». Провайдер без расписания работает круглосуточно.
        self._windows = {
            provider.provider_id: provider.schedule.window(configuration.application.timezone)
            for provider in configuration.providers
            if provider.schedule is not None
        }

    def get(self, provider_id: ProviderId) -> Provider | None:
        """Найти провайдера."""
        return self._providers.get(provider_id)

    def require(self, provider_id: ProviderId) -> Provider:
        """Найти провайдера или сообщить об ошибке конфигурации."""
        provider = self.get(provider_id)
        if provider is None:
            raise ConfigurationError(
                f"provider {provider_id.value} is not configured",
                code="provider_unknown",
            )
        return provider

    def is_enabled(self, provider_id: ProviderId) -> bool:
        """Включён ли провайдер.

        Disabled провайдер не получает запросов
        (``17_CONFIGURATION.md`` §27).
        """
        provider = self.get(provider_id)
        return provider is not None and provider.enabled

    def emoji(self, provider_id: ProviderId) -> str | None:
        """Значок провайдера, если он задан."""
        return self._presentation.get(provider_id, (None, None))[0]

    def ui_url(self, provider_id: ProviderId) -> str | None:
        """Страница обмена провайдера, если она задана."""
        return self._presentation.get(provider_id, (None, None))[1]

    def participates_in_fast_scan(self, provider_id: ProviderId) -> bool:
        """Опрашивается ли провайдер в учащённом проходе."""
        return self._fast_scan.get(provider_id, True)

    def window(self, provider_id: ProviderId) -> DailyWindow | None:
        """Окно работы провайдера, если оно задано."""
        return self._windows.get(provider_id)

    def is_active(self, provider_id: ProviderId, now: datetime) -> bool:
        """Разрешено ли обращаться к провайдеру в этот момент.

        Отличается от :meth:`is_enabled`: выключенный провайдер не
        используется никогда, а вне окна он просто отдыхает и вернётся в
        работу сам. Ни возможности провайдера, ни его состояние здоровья
        от расписания не зависят — это решение оператора, а не сбой.
        """
        if not self.is_enabled(provider_id):
            return False
        window = self._windows.get(provider_id)
        return window is None or window.contains(now)

    def active(self, now: datetime) -> tuple[Provider, ...]:
        """Включённые провайдеры, находящиеся сейчас в своём окне."""
        return tuple(
            provider for provider in self.enabled() if self.is_active(provider.provider_id, now)
        )

    def enabled(self) -> tuple[Provider, ...]:
        """Все включённые провайдеры."""
        return tuple(provider for provider in self._providers.values() if provider.enabled)

    def declares_network(self, provider_id: ProviderId, network_id: NetworkId) -> bool:
        """Объявляет ли провайдер поддержку сети в конфигурации.

        Это не подтверждение поддержки: оно приходит из Capability Registry
        (``06_AGGREGATOR_ADAPTERS.md`` §15).
        """
        return network_id in self._networks.get(provider_id, set())

    def pairs(self) -> tuple[tuple[ProviderId, ProviderId], ...]:
        """Допустимые пары «BUY провайдер — SELL провайдер»."""
        return self._pairs
