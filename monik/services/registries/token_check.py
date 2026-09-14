"""Разовая проверка адресов токенов при запуске.

Опечатка в адресе контракта ведёт себя незаметно: агрегаторы отвечают
«нет маршрута», память отказов отключает комбинацию, и токен молча
выпадает из поиска. Внешне это не отличается от честного отсутствия
ликвидности, поэтому четыре неверных адреса прожили в конфигурации
незамеченными и обнаружились только ручной сверкой.

Проверка спрашивает у провайдеров, какие адреса они признают, и называет
те, которых не признал **ни один**. Понятие общее: провайдер сообщает
список, если умеет, и логика не знает, кто из них умеет, а кто нет.

Проверка ничего не выключает и не исправляет. Отсутствие токена в списке
провайдера — не доказательство ошибки: список может быть неполным, а сам
токен рабочим. Решение принимает оператор, а Monik лишь показывает
расхождение.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from monik.domain.enums.providers import ProviderId
from monik.domain.errors import MonikError
from monik.domain.models.token import Token
from monik.domain.value_objects.identity import NetworkId, TokenAddress
from monik.infrastructure.providers.contract import AggregatorAdapter
from monik.services.observability.logging import get_logger, log_fields
from monik.services.registries.networks import NetworkRegistry
from monik.services.registries.tokens import TokenRegistry

__all__ = ["TokenAddressCheck", "TokenCheckResult"]

_LOGGER = get_logger("services.registries.token_check")


@dataclass(frozen=True, slots=True)
class TokenCheckResult:
    """Итог сверки адресов с тем, что признают провайдеры."""

    #: Токены, которых не признал ни один ответивший провайдер.
    unrecognised: tuple[Token, ...] = ()
    #: Провайдеры, приславшие список. Пустой набор означает, что сверять
    #: было не с чем и молчание о токенах ничего не доказывает.
    answered: tuple[ProviderId, ...] = ()

    @property
    def conclusive(self) -> bool:
        """Был ли хоть один источник, с которым можно сверяться."""
        return bool(self.answered)


@dataclass
class TokenAddressCheck:
    """Сверяет настроенные адреса токенов со списками провайдеров."""

    adapters: dict[ProviderId, AggregatorAdapter]
    tokens: TokenRegistry
    networks: NetworkRegistry
    _checked: set[str] = field(default_factory=set)

    async def run(self) -> tuple[TokenCheckResult, ...]:
        """Проверить включённые сети и сообщить о расхождениях."""
        results = []
        for network in self.networks.enabled():
            results.append(await self.check(network.network_id))
        return tuple(results)

    async def check(self, network_id: NetworkId) -> TokenCheckResult:
        """Сверить адреса одной сети."""
        known: set[str] = set()
        answered: list[ProviderId] = []
        for provider_id, adapter in sorted(self.adapters.items(), key=lambda item: item[0].value):
            addresses = await self._ask(provider_id, adapter, network_id)
            if addresses is None:
                continue
            answered.append(provider_id)
            known.update(_normalized(address) for address in addresses)

        configured = list(self.tokens.list_enabled(network_id))
        # Если списка не прислал никто, сверять не с чем. Молчание нельзя
        # превращать в обвинение конфигурации: иначе «никто не ответил»
        # выглядело бы как «все адреса ошибочны».
        unrecognised = (
            tuple(token for token in configured if _normalized(token.address) not in known)
            if answered
            else ()
        )
        result = TokenCheckResult(unrecognised=unrecognised, answered=tuple(answered))
        self._report(network_id, configured, result)
        return result

    async def _ask(
        self, provider_id: ProviderId, adapter: AggregatorAdapter, network_id: NetworkId
    ) -> frozenset[TokenAddress] | None:
        """Спросить один список, не позволяя сбою сорвать проверку."""
        try:
            return await adapter.known_tokens(network_id)
        except MonikError as error:
            _LOGGER.info(
                "token list unavailable",
                extra=log_fields(provider=provider_id.value, error_code=error.info.code),
            )
            return None

    def _report(
        self, network_id: NetworkId, configured: list[Token], result: TokenCheckResult
    ) -> None:
        """Записать итог проверки: расхождения важнее тишины."""
        if not result.conclusive:
            _LOGGER.info(
                "token addresses were not verified: no provider published a list",
                extra=log_fields(network=str(network_id), tokens=len(configured)),
            )
            return
        if not result.unrecognised:
            _LOGGER.info(
                "token addresses verified",
                extra=log_fields(
                    network=str(network_id),
                    tokens=len(configured),
                    providers=len(result.answered),
                ),
            )
            return
        _LOGGER.warning(
            "configured tokens are not recognised by any provider",
            extra=log_fields(
                network=str(network_id),
                tokens=", ".join(
                    f"{token.symbol}:{token.address}" for token in result.unrecognised
                ),
                providers=len(result.answered),
            ),
        )


def _normalized(address: TokenAddress) -> str:
    """Адрес в едином виде: регистр записи адреса не различает."""
    return str(address).lower()
