"""Память о пользователях (users.json): кто писал боту и кого уже поприветствовали.

Нужна, чтобы после добавления человека в ALLOWED_USER_IDS бот сам сообщил ему
об этом: писать первым Telegram не разрешает, но если человек уже писал боту —
чат открыт.
"""

import json
import logging

import config

logger = logging.getLogger(__name__)

_EMPTY = {"seen": [], "welcomed": []}


def _load() -> dict:
    try:
        with open(config.USERS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return {key: list(data.get(key, [])) for key in _EMPTY}
            return dict(_EMPTY)
    except FileNotFoundError:
        return dict(_EMPTY)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("users.json не читается (%s) — считаю пустым", exc)
        return dict(_EMPTY)


def _save(data: dict) -> None:
    with open(config.USERS_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def mark_seen(user_id: int) -> None:
    data = _load()
    if user_id not in data["seen"]:
        data["seen"].append(user_id)
        _save(data)


def seen() -> set[int]:
    return set(_load()["seen"])


def welcomed() -> set[int]:
    return set(_load()["welcomed"])


def mark_welcomed(user_id: int) -> None:
    data = _load()
    if user_id not in data["welcomed"]:
        data["welcomed"].append(user_id)
        _save(data)
