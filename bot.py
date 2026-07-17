"""Личный Telegram-бот (aiogram): адрес и сумма → тариф аренды энергии → TronLink."""

import asyncio
import io
import logging
import logging.handlers
import re
import time
import uuid
from collections import OrderedDict
from decimal import ROUND_DOWN, Decimal, InvalidOperation

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
import deeplink
import tron
import users
import wallets
from pluralize import plural
from tron import AccountInfo, TronApiError, TronChecker

logger = logging.getLogger(__name__)

dp = Dispatcher()

ADDRESS_PATTERN = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")
# Лукбехинд отсекает числа, прилипшие к словам («trc20», «trc-20»).
AMOUNT_PATTERN = re.compile(r"(?<![\w.,-])\d+(?:[.,]\d+)?")

# Состояние в памяти процесса (бот один, пользователей немного).
_checker: TronChecker | None = None
AWAITING_WALLET: set[int] = set()          # кто сейчас меняет кошелёк
PENDING_REQUEST: dict[int, str] = {}       # отложенный перевод до настройки кошелька
AWAITING_AMOUNT: dict[int, str] = {}       # спросили сумму перевода: user → получатель
AWAITING_EXCHANGE: set[int] = set()        # спросили сумму обмена USDT → TRX
TRX_WARNED_AT: dict[int, float] = {}       # когда последний раз мягко предупреждали о TRX
WATCHERS: dict[int, tuple[asyncio.Task, str]] = {}  # user → (вотчер, описание)
QR_STORE: OrderedDict[str, tuple[str, str]] = OrderedDict()


def _fmt_energy(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def _fmt_amount(value: Decimal) -> str:
    """Баланс для показа: до двух знаков, лишние хвосты отрезаются (24.09103 → 24.09).

    Округление вниз — лучше показать на копейку меньше, чем приукрасить.
    """
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_DOWN).normalize():f}"


# Каноничная формулировка запроса — одна на все сообщения бота.
REQUEST_HINT = "адрес получателя и сумму USDT через пробел"

START_TEXT = (
    f"Пришли {REQUEST_HINT}, например:\n"
    "<code>TAUN6FwrnwwmaEqYcckffC7wYmbaS6cBiX 25</code>\n\n"
    "Дальше я:\n"
    "1) проверю получателя и скажу тариф аренды;\n"
    "2) дам кнопку и QR для оплаты TRX сервису аренды;\n"
    "3) дождусь, когда энергия придёт на твой кошелёк;\n"
    "4) пришлю кнопку и QR на сам перевод USDT.\n\n"
    "Кнопка ведёт на страницу-мостик: на телефоне она передаёт перевод в "
    "приложение TronLink, на компьютере — в расширение TronLink "
    "(разблокируй его заранее). QR — то же самое через камеру телефона; "
    "пришлю картинкой по кнопке, чтобы не загромождать чат.\n\n"
    f"Тарифы:\n"
    f"• у получателя уже есть USDT → {_fmt_energy(config.ENERGY_LOW)} энергии → "
    f"{config.TARIFF_LOW_TRX:g} TRX\n"
    f"• USDT нет или аккаунт не активирован → {_fmt_energy(config.ENERGY_HIGH)} энергии → "
    f"{config.TARIFF_HIGH_TRX:g} TRX\n\n"
    "Сумму можно не писать — тогда я спрошу её отдельным вопросом.\n"
    "/wallet — задать свой кошелёк, /config — тарифы и адреса, "
    "/cancel — перестать ждать энергию, /help — список команд."
)

HELP_TEXT = (
    "Команды:\n"
    "/start — инструкция, как пользоваться\n"
    "/wallet — задать или сменить свой кошелёк (нужно один раз, до первого перевода)\n"
    "/config — тарифы и адреса\n"
    "/cancel — перестать ждать энергию\n"
    "/help — этот список\n\n"
    f"Главное: пришли {REQUEST_HINT} — дальше бот всё сделает сам.\n\n"
    "Сервис аренды энергии и обмена USDT ↔ TRX: @TRXerer"
)

WALLET_PROMPT = (
    "Для начала настроим твой кошелёк. Пришли следующим сообщением адрес "
    "своего TRON-кошелька — с него ты будешь платить за аренду энергии и "
    "отправлять USDT. Нужен только публичный адрес (начинается с T), "
    "никаких ключей и паролей."
)

BRIDGE_HINT = (
    "💡 Кнопка и QR с авто-открытием TronLink появятся после настройки "
    "мостика (BRIDGE_BASE_URL, раздел «Мостик» в README)."
)


def _service_address_configured() -> bool:
    return tron.is_valid_address(config.ENERGY_SERVICE_PAYMENT_ADDRESS)


def _exchange_configured() -> bool:
    return tron.is_valid_address(config.EXCHANGE_PAYMENT_ADDRESS)


