"""Разовая проверка адресов токенов при запуске.

Опечатка в адресе контракта ведёт себя незаметно: агрегаторы отвечают
«нет маршрута», память отказов отключает комбинацию, и токен молча
выпадает из поиска. Внешне это неотличимо от честного отсутствия
ликвидности, поэтому четыре неверных адреса прожили в конфигурации
незамеченными и нашлись только ручной сверкой.

Источник истины — сама сеть, а не списки агрегаторов. Списки кураторские
и неполные: сверка по ним объявила ошибочными десять токенов, которыми
провайдер спокойно торгует. Контракт же либо отвечает на стандартные
вызовы ERC-20, либо нет, и его ответ ни от чьих подборок не зависит.

Проверка ничего не выключает и не исправляет: она только показывает
расхождение. Решение принимает оператор.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from monik.domain.errors import MonikError
from monik.domain.models.token import Token
from monik.domain.value_objects.identity import NetworkId
from monik.services.observability.logging import get_logger, log_fields
from monik.services.registries.networks import NetworkRegistry
from monik.services.registries.onchain import OnchainTokenMetadata
from monik.services.registries.tokens import TokenRegistry

__all__ = ["TokenAddressCheck", "TokenCheckResult", "TokenMismatch"]

_LOGGER = get_logger("services.registries.token_check")


@dataclass(frozen=True, slots=True)
class TokenMismatch:
    """Расхождение между настройкой и тем, что отвечает сеть.

    ``critical`` отделяет ошибку от косметики. Отсутствие контракта и
    неверное число знаков делают токен непригодным: суммы считаются не по
    тому активу или не в том масштабе. Разошедшийся символ — вопрос
    отображения: идентичность токена задаёт адрес, а не имя
    (``36_DATA_MODELS.md`` §10). Например, USDT на Polygon отвечает
    символом ``USDT0`` после переименования Tether, оставаясь тем же
    контрактом.
    """

    token: Token
    reason: str
    critical: bool = True

    def describe(self) -> str:
        """Строка для журнала."""
        return f"{self.token.symbol}:{self.token.address} — {self.reason}"


@dataclass(frozen=True, slots=True)
class TokenCheckResult:
    """Итог сверки адресов одной сети."""

    network_id: NetworkId
    checked: int = 0
    mismatches: tuple[TokenMismatch, ...] = ()
    #: Токены, о которых узел ничего не сказал из-за сбоя связи. Это не
    #: обвинение конфигурации: неизвестное не равно ошибочному.
    unverified: tuple[Token, ...] = ()


@dataclass
class TokenAddressCheck:
    """Сверяет настроенные адреса с тем, что отвечает сеть."""

    metadata: OnchainTokenMetadata
    tokens: TokenRegistry
    networks: NetworkRegistry
    _reported: set[str] = field(default_factory=set)

    async def run(self) -> tuple[TokenCheckResult, ...]:
        """Проверить включённые сети и сообщить о расхождениях."""
        return tuple([await self.check(network.network_id) for network in self.networks.enabled()])

    async def check(self, network_id: NetworkId) -> TokenCheckResult:
        """Сверить адреса одной сети."""
        configured = list(self.tokens.list_enabled(network_id))
        if not self.metadata.supports(network_id):
            _LOGGER.info(
                "token addresses were not verified: network has no rpc endpoint",
                extra=log_fields(network=str(network_id), tokens=len(configured)),
            )
            return TokenCheckResult(network_id=network_id, unverified=tuple(configured))

        mismatches: list[TokenMismatch] = []
        unverified: list[Token] = []
        for token in configured:
            mismatch = await self._verify(network_id, token, unverified)
            if mismatch is not None:
                mismatches.append(mismatch)
        result = TokenCheckResult(
            network_id=network_id,
            checked=len(configured) - len(unverified),
            mismatches=tuple(mismatches),
            unverified=tuple(unverified),
        )
        self._report(result)
        return result

    async def _verify(
        self, network_id: NetworkId, token: Token, unverified: list[Token]
    ) -> TokenMismatch | None:
        """Сверить один токен, не позволяя сбою сорвать проверку."""
        try:
            found = await self.metadata.metadata(network_id, token.address)
        except MonikError as error:
            unverified.append(token)
            _LOGGER.info(
                "token address could not be verified",
                extra=log_fields(token=str(token.address), error_code=error.info.code),
            )
            return None
        if found is None:
            return TokenMismatch(token=token, reason="по адресу нет контракта ERC-20")
        if found.decimals != token.decimals:
            return TokenMismatch(
                token=token,
                reason=f"число знаков {found.decimals}, в настройке {token.decimals}",
            )
        if found.symbol and found.symbol.upper() != str(token.symbol).upper():
            return TokenMismatch(
                token=token,
                reason=f"символ контракта {found.symbol}, в настройке {token.symbol}",
                critical=False,
            )
        return None

    def _report(self, result: TokenCheckResult) -> None:
        """Записать итог: расхождения важнее тишины.

        Предупреждение поднимается только на то, что делает токен
        непригодным. Иначе постоянное предупреждение о переименованном
        символе приучило бы не читать предупреждения вовсе.
        """
        critical = [item for item in result.mismatches if item.critical]
        cosmetic = [item for item in result.mismatches if not item.critical]
        if cosmetic:
            _LOGGER.info(
                "token symbols differ from the chain",
                extra=log_fields(
                    network=str(result.network_id),
                    tokens="; ".join(item.describe() for item in cosmetic),
                ),
            )
        if critical:
            _LOGGER.warning(
                "configured token addresses do not match the chain",
                extra=log_fields(
                    network=str(result.network_id),
                    checked=result.checked,
                    tokens="; ".join(item.describe() for item in critical),
                ),
            )
            return
        _LOGGER.info(
            "token addresses verified against the chain",
            extra=log_fields(
                network=str(result.network_id),
                checked=result.checked,
                unverified=len(result.unverified),
            ),
        )
