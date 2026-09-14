"""Сверка адресов токенов со списками провайдеров.

Опечатка в адресе ведёт себя как отсутствие ликвидности: агрегаторы
отвечают «нет маршрута», память отказов отключает комбинацию, и токен
молча выпадает из поиска. Четыре таких адреса прожили в конфигурации
незамеченными, пока их не нашли ручной сверкой.
"""

from __future__ import annotations

from monik.config import parse_configuration
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import ProviderError
from monik.domain.value_objects.identity import NetworkId, TokenAddress
from monik.services.registries import (
    NetworkRegistry,
    TokenAddressCheck,
    TokenRegistry,
)
from tests.component.level1.conftest import level1_document
from tests.unit.config.conftest import AAVE_ADDRESS, USDT_ADDRESS, VALID_ENV

POLYGON = NetworkId("polygon")


class _ListingAdapter:
    """Провайдер, публикующий список признаваемых токенов."""

    def __init__(self, addresses: tuple[str, ...]) -> None:
        self._addresses = addresses
        self.calls = 0

    async def known_tokens(self, network_id: NetworkId) -> frozenset[TokenAddress] | None:
        self.calls += 1
        return frozenset(TokenAddress(address) for address in self._addresses)


class _SilentAdapter:
    """Провайдер без такого endpoint'а."""

    async def known_tokens(self, network_id: NetworkId) -> frozenset[TokenAddress] | None:
        return None


class _BrokenAdapter:
    """Провайдер, у которого список недоступен."""

    async def known_tokens(self, network_id: NetworkId) -> frozenset[TokenAddress] | None:
        raise ProviderError("token list unavailable", code="provider_unavailable")


def _check(**adapters: object) -> TokenAddressCheck:
    configuration = parse_configuration(level1_document(), environ=dict(VALID_ENV)).config
    return TokenAddressCheck(
        adapters={ProviderId(name): adapter for name, adapter in adapters.items()},  # type: ignore[misc]
        tokens=TokenRegistry(configuration),
        networks=NetworkRegistry(configuration),
    )


class TestRecognition:
    async def test_unknown_address_is_named(self) -> None:
        """Адрес, которого не знает ни один провайдер, попадает в отчёт."""
        check = _check(velora=_ListingAdapter((USDT_ADDRESS,)))

        result = await check.check(POLYGON)

        assert [token.symbol for token in result.unrecognised] == ["AAVE"]
        assert result.conclusive

    async def test_case_of_the_address_does_not_matter(self) -> None:
        """Регистр записи адреса его не различает."""
        check = _check(velora=_ListingAdapter((USDT_ADDRESS.lower(), AAVE_ADDRESS.upper())))

        assert (await check.check(POLYGON)).unrecognised == ()

    async def test_one_provider_is_enough_to_recognise(self) -> None:
        """Токен считается известным, если его знает хотя бы один."""
        check = _check(
            velora=_ListingAdapter((USDT_ADDRESS,)),
            zero_x=_ListingAdapter((AAVE_ADDRESS,)),
        )

        assert (await check.check(POLYGON)).unrecognised == ()


class TestInconclusive:
    """Молчание провайдеров не превращается в обвинение конфигурации."""

    async def test_without_any_list_nothing_is_reported(self) -> None:
        check = _check(velora=_SilentAdapter())

        result = await check.check(POLYGON)

        assert result.unrecognised == ()
        assert not result.conclusive

    async def test_failed_request_does_not_break_the_check(self) -> None:
        """Сбой одного провайдера не срывает проверку и не роняет старт."""
        check = _check(velora=_BrokenAdapter(), zero_x=_ListingAdapter((USDT_ADDRESS,)))

        result = await check.check(POLYGON)

        assert [token.symbol for token in result.unrecognised] == ["AAVE"]
        assert result.answered == (ProviderId.ZERO_X,)