def _user_wallet(user_id: int) -> str | None:
    """Кошелёк пользователя; None, если не задан или невалиден."""
    wallet = wallets.get(user_id)
    return wallet if tron.is_valid_address(wallet) else None


def _is_allowed(user_id: int | None) -> bool:
    return user_id is not None and user_id in config.ALLOWED_USER_IDS


async def _deny(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else "неизвестен"
    if message.from_user is not None:
        users.mark_seen(message.from_user.id)
    await message.answer(
        "⛔ Доступ ограничен — это личный бот.\n\n"
        f"Ваш Telegram ID: <code>{user_id}</code>\n"
        "Сообщите его разработчику бота. Как только вас добавят в список, "
        "бот сам напишет вам об этом."
    )


def _pick_tariff(info: AccountInfo) -> tuple[float, int]:
    """Возвращает (тариф в TRX, требуемая энергия)."""
    if info.activated and info.usdt_balance > 0:
        return config.TARIFF_LOW_TRX, config.ENERGY_LOW
    return config.TARIFF_HIGH_TRX, config.ENERGY_HIGH


def _extract_amount(text: str) -> tuple[Decimal | None, str | None]:
    """Ищет сумму USDT в тексте; возвращает (сумма, текст ошибки)."""
    match = AMOUNT_PATTERN.search(text)
    if match is None:
        return None, None
    try:
        amount = Decimal(match.group(0).replace(",", "."))
    except InvalidOperation:
        return None, "Не понял сумму — напиши числом, например 25 или 25.5."
    if amount <= 0:
        return None, "Сумма должна быть больше нуля."
    if -amount.as_tuple().exponent > 6:
        return None, "У USDT максимум 6 знаков после запятой."
    return amount, None


def format_report(info: AccountInfo) -> str:
    tariff, energy = _pick_tariff(info)
    lines = [f"Получатель: <code>{info.address}</code>"]
    if not info.activated:
        lines.append("⚠️ Аккаунт <b>не активирован</b>.")
    lines.append(f"Баланс USDT: <b>{_fmt_amount(info.usdt_balance)}</b>")
    lines.append("")
    if info.activated and info.usdt_balance > 0:
        lines.append(
            f"✅ USDT у получателя есть → {_fmt_energy(energy)} энергии → "
            f"{tariff:g} TRX"
        )
        if info.usdt_balance < Decimal(str(config.LOW_BALANCE_WARN_THRESHOLD)):
            lines.append(
                f"⚠️ Баланс получателя меньше "
                f"{config.LOW_BALANCE_WARN_THRESHOLD:g} USDT — его могут "
                "обнулить, и тогда к моменту перевода понадобится двойной "
                f"тариф ({config.TARIFF_HIGH_TRX:g} TRX)."
            )
    else:
        lines.append(
            f"❗ USDT у получателя нет / аккаунт не активирован → "
            f"{_fmt_energy(energy)} энергии → {tariff:g} TRX"
        )
    return "\n".join(lines)


def _rental_block(tariff: float) -> str:
    if not _service_address_configured():
        return (
            "⚠️ Адрес сервиса аренды не настроен — укажи "
            "ENERGY_SERVICE_PAYMENT_ADDRESS в .env и перезапусти бота."
        )
    return "\n".join(
        [
            f"Шаг 1 из 2 — аренда: отправь ровно <code>{tariff:g}</code> TRX на:",
            f"<code>{config.ENERGY_SERVICE_PAYMENT_ADDRESS}</code>",
        ]
    )


def _transfer_block(recipient: str, amount: Decimal, step_prefix: str = "") -> str:
    return "\n".join(
        [
            f"{step_prefix}перевод <b>{amount.normalize():f} USDT</b> на:",
            f"<code>{recipient}</code>",
            "Сверь сумму и получателя, подтверди в TronLink.",
        ]
    )


def _qr_png(data: str) -> bytes:
    buffer = io.BytesIO()
    qrcode.make(data).save(buffer)
    return buffer.getvalue()


def _remember_qr(payload: str, caption: str) -> str:
    """Кладёт данные QR в память и возвращает id для callback-кнопки.

    QR отправляется картинкой только по нажатию кнопки — иначе он
    загромождает чат тем, кто работает с компьютера.
    """
    while len(QR_STORE) >= 30:
        QR_STORE.popitem(last=False)
    qr_id = uuid.uuid4().hex[:12]
    QR_STORE[qr_id] = (payload, caption)
    return qr_id


def _action_ui(
    deep_link: str,
    qr_caption: str,
    fallback_qr: tuple[str, str] | None,
) -> tuple[InlineKeyboardMarkup | None, str | None]:
    """Кнопки действия: (клавиатура, url мостика или None).

    С мостиком — «Открыть ссылку» + «QR ссылки» (для камеры телефона) +
    «QR приложения» (чистый адрес для сканера внутри TronLink).
    Без мостика — только «QR приложения».
    """
    bridge = deeplink.bridge_url(deep_link)
    if bridge is not None:
        qr_id = _remember_qr(bridge, qr_caption)
        row = [
            InlineKeyboardButton(text="Открыть ссылку", url=bridge),
            InlineKeyboardButton(text="QR ссылки", callback_data=f"qr:{qr_id}"),
        ]
        if fallback_qr is not None:
            # Отдельный QR под сканер ВНУТРИ TronLink: он понимает только
            # чистый адрес (подставит получателя, сумму вводить руками).
            payload, caption = fallback_qr
            scanner_id = _remember_qr(payload, caption)
            row.append(
                InlineKeyboardButton(
                    text="QR приложения", callback_data=f"qr:{scanner_id}"
                )
            )
        return InlineKeyboardMarkup(inline_keyboard=[row]), bridge
    if fallback_qr is not None:
        payload, caption = fallback_qr
        qr_id = _remember_qr(payload, caption)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="QR приложения", callback_data=f"qr:{qr_id}"
                )
            ]]
        )
        return markup, None
    return None, None


