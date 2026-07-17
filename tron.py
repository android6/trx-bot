"""Работа с TRON: валидация адресов и проверка аккаунта через TronGrid/Tronscan."""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx

import config

logger = logging.getLogger(__name__)

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_USDT_DECIMALS = 6


class TronApiError(Exception):
    """Не удалось получить данные ни из TronGrid, ни из Tronscan."""


@dataclass(frozen=True)
class AccountInfo:
    address: str
    activated: bool
    usdt_balance: Decimal
    source: str  # "TronGrid" или "Tronscan"
    trx_balance: Decimal = Decimal(0)


def decode_base58check(address: str) -> bytes | None:
    """Декодирует TRON-адрес; 21-байтный payload или None, если адрес невалиден."""
    if len(address) != 34 or not address.startswith("T"):
        return None
    num = 0
    for char in address:
        digit = _BASE58_ALPHABET.find(char)
        if digit < 0:
            return None
        num = num * 58 + digit
    try:
        raw = num.to_bytes(25, "big")
    except OverflowError:
        return None
    payload, checksum = raw[:21], raw[21:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        return None
    if payload[0] != 0x41:
        return None
    return payload


def is_valid_address(address: str) -> bool:
    return decode_base58check(address) is not None


class TronChecker:
    """Проверяет активацию аккаунта и баланс USDT. Основной API — TronGrid,
    при его недоступности — Tronscan."""

    def __init__(self) -> None:
        headers = {}
        if config.TRONGRID_API_KEY:
            headers["TRON-PRO-API-KEY"] = config.TRONGRID_API_KEY
        timeout = httpx.Timeout(config.HTTP_TIMEOUT_SECONDS)
        self._trongrid = httpx.AsyncClient(
            base_url=config.TRONGRID_BASE_URL, headers=headers, timeout=timeout
        )
        self._tronscan = httpx.AsyncClient(
            base_url=config.TRONSCAN_BASE_URL, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._trongrid.aclose()
        await self._tronscan.aclose()

    async def check(self, address: str) -> AccountInfo:
        """Бросает TronApiError, если оба источника недоступны."""
        try:
            return await self._check_trongrid(address)
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            logger.warning("TronGrid не ответил (%r), переключаюсь на Tronscan", exc)
        try:
            return await self._check_tronscan(address)
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            logger.error("Tronscan тоже не ответил: %r", exc)
            raise TronApiError("TronGrid и Tronscan недоступны") from exc

    # --- TronGrid ---

    async def _check_trongrid(self, address: str) -> AccountInfo:
        account, balance = await asyncio.gather(
            self._request(
                self._trongrid,
                "POST",
                "/wallet/getaccount",
                json={"address": address, "visible": True},
            ),
            self._trongrid_usdt_balance(address),
        )
        # Пустой ответ getaccount ({}) означает неактивированный аккаунт.
        return AccountInfo(
            address=address,
            activated=bool(account),
            usdt_balance=balance,
            source="TronGrid",
            trx_balance=Decimal(int(account.get("balance", 0))) / 10**6,
        )

    async def _trongrid_usdt_balance(self, address: str) -> Decimal:
        payload = decode_base58check(address)
        assert payload is not None, "адрес должен быть провалидирован до запроса"
        parameter = payload[1:].hex().rjust(64, "0")
        data = await self._request(
            self._trongrid,
            "POST",
            "/wallet/triggerconstantcontract",
            json={
                "owner_address": address,
                "contract_address": config.USDT_CONTRACT_ADDRESS,
                "function_selector": "balanceOf(address)",
                "parameter": parameter,
                "visible": True,
            },
        )
        raw = int(data["constant_result"][0], 16)
        return Decimal(raw) / 10**_USDT_DECIMALS

    async def available_energy(self, address: str) -> int:
        """Свободная энергия на кошельке (свой стейкинг + делегированная)."""
        data = await self._request(
            self._trongrid,
            "POST",
            "/wallet/getaccountresource",
            json={"address": address, "visible": True},
        )
        return max(0, int(data.get("EnergyLimit", 0)) - int(data.get("EnergyUsed", 0)))

    # --- Tronscan (фолбэк) ---

    async def _check_tronscan(self, address: str) -> AccountInfo:
        data = await self._request(
            self._tronscan, "GET", "/api/account", params={"address": address}
        )
        # У этого эндпоинта нет флага активации — судим по следам жизни аккаунта:
        # у неактивированного все счётчики нулевые и списки токенов пустые.
        activated = bool(
            data.get("date_created")
            or data.get("totalTransactionCount")
            or data.get("balance")
            or data.get("trc20token_balances")
        )
        balance = Decimal(0)
        for token in data.get("trc20token_balances") or []:
            if token.get("tokenId") == config.USDT_CONTRACT_ADDRESS:
                decimals = int(token.get("tokenDecimal", _USDT_DECIMALS))
                balance = Decimal(str(token.get("balance", "0"))) / 10**decimals
                break
        return AccountInfo(
            address=address,
            activated=activated,
            usdt_balance=balance,
            source="Tronscan",
            trx_balance=Decimal(int(data.get("balance") or 0)) / 10**6,
        )

    # --- общее ---

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, **kwargs):
        last_exc: httpx.HTTPError | None = None
        for attempt in range(1, config.HTTP_ATTEMPTS + 1):
            try:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "%s %s%s: попытка %d/%d не удалась: %r",
                    method, client.base_url, path, attempt, config.HTTP_ATTEMPTS, exc,
                )
        assert last_exc is not None
        raise last_exc
