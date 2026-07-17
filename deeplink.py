"""Сборка deeplink-ссылок TronLink с предзаполненным переводом.

Формат из официальной документации TronLink (docs.tronlink.org, раздел DeepLink):
tronlinkoutside://pull.activity?param=<urlencoded JSON>
Подписание происходит только в самом TronLink — ссылка лишь предзаполняет форму.
"""

import json
import uuid
from decimal import Decimal
from urllib.parse import quote

import config


def _format_amount(value: Decimal | None) -> str:
    if value is None:
        return ""  # мостик сам спросит сумму у пользователя
    if config.DEEPLINK_AMOUNT_IN_SUN:
        return str(int(value * 1_000_000))
    return f"{value.normalize():f}"


def _link(*, to: str, amount: Decimal, token_id: str, contract: str,
          from_wallet: str) -> str:
    params = {
        "url": "",
        "callbackUrl": "",
        "dappIcon": "",
        "dappName": "tron-energy-bot",
        "protocol": "TronLink",
        "version": "1.0",
        "chainId": config.TRON_CHAIN_ID,
        "memo": "",
        "from": from_wallet,
        "to": to,
        "loginAddress": from_wallet,
        "tokenId": token_id,
        "contract": contract,
        "amount": _format_amount(amount),
        "action": "transfer",
        "actionId": str(uuid.uuid4()),
    }
    encoded = quote(json.dumps(params, separators=(",", ":")), safe="")
    return f"tronlinkoutside://pull.activity?param={encoded}"


def open_in_tronlink(url: str) -> str:
    """Deeplink «открыть URL во встроенном dApp-браузере TronLink» (action=open).

    В отличие от action=transfer не требует whitelist-а TronLink
    (transfer работает только для dApp, одобренных самим TronLink).
    """
    params = {"url": url, "action": "open", "protocol": "tronlink", "version": "1.0"}
    encoded = quote(json.dumps(params, separators=(",", ":")), safe="")
    return f"tronlinkoutside://pull.activity?param={encoded}"


def bridge_url(link: str) -> str | None:
    """https-обёртка deeplink-а через страницу-мостик; None, если мостик не настроен.

    Ссылка уходит во фрагменте (#...) — до сервера хостинга она не долетает.
    """
    if not config.BRIDGE_BASE_URL:
        return None
    return f"{config.BRIDGE_BASE_URL}/#link={quote(link, safe='')}"


def trx_transfer(to: str, amount_trx: Decimal, from_wallet: str) -> str:
    """Перевод TRX (оплата аренды энергии). tokenId «0» = нативный TRX."""
    return _link(to=to, amount=amount_trx, token_id="0", contract="",
                 from_wallet=from_wallet)


def usdt_transfer(to: str, amount_usdt: Decimal | None, from_wallet: str) -> str:
    """Перевод USDT TRC-20 получателю. Без суммы — мостик спросит её сам."""
    return _link(
        to=to,
        amount=amount_usdt,
        token_id=config.USDT_CONTRACT_ADDRESS,
        contract=config.USDT_CONTRACT_ADDRESS,
        from_wallet=from_wallet,
    )