def _trx_warning(
    own: AccountInfo | None, tariff: float, user_id: int
) -> tuple[str | None, InlineKeyboardMarkup | None]:
    """Предупреждение о TRX: (текст, клавиатура обмена) или (None, None).

    Два уровня: «не хватает на эту аренду» — жёсткое, показывается всегда;
    «запас тает — меньше порога TRX_LOW_BALANCE_WARN» — мягкое, не чаще
    раза в TRX_WARN_COOLDOWN_SECONDS, чтобы не утомлять.
    """
    if own is None:
        return None, None
    low = Decimal(str(config.TRX_LOW_BALANCE_WARN))
    if own.trx_balance >= low:
        return None, None
    balance = _fmt_amount(own.trx_balance)
    need = Decimal(str(tariff)) + Decimal(str(config.TRX_FEE_RESERVE))
    if own.trx_balance < need:
        header = (
            f"⚠️ Мало TRX для оплаты аренды: на твоём кошельке {balance} TRX, "
            f"а нужно ~{_fmt_amount(need)} (тариф + комиссия сети)."
        )
        TRX_WARNED_AT[user_id] = time.monotonic()
    else:
        # Мягкий уровень — с периодом тишины, чтобы не капать на мозги.
        now = time.monotonic()
        last = TRX_WARNED_AT.get(user_id)
        if last is not None and now - last < config.TRX_WARN_COOLDOWN_SECONDS:
            return None, None
        TRX_WARNED_AT[user_id] = now
        full_price = Decimal(str(config.TARIFF_HIGH_TRX)) + Decimal(
            str(config.TRX_FEE_RESERVE)
        )
        rentals_left = int(own.trx_balance / full_price)
        if rentals_left > 0:
            word = plural(rentals_left, "перевод", "перевода", "переводов")
            left = (
                f"хватит ещё примерно на {rentals_left} {word} "
                "по двойному тарифу"
            )
        else:
            left = "на перевод по двойному тарифу уже не хватит"
        header = f"ℹ️ Запас TRX тает: осталось {balance} — {left}. Пора пополнить."
    lines = [header]
    markup = None
    if _exchange_configured():
        lines.append(
            "Обмен USDT → TRX: отправь USDT на адрес обмена — TRX "
            "вернутся автоматически:"
        )
        lines.append(f"<code>{config.EXCHANGE_PAYMENT_ADDRESS}</code>")
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="Обменять USDT → TRX", callback_data="exch"
                )
            ]]
        )
    return "\n".join(lines), markup


def _browser_link_quote(bridge: str) -> str:
    """Свёрнутая цитата с копируемой ссылкой — если кнопка открыла не тот браузер."""
    return (
        "<blockquote expandable>Кнопка открыла браузер без расширения TronLink? "
        "Скопируй ссылку и вставь в нужный:\n"
        f"<code>{bridge}</code></blockquote>"
    )


# --- ожидание энергии ---


