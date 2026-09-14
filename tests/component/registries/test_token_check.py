"""Сверка адресов токенов с сетью.

Опечатка в адресе ведёт себя как отсутствие ликвидности: агрегаторы
отвечают «нет маршрута», память отказов отключает комбинацию, и токен
молча выпадает из поиска. Четыре таких адреса прожили в конфигурации
незамеченными, пока их не нашли ручной сверкой.

Источник проверки — сама сеть. Списки токенов агрегаторов для этого не
годятся: они кураторские, и сверка по ним объявила ошибочными десять
токенов, которыми провайдер спокойно торгует.
"""

from __future__ import annotations

from monik.config import parse_configuration
from monik.domain.errors import ProviderError
from monik.domain.value_objects.identity import NetworkId, TokenAddress
from monik.services.registries import (
    NetworkRegistry,
    TokenAddressCheck,
    TokenMetadata,
    TokenRegistry,
)
from tests.component.level1.conftest import level1_document
from tests.unit.config.conftest import AAVE_ADDRESS, USDT_ADDRESS, VALID_ENV

POLYGON = NetworkId("polygon")

#: Ответы сети, совпадающие с конфигурацией: от них тест и отклоняется.
_MATCHING = {
    USDT_ADDRESS.lower(): TokenMetadata(decimals=6, symbol="USDT"),
    AAVE_ADDRESS.lower(): TokenMetadata(decimals=18, symbol="AAVE"),
}


class _Chain:
    """Узел сети с заданными ответами."""

    def __init__(
        self,
        answers: dict[str, TokenMetadata | None] | None = None,
        *,
        supported: bool = True,
        error: Exception | None = None,
    ) -> None:
        self._answers = answers or {}
        self._supported = supported
        self._error = error
        self.asked: list[str] = []

    def supports(self, network_id: NetworkId) -> bool:
        return self._supported

    async def metadata(
        self, network_id: NetworkId, address: TokenAddress
    ) -> TokenMetadata | None:
        self.asked.append(str(address).lower())
        if self._error is not None:
            raise self._error
        return self._answers.get(str(address).lower(), _MATCHING[str(address).lower()])


def _check(chain: _Chain) -> TokenAddressCheck:
    configuration = parse_configuration(level1_document(), environ=dict(VALID_ENV)).config
    return TokenAddressCheck(
        metadata=chain,  # type: ignore[arg-type]
        tokens=TokenRegistry(configuration),
        networks=NetworkRegistry(configuration),
    )


class TestMismatch:
    async def test_address_without_a_contract_is_reported(self) -> None:
        """Ровно так выглядела опечатка в адресе: контракта нет."""
        chain = _Chain({AAVE_ADDRESS.lower(): None})

        result = await _check(chain).check(POLYGON)

        assert [item.token.symbol for item in result.mismatches] == ["AAVE"]
        assert "нет контракта" in result.mismatches[0].reason

    async def test_wrong_decimals_are_reported(self) -> None:
        """Ошибка в числе знаков искажает все суммы, поэтому сверяется строго."""
        chain = _Chain({AAVE_ADDRESS.lower(): TokenMetadata(decimals=6, symbol="AAVE")})

        result = await _check(chain).check(POLYGON)

        assert [item.token.symbol for item in result.mismatches] == ["AAVE"]
        assert "число знаков" in result.mismatches[0].reason

    async def test_wrong_symbol_is_reported_but_not_critical(self) -> None:
        """Символ — вопрос отображения: идентичность задаёт адрес.

        USDT на Polygon отвечает символом ``USDT0`` после переименования
        Tether, оставаясь тем же контрактом. Поднимать на это
        предупреждение при каждом запуске значит приучить его не читать.
        """
        chain = _Chain({AAVE_ADDRESS.lower(): TokenMetadata(decimals=18, symbol="WBTC")})

        result = await _check(chain).check(POLYGON)

        assert "символ контракта WBTC" in result.mismatches[0].reason
        assert not result.mismatches[0].critical

    async def test_missing_contract_is_critical(self) -> None:
        chain = _Chain({AAVE_ADDRESS.lower(): None})

        result = await _check(chain).check(POLYGON)

        assert result.mismatches[0].critical

    async def test_matching_token_is_silent(self) -> None:
        chain = _Chain({AAVE_ADDRESS.lower(): TokenMetadata(decimals=18, symbol="AAVE")})

        result = await _check(chain).check(POLYGON)

        assert result.mismatches == ()
        assert result.checked > 0

    async def test_symbol_case_does_not_matter(self) -> None:
        chain = _Chain({AAVE_ADDRESS.lower(): TokenMetadata(decimals=18, symbol="aave")})

        assert (await _check(chain).check(POLYGON)).mismatches == ()

    async def test_missing_symbol_is_not_a_mismatch(self) -> None:
        """Часть контрактов символ не отдаёт: это не повод их подозревать."""
        chain = _Chain({AAVE_ADDRESS.lower(): TokenMetadata(decimals=18, symbol=None)})

        assert (await _check(chain).check(POLYGON)).mismatches == ()


class TestInconclusive:
    """Неизвестное не превращается в обвинение конфигурации."""

    async def test_network_without_rpc_is_not_checked(self) -> None:
        chain = _Chain(supported=False)

        result = await _check(chain).check(POLYGON)

        assert result.mismatches == ()
        assert result.checked == 0
        assert result.unverified
        assert chain.asked == []

    async def test_network_failure_does_not_accuse_the_configuration(self) -> None:
        chain = _Chain(error=ProviderError("rpc unavailable", code="provider_unavailable"))

        result = await _check(chain).check(POLYGON)

        assert result.mismatches == ()
        assert len(result.unverified) == result.checked + len(result.unverified)


class TestParsing:
    """Разбор ответов узла: контракты отвечают по-разному."""

    def test_symbol_as_a_string(self) -> None:
        from monik.services.registries.onchain import _parse_symbol

        # Стандартный ответ: смещение, длина, данные.
        payload = (
            "0x"
            + "20".rjust(64, "0")
            + "4".rjust(64, "0")
            + b"AAVE".hex().ljust(64, "0")
        )
        assert _parse_symbol(payload) == "AAVE"

    def test_symbol_as_bytes32(self) -> None:
        """Часть старых контрактов отдаёт символ как ``bytes32``."""
        from monik.services.registries.onchain import _parse_symbol

        assert _parse_symbol("0x" + b"MKR".hex().ljust(64, "0")) == "MKR"

    def test_unreadable_symbol_is_not_an_error(self) -> None:
        from monik.services.registries.onchain import _parse_symbol

        assert _parse_symbol("0x") is None
        assert _parse_symbol(None) is None

    def test_decimals_are_parsed(self) -> None:
        from monik.services.registries.onchain import _parse_decimals

        assert _parse_decimals("0x" + "6".rjust(64, "0")) == 6
        assert _parse_decimals("0x" + "12".rjust(64, "0")) == 18

    def test_empty_answer_means_no_contract(self) -> None:
        """Пустой ответ ``eth_call`` — это отсутствие контракта."""
        from monik.services.registries.onchain import _parse_decimals

        assert _parse_decimals("0x") is None
        assert _parse_decimals(None) is None

    def test_absurd_decimals_are_rejected(self) -> None:
        """Ответ не от ERC-20 не должен выглядеть как число знаков."""
        from monik.services.registries.onchain import _parse_decimals

        assert _parse_decimals("0x" + "f" * 64) is None
