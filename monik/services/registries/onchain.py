"""Метаданные токена, прочитанные у самой сети.

Списки токенов агрегаторов для сверки адресов не годятся: они кураторские
и неполные. Velora, например, торгует токенами, которых в её списке нет, —
проверка по такому списку объявила десять рабочих токенов ошибочными.

Источник истины для вопроса «этот адрес — действительно тот токен?» один:
сам блокчейн. Контракт либо отвечает на стандартные вызовы ERC-20, либо
нет, и его ответ не зависит от чужих решений о том, что включать в
подборку.

Читаются два свойства: число знаков и символ. Число знаков — обязательное
и сравнивается строго: ошибка в нём искажает все суммы. Символ
необязателен и сравнивается мягко: часть контрактов отдаёт его как
``bytes32`` вместо строки, а часть не отдаёт вовсе.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from monik.domain.enums.capability import CapabilityOperation
from monik.domain.enums.resources import RequestPriority
from monik.domain.errors import DataError, MonikError
from monik.domain.models.resource import ResourceKey, ResourceRequest
from monik.domain.value_objects.identifiers import RequestId
from monik.domain.value_objects.identity import NetworkId, TokenAddress
from monik.infrastructure.http import HttpClient, HttpRequest, classify_response
from monik.services.gas.providers import RPC_RESOURCE_OWNER
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields
from monik.services.resources import ResourceManager

__all__ = ["OnchainTokenMetadata", "TokenMetadata"]

_LOGGER = get_logger("services.registries.onchain")

#: Селекторы стандартных методов ERC-20.
_DECIMALS_SELECTOR = "0x313ce567"
_SYMBOL_SELECTOR = "0x95d89b41"

#: Длина ответа, состоящего ровно из одного 32-байтного слова.
_WORD_HEX = 64


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """Что сеть сообщает о контракте по этому адресу."""

    decimals: int
    #: Символ, если контракт его отдал в разбираемом виде.
    symbol: str | None = None


class OnchainTokenMetadata:
    """Читает свойства токена у узла сети."""

    def __init__(
        self,
        *,
        http: HttpClient,
        resources: ResourceManager,
        clock: Clock,
        rpc_urls: dict[str, str],
        timeout_seconds: float = 5.0,
    ) -> None:
        self._http = http
        self._resources = resources
        self._clock = clock
        self._rpc_urls = dict(rpc_urls)
        self._timeout = timedelta(seconds=timeout_seconds)

    def supports(self, network_id: NetworkId) -> bool:
        """Есть ли у сети узел, которому можно задать вопрос."""
        return str(network_id) in self._rpc_urls

    async def metadata(self, network_id: NetworkId, address: TokenAddress) -> TokenMetadata | None:
        """Свойства контракта или ``None``, если это не токен.

        ``None`` означает «по этому адресу нет контракта, отвечающего как
        ERC-20». Сетевой сбой к такому выводу не приводит: он выпускается
        наружу ошибкой, и проверка сама решает, что с ним делать.
        """
        url = self._rpc_urls.get(str(network_id))
        if url is None:
            return None
        raw_decimals = await self._call(url, network_id, address, _DECIMALS_SELECTOR)
        decimals = _parse_decimals(raw_decimals)
        if decimals is None:
            return None
        raw_symbol = await self._call(url, network_id, address, _SYMBOL_SELECTOR)
        return TokenMetadata(decimals=decimals, symbol=_parse_symbol(raw_symbol))

    async def _call(
        self, url: str, network_id: NetworkId, address: TokenAddress, selector: str
    ) -> Any:
        """Выполнить ``eth_call`` через Resource Manager."""
        request_id = RequestId.generate()
        resource_request = ResourceRequest(
            request_id=request_id,
            key=ResourceKey(
                provider_id=RPC_RESOURCE_OWNER,
                network_id=network_id,
                operation=CapabilityOperation.TOKEN_METADATA,
            ),
            priority=RequestPriority.MAINTENANCE,
            timeout=self._timeout,
            created_at=self._clock.now(),
            sequence=0,
            deduplication_key=f"rpc:{network_id}:{address}:{selector}",
        )

        async def call() -> Any:
            response = await self._http.send(
                HttpRequest(
                    method="POST",
                    url=url,
                    json_body={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_call",
                        "params": [{"to": str(address), "data": selector}, "latest"],
                    },
                    request_id=request_id,
                    timeout_seconds=self._timeout.total_seconds(),
                )
            )
            classify_response(response, provider="rpc")
            body = response.json()
            if not isinstance(body, dict):
                raise DataError("rpc response is not a JSON object", code="rpc_response_malformed")
            if body.get("error") is not None:
                # Ответ «вызов не выполнился» — это свойство адреса, а не
                # сбой узла: по такому адресу контракта ERC-20 нет.
                _LOGGER.info(
                    "token contract call reverted",
                    extra=log_fields(token=str(address), selector=selector),
                )
                return None
            return body.get("result")

        try:
            return await self._resources.execute(resource_request, call)
        except MonikError:
            raise


def _parse_decimals(raw: Any) -> int | None:
    """Число знаков из ответа ``decimals()``."""
    if not isinstance(raw, str):
        return None
    value = raw.removeprefix("0x")
    if not value or set(value) == {"0"} and len(value) < _WORD_HEX:
        return None
    try:
        decimals = int(value, 16)
    except ValueError:
        return None
    # Разумный предел: у ERC-20 число знаков не превышает 36.
    return decimals if 0 <= decimals <= 36 else None


def _parse_symbol(raw: Any) -> str | None:
    """Символ из ответа ``symbol()``.

    Стандарт предписывает строку, но часть старых контрактов отдаёт
    ``bytes32``. Разбираются оба вида; неразбираемый ответ даёт ``None`` —
    это не ошибка, символ для сверки необязателен.
    """
    if not isinstance(raw, str):
        return None
    value = raw.removeprefix("0x")
    if not value:
        return None
    if len(value) == _WORD_HEX:
        return _decode_bytes(value) or None
    if len(value) < _WORD_HEX * 3:
        return None
    length = int(value[_WORD_HEX : _WORD_HEX * 2], 16)
    if length == 0 or length > 64:
        return None
    return _decode_bytes(value[_WORD_HEX * 2 : _WORD_HEX * 2 + length * 2]) or None


def _decode_bytes(chunk: str) -> str:
    """Расшифровать шестнадцатеричную строку как текст."""
    try:
        return bytes.fromhex(chunk).rstrip(b"\x00").decode("utf-8", errors="ignore").strip()
    except ValueError:
        return ""