async def _send_with_retry(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """Отправка с повторами: сетевой чих не должен терять готовое уведомление."""
    for attempt in range(1, 6):
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return True
        except TelegramNetworkError as exc:
            logger.warning(
                "отправка уведомления не прошла (попытка %d/5): %r", attempt, exc
            )
            await asyncio.sleep(5 * attempt)
    logger.error("уведомление так и не отправилось после 5 попыток")
    return False


def _start_energy_watch(
    bot: Bot,
    chat_id: int,
    user_id: int,
    wallet: str,
    required_energy: int,
    recipient: str,
    amount: Decimal | None,
) -> None:
    if amount is not None:
        label = f"перевод {amount.normalize():f} USDT на {recipient}"
    else:
        label = f"перевод на {recipient}"
    old = WATCHERS.pop(user_id, None)
    if old is not None:
        old_task, old_label = old
        if not old_task.done():
            old_task.cancel()
            # Энергии хватает на один перевод, поэтому ожидание всегда одно —
            # но о перекрытии говорим явно, а не молча.
            if old_label != label:
                asyncio.create_task(
                    _send_with_retry(
                        bot,
                        chat_id,
                        f"ℹ️ Прежнее ожидание энергии ({old_label}) отменил — "
                        "теперь жду её под новый запрос.\n\n"
                        "⚠️ Если ту аренду ты уже оплатил — новую НЕ оплачивай: "
                        "сервис не суммирует энергию, работает одна аренда за "
                        "раз. Пришедшую энергию я подставлю под новый запрос. "
                        "Прежний перевод, если нужен, пришли заново после.",
                    )
                )
    task = asyncio.create_task(
        _watch_energy(bot, chat_id, wallet, required_energy, recipient, amount)
    )
    WATCHERS[user_id] = (task, label)

    def _cleanup(done: asyncio.Task) -> None:
        current = WATCHERS.get(user_id)
        if current is not None and current[0] is done:
            WATCHERS.pop(user_id, None)
        if not done.cancelled() and done.exception() is not None:
            logger.error("вотчер энергии упал", exc_info=done.exception())

    task.add_done_callback(_cleanup)


async def _watch_energy(bot: Bot, chat_id: int, wallet: str,
                        required_energy: int, recipient: str,
                        amount: Decimal | None) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.ENERGY_WAIT_TIMEOUT_SECONDS
    while True:
        energy = None
        try:
            energy = await _checker.available_energy(wallet)
        except Exception as exc:
            logger.debug("опрос энергии не удался: %r", exc)
        if energy is not None and energy >= required_energy:
            if amount is None:
                # Сумму так и не прислали — напомним и дадим аварийный QR.
                qr_id = _remember_qr(
                    recipient,
                    "QR с адресом получателя — его читает сканер ВНУТРИ TronLink "
                    "(в форме перевода USDT); аварийный вариант, сумму введи руками.",
                )
                emergency = InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="QR приложения", callback_data=f"qr:{qr_id}"
                        )
                    ]]
                )
                await _send_with_retry(
                    bot,
                    chat_id,
                    f"⚡ Энергия пришла: доступно {_fmt_energy(energy)}. "
                    "Пришли сумму перевода числом — дам кнопку на Шаг 2 из 2. "
                    "Или отправь USDT сам из TronLink.",
                    reply_markup=emergency,
                )
                return
            if amount is not None:
                # Перед ссылкой ещё раз сверяем баланс USDT: неудачный перевод
                # откатится, а арендованная энергия сгорит впустую.
                own_balance = None
                try:
                    own_balance = (await _checker.check(wallet)).usdt_balance
                except Exception as exc:
                    logger.warning("не смог узнать свой баланс USDT: %r", exc)
                if own_balance is not None and own_balance < amount:
                    await _send_with_retry(
                        bot,
                        chat_id,
                        f"⚡ Энергия пришла (доступно {_fmt_energy(energy)}), но на "
                        f"твоём кошельке только {_fmt_amount(own_balance)} USDT из "
                        f"{amount.normalize():f} нужных. Если отправить сейчас — "
                        "перевод не пройдёт, а энергия сгорит впустую. Пополни USDT "
                        f"и пришли {REQUEST_HINT} ещё раз.",
                    )
                    return
            link = deeplink.usdt_transfer(recipient, amount, wallet)
            markup, bridge = _action_ui(
                link,
                "QR шага 2 из 2 — сканируй камерой телефона: откроется страница и "
                "передаст перевод USDT в приложение TronLink.",
                fallback_qr=(
                    recipient,
                    "QR с адресом получателя — его читает сканер ВНУТРИ TronLink "
                    "(в форме перевода USDT); сумму введи руками.",
                ),
            )
            text = (
                f"⚡ Энергия пришла: доступно {_fmt_energy(energy)}.\n\n"
                + _transfer_block(recipient, amount, step_prefix="Шаг 2 из 2 — ")
            )
            if bridge is not None:
                text += "\n\n" + _browser_link_quote(bridge)
            else:
                text += "\n\n" + BRIDGE_HINT
            await _send_with_retry(bot, chat_id, text, reply_markup=markup)
            return
        if loop.time() >= deadline:
            if amount is None:
                # Без суммы это могла быть просто проверка баланса — не шумим.
                logger.info("энергия за таймаут не пришла (без суммы) — молчу")
                return
            have = _fmt_energy(energy) if energy is not None else "не удалось узнать сколько"
            await _send_with_retry(
                bot,
                chat_id,
                f"⌛ Перестал следить: за "
                f"{config.ENERGY_WAIT_TIMEOUT_SECONDS // 60} мин аренда энергии "
                f"так и не пришла на твой кошелёк (сейчас доступно {have} из "
                f"{_fmt_energy(required_energy)} нужных).\n\n"
                "Это не страшно: когда оплатишь аренду, просто пришли "
                f"{REQUEST_HINT} ещё раз — я снова проверю энергию, и если она "
                "уже пришла, сразу дам кнопку перевода без повторной оплаты.",
            )
            return
        await asyncio.sleep(config.ENERGY_POLL_SECONDS)


