"""Конфигурация: секреты берутся из .env, константы задаются здесь."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Секреты и персональные настройки (.env, не коммитится) ---

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")


ENV_FILE = Path(__file__).with_name(".env")


def _parse_ids(raw: str) -> frozenset[int]:
    return frozenset(
        int(part) for part in raw.replace(" ", "").split(",") if part
    )


def reload_allowed_user_ids() -> frozenset[int]:
    """Перечитывает whitelist из .env — правки подхватываются без перезапуска."""
    global ALLOWED_USER_IDS
    from dotenv import dotenv_values

    raw = dotenv_values(ENV_FILE).get("ALLOWED_USER_IDS") or ""
    try:
        ALLOWED_USER_IDS = _parse_ids(raw)
    except ValueError:
        pass  # битую правку игнорируем, работаем по старому списку
    return ALLOWED_USER_IDS


# Whitelist: кто вообще допущен к боту.
try:
    ALLOWED_USER_IDS = _parse_ids(os.getenv("ALLOWED_USER_IDS", ""))
except ValueError as _exc:
    raise SystemExit(f"Не смог разобрать ALLOWED_USER_IDS в .env: {_exc}") from _exc

# Как часто перечитывать whitelist и приветствовать новичков (секунды).
WHITELIST_POLL_SECONDS = 60

# Кошельки пользователи задают сами командой /wallet — бот хранит их здесь.
WALLETS_FILE = Path(__file__).with_name("wallets.json")
# Память о пользователях: кто писал боту и кто уже получил приветствие.
USERS_FILE = Path(__file__).with_name("users.json")
# Лог-файл (ротация: 1 МБ × 3 файла).
LOG_FILE = Path(__file__).with_name("bot.log")
# Платёжный TRX-адрес сервиса аренды энергии.
ENERGY_SERVICE_PAYMENT_ADDRESS = os.getenv("ENERGY_SERVICE_PAYMENT_ADDRESS", "")
# Адрес автоматического обмена USDT → TRX того же сервиса (пополнить TRX,
# когда их не хватает на оплату аренды). Пусто — подсказка обмена не показывается.
EXCHANGE_PAYMENT_ADDRESS = os.getenv("EXCHANGE_PAYMENT_ADDRESS", "")
# Предупреждать заранее, когда TRX на кошельке меньше этого запаса
# (10 ≈ три полных перевода по двойному тарифу) — чтобы успеть обменять.
try:
    TRX_LOW_BALANCE_WARN = float(os.getenv("TRX_LOW_BALANCE_WARN", "") or 10.0)
except ValueError:
    TRX_LOW_BALANCE_WARN = 10.0
# База https-мостика (страница bridge/index.html на любом хостинге).
# Если задана — бот добавляет к сообщениям кнопку «Открыть в TronLink».
BRIDGE_BASE_URL = os.getenv("BRIDGE_BASE_URL", "").strip().rstrip("/")

# --- Константы (тарифы сервиса аренды могут меняться — правь здесь) ---

# Получатель уже держит USDT — хватает 65 000 энергии.
TARIFF_LOW_TRX = 1.5
ENERGY_LOW = 65_000

# USDT нет или аккаунт не активирован — нужно 131 000 энергии.
TARIFF_HIGH_TRX = 3.0
ENERGY_HIGH = 131_000

# Официальный контракт USDT TRC-20 (сверен с tronscan.org).
USDT_CONTRACT_ADDRESS = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# Ненулевой баланс ниже порога (в USDT) — предупреждаем о риске двойного тарифа.
LOW_BALANCE_WARN_THRESHOLD = 1.0

# Запас TRX сверх тарифа на сетевую комиссию при оплате аренды.
TRX_FEE_RESERVE = 0.3

# Мягкое предупреждение «запас TRX тает» — не чаще раза в этот период.
TRX_WARN_COOLDOWN_SECONDS = 3600

# --- Deeplink TronLink ---

TRON_CHAIN_ID = "0x2b6653dc"  # mainnet
# Документация TronLink не уточняет единицы суммы в deeplink; судя по примерам —
# целые токены. Если TronLink подставит сумму в миллион раз меньше — поставь True.
DEEPLINK_AMOUNT_IN_SUN = False

# --- Ожидание энергии после оплаты аренды ---

ENERGY_POLL_SECONDS = 2.0
ENERGY_WAIT_TIMEOUT_SECONDS = 180

# --- Сеть ---

TRONGRID_BASE_URL = "https://api.trongrid.io"
TRONSCAN_BASE_URL = "https://apilist.tronscanapi.com"
HTTP_TIMEOUT_SECONDS = 4.0
HTTP_ATTEMPTS = 2  # попыток на каждый запрос к каждому API
