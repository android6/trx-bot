"""Хранилище «Telegram ID → кошелёк»: wallets.json рядом с ботом.

Только публичные адреса — никаких ключей. Файл можно править и руками,
бот перечитывает его при каждом обращении.
"""

import json
import logging

import config

logger = logging.getLogger(__name__)


def _load() -> dict[str, str]:
    try:
        with open(config.WALLETS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("wallets.json не читается (%s) — считаю пустым", exc)
        return {}


def get(user_id: int) -> str:
    """Кошелёк пользователя из wallets.json; пустая строка, если не задан."""
    return _load().get(str(user_id), "")


def set_wallet(user_id: int, address: str) -> None:
    data = _load()
    data[str(user_id)] = address
    with open(config.WALLETS_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