# --- обработчики ---


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    await message.answer(START_TEXT)
    if _user_wallet(message.from_user.id) is None:
        AWAITING_WALLET.add(message.from_user.id)
        await message.answer(WALLET_PROMPT)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    await message.answer(HELP_TEXT)


@dp.message(Command("config"))
async def cmd_config(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    service = (
        f"<code>{config.ENERGY_SERVICE_PAYMENT_ADDRESS}</code>"
        if _service_address_configured()
        else "⚠️ не настроен (.env → ENERGY_SERVICE_PAYMENT_ADDRESS)"
    )
    wallet = _user_wallet(message.from_user.id)
    my_wallet = (
        f"<code>{wallet}</code>"
        if wallet is not None
        else "⚠️ не задан — настрой командой /wallet"
    )
    await message.answer(
        f"Тариф при наличии USDT: {_fmt_energy(config.ENERGY_LOW)} энергии = "
        f"{config.TARIFF_LOW_TRX:g} TRX\n"
        f"Тариф без USDT / без активации: {_fmt_energy(config.ENERGY_HIGH)} энергии = "
        f"{config.TARIFF_HIGH_TRX:g} TRX\n"
        f"Порог предупреждения о малом балансе: "
        f"{config.LOW_BALANCE_WARN_THRESHOLD:g} USDT\n"
        f"Контракт USDT: <code>{config.USDT_CONTRACT_ADDRESS}</code>\n"
        f"Адрес сервиса аренды: {service}\n"
        + (
            f"Адрес обмена USDT → TRX: <code>{config.EXCHANGE_PAYMENT_ADDRESS}</code>\n"
            if _exchange_configured()
            else ""
        )
        + f"Твой кошелёк: {my_wallet}\n"
        f"Ожидание энергии: опрос каждые {config.ENERGY_POLL_SECONDS:g} с, "
        f"таймаут {config.ENERGY_WAIT_TIMEOUT_SECONDS // 60} мин"
    )


async def _save_wallet(message: Message, address: str) -> bool:
    if not tron.is_valid_address(address):
        return False
    user_id = message.from_user.id
    wallets.set_wallet(user_id, address)
    text = f"✅ Кошелёк сохранён: <code>{address}</code>"
    if user_id not in PENDING_REQUEST:
        text += f"\nТеперь пришли {REQUEST_HINT}."
    await message.answer(text)
    return True


@dp.message(Command("wallet"))
async def cmd_wallet(message: Message, command: CommandObject) -> None:
    """Настройка кошелька диалогом: /wallet → бот спросит адрес → присылаешь."""
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    user_id = message.from_user.id
    if command.args:  # форма одним сообщением: /wallet Tадрес
        address = command.args.split()[0].strip()
        if await _save_wallet(message, address):
            AWAITING_WALLET.discard(user_id)
        else:
            await message.answer(
                "Это не похоже на адрес TRON-кошелька. Проверь, что он "
                "скопирован целиком, и пришли ещё раз."
            )
        return
    wallet = _user_wallet(user_id)
    intro = (
        f"Текущий кошелёк: <code>{wallet}</code>\n\n"
        if wallet is not None
        else "Кошелёк ещё не задан.\n\n"
    )
    AWAITING_WALLET.add(user_id)
    await message.answer(
        intro + "Пришли следующим сообщением адрес своего TRON-кошелька "
        "(начинается с T) — с него ты платишь за аренду и отправляешь USDT. "
        "Передумал — /cancel."
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    user_id = message.from_user.id
    cancelled = []
    if user_id in AWAITING_WALLET:
        AWAITING_WALLET.discard(user_id)
        PENDING_REQUEST.pop(user_id, None)
        cancelled.append("настройку кошелька")
    if user_id in AWAITING_EXCHANGE:
        AWAITING_EXCHANGE.discard(user_id)
        cancelled.append("обмен")
    if AWAITING_AMOUNT.pop(user_id, None) is not None:
        cancelled.append("вопрос про сумму")
    watcher_entry = WATCHERS.pop(user_id, None)
    if watcher_entry is not None and not watcher_entry[0].done():
        watcher_entry[0].cancel()
        cancelled.append(f"ожидание энергии ({watcher_entry[1]})")
    if cancelled:
        await message.answer("Ок, отменил: " + ", ".join(cancelled) + ".")
    else:
        await message.answer("Сейчас я и так ничего не жду.")


@dp.callback_query(F.data == "exch")
async def handle_exchange_button(query: CallbackQuery) -> None:
    """Кнопка «Обменять USDT → TRX»: спрашиваем сумму обмена."""
    if not _is_allowed(query.from_user.id):
        await query.answer("⛔ Доступ ограничен.", show_alert=True)
        return
    await query.answer()
    AWAITING_EXCHANGE.add(query.from_user.id)
    if query.message is not None:
        await query.message.answer(
            "Сколько USDT обменять на TRX? Пришли число, например 10. "
            "/cancel — передумать."
        )


@dp.callback_query(F.data.startswith("qr:"))
async def handle_qr(query: CallbackQuery) -> None:
    """Кнопки «QR …»: присылают картинку только по запросу."""
    if not _is_allowed(query.from_user.id):
        await query.answer("⛔ Доступ ограничен.", show_alert=True)
        return
    entry = QR_STORE.get(query.data.split(":", 1)[1])
    if entry is None or query.message is None:
        await query.answer(
            f"QR устарел (бот перезапускался) — пришли {REQUEST_HINT} ещё раз.",
            show_alert=True,
        )
        return
    await query.answer()
    payload, caption = entry
    await query.message.answer_photo(
        BufferedInputFile(_qr_png(payload), filename="qr.png"), caption=caption
    )


@dp.message(F.text)
async def handle_message(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    text = (message.text or "").strip()
    if text.startswith("/"):
        return  # неизвестная команда — молчим, как и раньше
    user_id = message.from_user.id

    # Режим настройки кошелька. При регистрации бот явно ставит AWAITING_WALLET
    # (см. cmd_start / приветствие); а проверка «кошелька нет» — подстраховка на
    # случай перезапуска, ведь это состояние живёт в памяти.
    if user_id in AWAITING_WALLET or _user_wallet(user_id) is None:
        addr_match = ADDRESS_PATTERN.search(text)
        rest = text.replace(addr_match.group(0), " ") if addr_match else text
        amount_in_text, _ = _extract_amount(rest)
        if addr_match is not None and amount_in_text is not None:
            # «Адрес + сумма» — это запрос перевода, а не ответ про кошелёк.
            AWAITING_WALLET.discard(user_id)
            PENDING_REQUEST.pop(user_id, None)
            await _process_transfer_request(message, text)
            return
        pending = PENDING_REQUEST.get(user_id) or ""
        if text and text in pending:
            await message.answer(
                "Это адрес получателя — а мне нужен адрес ТВОЕГО кошелька, "
                "с которого ты платишь за аренду. Пришли его (или /cancel)."
            )
            return
        if await _save_wallet(message, text):
            AWAITING_WALLET.discard(user_id)
            pending_text = PENDING_REQUEST.pop(user_id, None)
            if pending_text:
                await _process_transfer_request(message, pending_text)
        else:
            await message.answer(
                "Сначала настроим твой кошелёк — пришли адрес СВОЕГО "
                "TRON-кошелька (начинается с T). Это тот, с которого ты "
                "платишь за аренду и отправляешь USDT."
            )
        return

    # Диалог обмена: ждём сумму USDT (адрес в сообщении = новый запрос).
    # Обмен — это обычный USDT-перевод на адрес обмена, и идёт он по полной
    # схеме: с проверками и арендой энергии (иначе сеть сожжёт ~13+ TRX).
    if user_id in AWAITING_EXCHANGE:
        if ADDRESS_PATTERN.search(text) is None:
            amount_value, amount_error = _extract_amount(text)
            if amount_value is not None:
                AWAITING_EXCHANGE.discard(user_id)
                await message.answer(
                    "🔁 Обмен USDT → TRX — оформляю как перевод на адрес "
                    "обмена, по обычной схеме с арендой энергии:"
                )
                await _process_transfer_request(
                    message,
                    f"{config.EXCHANGE_PAYMENT_ADDRESS} "
                    f"{amount_value.normalize():f}",
                )
                return
            await message.answer(
                amount_error
                or "Пришли сумму обмена числом, например 10. /cancel — передумать."
            )
            return
        AWAITING_EXCHANGE.discard(user_id)

    # Спросили сумму перевода: ждём число (адрес в сообщении = новый запрос).
    if user_id in AWAITING_AMOUNT:
        if ADDRESS_PATTERN.search(text) is None:
            amount_value, amount_error = _extract_amount(text)
            if amount_value is not None:
                recipient = AWAITING_AMOUNT.pop(user_id)
                await _process_transfer_request(
                    message, f"{recipient} {amount_value.normalize():f}"
                )
                return
            await message.answer(
                amount_error
                or "Пришли сумму перевода числом, например 25. "
                "/cancel — если перевод делаешь сам."
            )
            return
        AWAITING_AMOUNT.pop(user_id, None)

    await _process_transfer_request(message, text)


async def _process_transfer_request(message: Message, text: str) -> None:
    user_id = message.from_user.id

    match = ADDRESS_PATTERN.search(text)
    if match is None:
        await message.answer(f"Не вижу адреса. Пришли {REQUEST_HINT}.")
        return

    address = match.group(0)
    if not tron.is_valid_address(address):
        await message.answer(
            f"В адресе «{address}» похоже есть опечатка, или он скопирован "
            "не целиком. Проверь и пришли ещё раз."
        )
        return

    amount, amount_error = _extract_amount(text.replace(address, " "))
    if amount_error is not None:
        await message.answer(amount_error)
        return
    if amount is not None:
        AWAITING_AMOUNT.pop(user_id, None)  # полный запрос отменяет старый вопрос

    try:
        # «печатает…» — косметика; её сбой не должен ронять обработку запроса
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    except Exception as exc:
        logger.debug("send_chat_action не прошёл: %r", exc)
    try:
        info = await _checker.check(address)
    except TronApiError:
        await message.answer(
            "😕 Не удалось получить данные: и TronGrid, и Tronscan не отвечают. "
            "Попробуй ещё раз через минуту."
        )
        return

    tariff, required_energy = _pick_tariff(info)
    parts = [format_report(info)]
    markup = None
    # Предупреждение о тающем TRX уходит ОТДЕЛЬНЫМ сообщением со своими кнопками.
    trx_warn_text: str | None = None
    trx_warn_markup: InlineKeyboardMarkup | None = None

    wallet = _user_wallet(user_id)

    if wallet is None:
        parts.append(_rental_block(tariff))
        parts.append(
            "⚠️ У тебя ещё не задан свой кошелёк. Пришли следующим сообщением "
            "адрес СВОЕГО кошелька (с которого платишь за аренду и отправляешь "
            "USDT) — я сохраню его и сразу продолжу. /cancel — передумать."
        )
        AWAITING_WALLET.add(user_id)
        PENDING_REQUEST[user_id] = text
    elif not _service_address_configured():
        parts.append(_rental_block(tariff))
    else:
        # Свой кошелёк: хватает ли USDT на перевод и TRX на оплату аренды.
        own = None
        try:
            own = await _checker.check(wallet)
        except TronApiError:
            logger.warning("не смог проверить свой кошелёк — пропускаю проверки балансов")
        if amount is not None and own is not None and own.usdt_balance < amount:
            # Неудачный перевод USDT сжёг бы арендованную энергию впустую.
            parts.append(
                f"⛔ Тебе не хватает USDT: перевод на <b>{amount.normalize():f}</b>, "
                f"а на твоём кошельке только <b>{_fmt_amount(own.usdt_balance)}</b>. "
                "Аренда энергии пока ни к чему — пополни USDT и пришли "
                f"{REQUEST_HINT} ещё раз."
            )
            await message.answer("\n\n".join(parts))
            return
        energy_now = None
        try:
            energy_now = await _checker.available_energy(wallet)
        except Exception as exc:
            logger.warning("не смог узнать энергию кошелька: %r", exc)
        if energy_now is not None and energy_now >= required_energy:
            parts.append(
                f"⚡ На твоём кошельке уже {_fmt_energy(energy_now)} энергии — "
                "аренда не нужна."
            )
            if amount is not None:
                transfer_link = deeplink.usdt_transfer(address, amount, wallet)
                parts.append(_transfer_block(address, amount))
                markup, bridge = _action_ui(
                    transfer_link,
                    "QR — сканируй камерой телефона: откроется страница и "
                    "передаст перевод USDT в приложение TronLink.",
                    fallback_qr=(
                        address,
                        "QR с адресом получателя — его читает сканер ВНУТРИ TronLink "
                        "(в форме перевода USDT); сумму введи руками.",
                    ),
                )
                if bridge is not None:
                    parts.append(_browser_link_quote(bridge))
                else:
                    parts.append(BRIDGE_HINT)
            else:
                parts.append(
                    "💬 Сколько USDT перевести? Пришли число — подготовлю "
                    "кнопку перевода. Если просто проверял адрес — не отвечай."
                )
                AWAITING_AMOUNT[user_id] = address
            # Предупреждение о TRX здесь не показываем: человек занят
            # переводом, кнопка обмена рядом только путает. Оно всплывёт
            # на следующем запросе с арендой — там и уместно.
        else:
            rental_link = deeplink.trx_transfer(
                config.ENERGY_SERVICE_PAYMENT_ADDRESS, Decimal(str(tariff)), wallet
            )
            parts.append(_rental_block(tariff))
            if amount is not None:
                what_next = f"пришлю кнопку на перевод {amount.normalize():f} USDT"
            else:
                what_next = "сообщу, что можно переводить USDT"
            parts.append(
                f"⏳ После оплаты жду энергию на <code>{wallet}</code> "
                f"(до {config.ENERGY_WAIT_TIMEOUT_SECONDS // 60} мин) — "
                f"как придёт, {what_next}. /cancel — отменить."
            )
            if amount is None:
                parts.append(
                    "💬 Сколько USDT перевести? Пришли число — подготовлю "
                    "кнопку перевода. Делаешь перевод сам — не отвечай."
                )
                AWAITING_AMOUNT[user_id] = address
            markup, bridge = _action_ui(
                rental_link,
                f"QR шага 1 из 2 — сканируй камерой телефона: откроется страница и "
                f"передаст оплату {tariff:g} TRX в приложение TronLink.",
                fallback_qr=(
                    config.ENERGY_SERVICE_PAYMENT_ADDRESS,
                    f"QR с адресом сервиса аренды — его читает сканер ВНУТРИ "
                    f"TronLink (в форме перевода TRX); сумму {tariff:g} TRX "
                    f"введи руками.",
                ),
            )
            if bridge is not None:
                parts.append(_browser_link_quote(bridge))
            else:
                parts.append(BRIDGE_HINT)
            # Внутри самого обмена про обмен не напоминаем — это была бы петля.
            if address != config.EXCHANGE_PAYMENT_ADDRESS:
                trx_warn_text, trx_warn_markup = _trx_warning(own, tariff, user_id)
            _start_energy_watch(
                message.bot,
                chat_id=message.chat.id,
                user_id=user_id,
                wallet=wallet,
                required_energy=required_energy,
                recipient=address,
                amount=amount,
            )

    await message.answer("\n\n".join(parts), reply_markup=markup)
    if trx_warn_text is not None:
        await message.answer(trx_warn_text, reply_markup=trx_warn_markup)


# --- фон: whitelist и приветствия ---


async def _welcome_newcomers(bot: Bot) -> None:
    """Пишет тем, кого добавили в whitelist после того, как они писали боту."""
    newcomers = (config.ALLOWED_USER_IDS & users.seen()) - users.welcomed()
    for user_id in sorted(newcomers):
        try:
            await bot.send_message(
                user_id,
                "✅ Тебя добавили в список пользователей бота!\n\n" + HELP_TEXT,
            )
            users.mark_welcomed(user_id)
            logger.info("поприветствовал нового пользователя %s", user_id)
        except Exception as exc:
            logger.warning("не смог написать пользователю %s: %r", user_id, exc)
            continue
        # Сразу настраиваем кошелёк — последовательнее, чем посреди перевода.
        if _user_wallet(user_id) is None:
            try:
                await bot.send_message(user_id, WALLET_PROMPT)
                AWAITING_WALLET.add(user_id)
            except Exception as exc:
                logger.warning("не смог спросить кошелёк у %s: %r", user_id, exc)


async def _watch_whitelist(bot: Bot) -> None:
    """Раз в минуту перечитывает ALLOWED_USER_IDS из .env — без перезапуска бота."""
    while True:
        try:
            config.reload_allowed_user_ids()
            await _welcome_newcomers(bot)
        except Exception as exc:
            logger.warning("проверка whitelist не удалась: %r", exc)
        await asyncio.sleep(config.WHITELIST_POLL_SECONDS)


@dp.errors()
async def on_error(event: ErrorEvent) -> None:
    """Ошибки — одной строкой в лог вместо простыни трейсбека в консоль."""
    error = event.exception
    if isinstance(error, TelegramNetworkError):
        logger.warning("сбой сети Telegram: %r", error)
        return
    logger.error("необработанная ошибка", exc_info=error)
    message = event.update.message if event.update else None
    if message is not None:
        try:
            await message.answer("😕 Что-то пошло не так. Попробуй ещё раз.")
        except Exception:
            pass


async def _run() -> None:
    global _checker
    _checker = TronChecker()
    bot = Bot(
        config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    whitelist_task = asyncio.create_task(_watch_whitelist(bot))
    try:
        while True:
            try:
                await dp.start_polling(bot)
                break  # штатная остановка
            except TelegramNetworkError as exc:
                logger.warning(
                    "нет связи с Telegram (%r) — попробую снова через 10 с", exc
                )
                await asyncio.sleep(10)
    finally:
        whitelist_task.cancel()
        for task, _label in WATCHERS.values():
            task.cancel()
        await _checker.aclose()


def main() -> None:
    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        level=logging.INFO,
        handlers=[logging.StreamHandler(), file_handler],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN не задан. Скопируй .env.example в .env и заполни его."
        )
    if not config.ALLOWED_USER_IDS:
        logger.warning(
            "ALLOWED_USER_IDS пуст — бот будет отказывать всем. Напиши боту, "
            "он покажет твой ID, добавь его в .env и перезапусти."
        )
    if not _service_address_configured():
        logger.warning(
            "ENERGY_SERVICE_PAYMENT_ADDRESS не задан или невалиден — добавь его "
            "в .env; в ответах будет заглушка вместо платёжного адреса."
        )

    try:
        asyncio.run(_run())
    except TelegramUnauthorizedError:
        raise SystemExit(
            "Telegram не принял токен — проверь TELEGRAM_BOT_TOKEN в .env."
        )
    except KeyboardInterrupt:
        pass
    logger.info("Бот остановлен.")


if __name__ == "__main__":
    main()
