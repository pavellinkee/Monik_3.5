"""Определение границ одного цикла Level 1.

Scope полностью определяется конфигурацией и реестрами
(``02_LEVEL1_SCANNER.md`` §5, §68): списки сетей, токенов, сумм и
провайдеров в коде не зашиты. Изменение конфигурации применяется со
следующего цикла (``02_LEVEL1_SCANNER.md`` §69), поэтому scope
фиксируется на старте.
"""

from __future__ import annotations

from monik.config.root import Configuration
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import ConfigurationError
from monik.domain.models.scan import ScanScope
from monik.domain.models.token import Token
from monik.services.observability.clock import Clock
from monik.services.registries.networks import NetworkRegistry
from monik.services.registries.providers import ProviderRegistry
from monik.services.registries.tokens import TokenRegistry

__all__ = ["ScopeBuilder"]


class ScopeBuilder:
    """Строит :class:`ScanScope` из актуальной конфигурации."""

    def __init__(
        self,
        configuration: Configuration,
        *,
        networks: NetworkRegistry,
        tokens: TokenRegistry,
        providers: ProviderRegistry,
        clock: Clock,
    ) -> None:
        self._configuration = configuration
        self._networks = networks
        self._tokens = tokens
        self._providers = providers
        self._clock = clock

    def build(self) -> ScanScope:
        """Собрать scope цикла.

        Отключённые сети, токены и провайдеры в scope не попадают
        (``02_LEVEL1_SCANNER.md`` §70-72).
        """
        scanner = self._configuration.scanner
        network_id = scanner.base_network
        if not self._networks.is_enabled(network_id):
            raise ConfigurationError(
                f"base network {network_id} is disabled; Level 1 has nothing to scan"
            )

        base_token = self._tokens.base_token
        providers = self.active_providers()
        if not providers:
            raise ConfigurationError(
                f"no provider is available for network {network_id}; Level 1 has no source"
            )

        tokens = self.scan_tokens()
        if not tokens:
            raise ConfigurationError("no enabled intermediate token is available for scanning")

        # Поиск ведётся одной суммой: стоимость этапа не должна расти
        # вместе с числом сумм, которые предстоит проверить Level 2.
        # Остальные суммы подставляются в уже найденную возможность.
        raw_amounts = (base_token.amount_from_decimal(str(scanner.level1_amount)).raw,)
        return ScanScope(
            networks=(network_id,),
            providers=providers,
            tokens=tuple(token.key for token in tokens),
            raw_amounts=raw_amounts,
        )

    def active_providers(self) -> tuple[ProviderId, ...]:
        """Провайдеры, участвующие в цикле прямо сейчас.

        Кроме включённости и заявленной сети учитываются часы работы:
        вне своего окна провайдер не опрашивается вовсе. Запрос не
        отправляется и не отклоняется — его просто не возникает, поэтому
        расписание не отражается ни на статистике отказов, ни на
        состоянии здоровья.
        """
        network_id = self._configuration.scanner.base_network
        now = self._clock.now()
        return tuple(
            provider.provider_id
            for provider in self._providers.active(now)
            if self._providers.declares_network(provider.provider_id, network_id)
        )

    def build_stable(self) -> ScanScope | None:
        """Scope учащённого прохода: только стабильные токены.

        Возвращает ``None``, когда проходу не с чем работать — нет
        стабильных токенов или ни один провайдер в нём не участвует.
        Пустой scope создавать нельзя: цикл без источников и без токенов
        не имеет смысла и только засорял бы историю.

        Набор задаётся меткой ``usd_stable``, а не списком имён: новый
        стабильный токен попадает в проход, как только получит метку.
        """
        scanner = self._configuration.scanner
        network_id = scanner.base_network
        providers = tuple(
            provider_id
            for provider_id in self.active_providers()
            if self._providers.participates_in_fast_scan(provider_id)
        )
        tokens = tuple(token for token in self.scan_tokens() if token.usd_stable)
        if not providers or not tokens:
            return None
        base_token = self._tokens.base_token
        raw_amounts = (base_token.amount_from_decimal(str(scanner.level1_amount)).raw,)
        return ScanScope(
            networks=(network_id,),
            providers=providers,
            tokens=tuple(token.key for token in tokens),
            raw_amounts=raw_amounts,
        )

    def scan_tokens(self) -> tuple[Token, ...]:
        """Промежуточные токены цикла, ограниченные Top-N (§6)."""
        limit = self._configuration.scanner.level1.top_tokens
        return self._tokens.scan_tokens()[:limit]

    @property
    def base_token(self) -> Token:
        """Базовый токен: вход и выход round-trip (``10_LEVEL_1_SCANNER.md`` §37)."""
        return self._tokens.base_token
