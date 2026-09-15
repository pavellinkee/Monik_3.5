"""Конфигурация сетей."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from monik.config.base import ConfigSection
from monik.domain.value_objects.identity import NetworkId, TokenAddress, TokenSymbol

__all__ = ["NetworkConfig"]


class NetworkConfig(ConfigSection):
    """Параметры одной сети.

    Сеть не зашита в scanner logic и приходит отсюда
    (``01_PROJECT_REQUIREMENTS.md`` §6). Конфигурация одной сети не
    применяется к другой (``17_CONFIGURATION.md`` §24).
    """

    network_id: NetworkId
    name: str = Field(min_length=1, max_length=64)
    chain_id: int = Field(gt=0)
    native_token_symbol: TokenSymbol
    native_token_decimals: int = Field(default=18, ge=0, le=36)
    wrapped_native_address: TokenAddress
    #: Базовый токен round-trip этой сети: вход и выход цикла.
    #:
    #: Принадлежит сети, а не сканеру: адрес токена network-specific
    #: (``01_PROJECT_REQUIREMENTS.md`` §10), а конфигурация одной сети не
    #: применяется к другой (``17_CONFIGURATION.md`` §24). Единая
    #: настройка на весь сканер означала бы, что вторая сеть считает
    #: круг от чужого контракта.
    base_token_address: TokenAddress
    rpc_url: str | None = Field(default=None, max_length=512)
    #: Значок сети в уведомлении. Свойство сети, а не формата сообщения.
    emoji: str | None = Field(default=None, min_length=1, max_length=8)
    enabled: bool = True

    @model_validator(mode="after")
    def _validate_rpc(self) -> Self:
        """RPC endpoint обязан использовать HTTPS (``32_SECURITY.md``)."""
        if self.rpc_url is not None and not self.rpc_url.startswith("https://"):
            raise ValueError("rpc_url must use https")
        return self
