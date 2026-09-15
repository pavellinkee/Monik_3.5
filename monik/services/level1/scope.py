"""Определение границ одного цикла Level 1.

Scope полностью определяется конфигурацией и реестрами
(``02_LEVEL1_SCANNER.md`` §5, §68): списки сетей, токенов, сумм и
провайдеров в коде не зашиты. Изменение конфигурации применяется со
следующего цикла (``02_LEVEL1_SCANNER.md`` §69), поэтому scope
фиксируется на старте.

Цикл всегда принадлежит **одной** сети: BUY и SELL одной возможности
обязаны относиться к одной сети, а межсетевой арбитраж в текущий
workflow не входит (``10_LEVEL_1_SCANNER.md`` §38). Поэтому каждая
включённая сеть получает собственный scope, а не общий на всех: так
комбинация из разных сетей не может возникнуть даже случайно.
"""

from __future__ import annotations

from monik.config.root import Configuration
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import ConfigurationError
from monik.domain.models.scan import ScanScope
from monik.domain.models.token import Token
from monik.domain.value_objects.identity import NetworkId
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

    def scan_networks(self) -> tuple[NetworkId, ...]:
        """Сети, которые сканируются в этом такте.

        Выключенная сеть не участвует в scan (``02_LEVEL1_SCANNER.md``
        §72), поэтому оператору достаточно снять ``enabled`` у сети —
        отдельного списка сканируемых сетей не существует и рассогласовать
        его не с чем.
        """
        return tuple(network.network_id for network in self._networks.enabled())

    def build(self, network_id: NetworkId) -> ScanScope:
        """Собрать scope цикла одной сети.

        Отключённые сети, токены и провайдеры в scope не попадают
        (``02_LEVEL1_SCANNER.md`` §70-72).
        """
        if not self._networks.is_enabled(network_id):
            raise ConfigurationError(
                f"network {network_id} is disabled; Level 1 has nothing to scan there"
            )

        base_token = self._tokens.base_token(network_id)
        providers = self.active_providers(network_id)
        if not providers:
            raise ConfigurationError(
                f"no provider is available for network {network_id}; Level 1 has no source"
            )

        tokens = self.scan_tokens(network_id)
        if not tokens:
            raise ConfigurationError(
                f"no enabled intermediate token is available on network {network_id}"
            )

        return ScanScope(
            networks=(network_id,),
            providers=providers,
            tokens=tuple(token.key for token in tokens),
            raw_amounts=self._raw_amounts(base_token),
        )

    def active_providers(self, network_id: NetworkId) -> tuple[ProviderId, ...]:
        """Провайдеры, работающие с этой сетью прямо сейчас.

        Кроме включённости и заявленной сети учитываются часы работы:
        вне своего окна провайдер не опрашивается вовсе. Запрос не
        отправляется и не отклоняется — его просто не возникает, поэтому
        расписание не отражается ни на статистике отказов, ни на
        состоянии здоровья.
        """
        now = self._clock.now()
        return tuple(
            provider.provider_id
            for provider in self._providers.active(now)
            if self._providers.declares_network(provider.provider_id, network_id)
        )

    def build_stable(self, network_id: NetworkId) -> ScanScope | None:
        """Scope учащённого прохода одной сети: только стабильные токены.

        Возвращает ``None``, когда проходу не с чем работать — нет
        стабильных токенов или ни один провайдер в нём не участвует.
        Пустой scope создавать нельзя: цикл без источников и без токенов
        не имеет смысла и только засорял бы историю.

        Набор задаётся меткой ``usd_stable``, а не списком имён: новый
        стабильный токен попадает в проход, как только получит метку.
        """
        if not self._networks.is_enabled(network_id):
            return None
        providers = tuple(
            provider_id
            for provider_id in self.active_providers(network_id)
            if self._providers.participates_in_fast_scan(provider_id)
        )
        tokens = tuple(token for token in self.scan_tokens(network_id) if token.usd_stable)
        if not providers or not tokens:
            return None
        return ScanScope(
            networks=(network_id,),
            providers=providers,
            tokens=tuple(token.key for token in tokens),
            raw_amounts=self._raw_amounts(self._tokens.base_token(network_id)),
        )

    def scan_tokens(self, network_id: NetworkId) -> tuple[Token, ...]:
        """Промежуточные токены сети, ограниченные Top-N (§6)."""
        return self._tokens.scan_tokens(network_id)

    def base_token(self, network_id: NetworkId) -> Token:
        """Базовый токен сети: вход и выход round-trip (``10_LEVEL_1_SCANNER.md`` §37)."""
        return self._tokens.base_token(network_id)

    def _raw_amounts(self, base_token: Token) -> tuple[int, ...]:
        """Сумма поиска в base units базового токена сети.

        Поиск ведётся одной суммой: стоимость этапа не должна расти
        вместе с числом сумм, которые предстоит проверить Level 2
        (``the_main_rules.md``, правило 1). Пересчёт делается для каждой
        сети отдельно — знаки базового токена у сетей могут различаться.
        """
        amount = self._configuration.scanner.level1_amount
        return (base_token.amount_from_decimal(str(amount)).raw,)
