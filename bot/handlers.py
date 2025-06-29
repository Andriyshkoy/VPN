from __future__ import annotations

import os
import tempfile
import uuid

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
)

from core.config import settings
from core.db.unit_of_work import uow
from core.exceptions import InsufficientBalanceError, ServiceError
from core.services import (
    BillingService,
    ConfigService,
    ServerService,
    TelegramPayService,
    UserService,
)

from .states import CreateConfig, RenameConfig

router = Router()

user_service = UserService(uow)
server_service = ServerService(uow)
config_service = ConfigService(uow)
billing_service = BillingService(uow, per_config_cost=settings.per_config_cost)

# How many referrals to show per page
REFERRALS_PER_PAGE = 10


async def get_or_create_user(tg_id: int, username: str, ref_id: str | None = None):
    ref_id = int(ref_id) if ref_id and ref_id.isdigit() else None
    return await user_service.register(tg_id, username=username, ref_id=ref_id)


async def setup_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Начать работу с ботом"),
        BotCommand(command="help", description="Показать справку"),
        BotCommand(command="topup", description="Пополнить баланс"),
        BotCommand(command="how_to_use", description="Как установить VPN"),
        BotCommand(command="balance", description="Проверить баланс"),
        BotCommand(command="configs", description="Список конфигураций"),
        BotCommand(command="create_config", description="Создать новую конфигурацию"),
        BotCommand(command="refferals", description="Реферальная программа"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())


async def _send_referrals(target: Message | CallbackQuery, user_id: int, tg_id: int, page: int = 0) -> None:
    """Send a paginated list of referrals to either a message or callback."""
    total = await user_service.count_referrals(user_id)
    offset = page * REFERRALS_PER_PAGE
    referrals = await user_service.get_referrals(user_id, limit=REFERRALS_PER_PAGE, offset=offset)

    text = (
        "📊 <b>Ваши рефералы</b>\n\n"
        "Приглашайте друзей и получайте бонусы!\n"
        f"Ваша реферальная ссылка:\n<code>https://t.me/andriyshkoy_devbot?start={tg_id}</code>\n\n"
    )

    if not referrals:
        text += "У вас нет рефералов."
        markup = None
    else:
        text += f"Всего: {total}\n\n"
        for ref in referrals:
            name = f"@{ref.username}" if ref.username else f"ID: {ref.tg_id}"
            text += f"• {name}\n"

        buttons = []
        if page > 0:
            buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"refs:{page-1}"))
        if offset + REFERRALS_PER_PAGE < total:
            buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"refs:{page+1}"))
        markup = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

    send_method = target.answer if isinstance(target, Message) else target.message.edit_text
    await send_method(text, reply_markup=markup, parse_mode="HTML")
    if isinstance(target, CallbackQuery):
        await target.answer()


@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject | None = None):
    ref_id = command.args if command and command.args else None
    await get_or_create_user(message.from_user.id, message.from_user.username, ref_id=ref_id)
    welcome_text = (
        "👋 <b>Добро пожаловать в VPN бот!</b>\n\n"
        "🔐 <b>Что такое OVPN?</b>\n"
        "OVPN — это конфигурационный файл для подключения к VPN через OpenVPN. "
        "Вы просто импортируете его в приложение — и подключаетесь к защищённому соединению.\n\n"
        "💡 <b>Преимущества нашего сервиса:</b>\n"
        "• Дешевле большинства аналогов: всего 10₽ за создание конфигурации и 50₽ в месяц\n"
        "• Несколько локаций на выбор\n"
        "• Простое и быстрое подключение\n"
        "• Поддержка всех платформ: Windows, macOS, Linux, Android, iOS, Smart TV и ТВ-приставок\n"
        "• Управление конфигурациями прямо в Telegram\n\n"
        "📌 <b>Основные команды:</b>\n"
        "• /create_config — создать новую VPN конфигурацию\n"
        "• /configs — список ваших конфигураций\n"
        "• /balance — проверить баланс\n"
        "• /topup — пополнить баланс\n"
        "• /how_to_use — инструкция по подключению к VPN\n"
        "• /refferals — реферальная программа\n\n"
        "👥 Приглашайте друзей и получайте бонусы — используйте /refferals для ссылки\n"
        "ℹ️ Для подробной информации используйте команду /help"
    )
    await message.answer(welcome_text, parse_mode="HTML")


@router.message(Command("refferals"))
async def cmd_refferals(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    await _send_referrals(message, user.id, message.from_user.id, page=0)


@router.callback_query(F.data.startswith("refs:"))
async def paginate_referrals(callback: CallbackQuery):
    try:
        page = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректные данные", show_alert=True)
        return
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    await _send_referrals(callback, user.id, callback.from_user.id, page=page)


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📌 <b>Список доступных команд:</b>\n\n"
        "• /start - начало работы с ботом\n"
        "• /how_to_use - инструкция по подключению к VPN\n"
        "• /help - показать эту справку\n"
        "• /balance - проверить ваш текущий баланс\n"
        "• /topup - информация о пополнении баланса\n"
        "• /configs - список ваших активных VPN конфигураций\n"
        "• /create_config - создать новую VPN конфигурацию\n"
        "• /refferals - реферальная программа\n\n"
        "<b>Стоимость услуг:</b>\n"
        "• создание конфигурации \u2014 10 рублей (списывается сразу)\n"
        "• использование конфигурации \u2014 50 рублей в месяц, списывается постепенно\n\n"
        "<b>Как пользоваться ботом:</b>\n"
        "1. Проверьте баланс с помощью /balance\n"
        "2. При необходимости пополните баланс через /topup\n"
        "3. Создайте конфигурацию используя /create_config\n"
        "4. Скачайте .ovpn файл и импортируйте его в ваш VPN клиент\n"
        "5. Просматривайте свои конфигурации через /configs"
    )
    await message.answer(help_text, parse_mode="HTML")


@router.message(Command("how_to_use"))
async def cmd_how_to_use(message: Message):
    help_text = (
        "🔐 <b>Подключение к VPN: пошаговая инструкция</b>\n\n"
        "Для начала работы:\n"
        "1. Установите VPN-клиент\n"
        "2. Импортируйте конфигурацию\n"
        "3. Активируйте подключение\n\n"

        "▫️▫️▫️\n\n"
        "🖥 <b>Windows</b>\n"
        "1. Скачайте <a href=\"https://openvpn.net/client-connect-vpn-for-windows/\">OpenVPN Connect</a>\n"
        "2. Запустите приложение → <b>«+ Import Profile»</b>\n"
        "3. Выберите полученный <code>.ovpn</code>-файл\n"
        "4. Нажмите <b>Connect</b>\n\n"

        "▫️▫️▫️\n\n"
        "🍎 <b>macOS</b>\n"
        "1. Установите <a href=\"https://tunnelblick.net/\">Tunnelblick</a>\n"
        "2. Дважды кликните на <code>.ovpn</code>-файл → <b>«Import»</b>\n"
        "3. Подключайтесь через иконку в строке меню\n\n"

        "▫️▫️▫️\n\n"
        "📱 <b>Android</b>\n"
        "1. Установите <a href=\"https://play.google.com/store/apps/details?id=net.openvpn.openvpn\">OpenVPN Connect</a>\n"
        "2. В приложении: <b>File</b> → выберите <code>.ovpn</code>\n"
        "3. Нажмите <b>Connect</b>\n\n"

        "▫️▫️▫️\n\n"
        "📱 <b>iOS (iPhone/iPad)</b>\n"
        "1. Установите <a href=\"https://apps.apple.com/app/openvpn-connect/id590379981\">OpenVPN Connect</a>\n"
        "2. Сохраните <code>.ovpn</code>-файл в приложении <b>Файлы</b>:\n"
        "   • В Telegram: нажмите на файл → <b>•••</b> → <b>Share</b> → <b>Save to Files</b>\n"
        "3. Откройте <b>OpenVPN Connect</b> → <b>+</b> → выберите файл из раздела <b>On My iPhone</b>\n"
        "4. Нажмите <b>Add</b> → <b>Connect</b>\n\n"

        "▫️▫️▫️\n\n"
        "🆘 <b>Возникли сложности?</b>\n"
        "Пишите <a href=\"https://t.me/andriyshkoy\">@andriyshkoy</a> — поможем!"
    )

    await message.answer(help_text, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("balance"))
async def cmd_balance(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(f"Ваш баланс: {user.balance}")


@router.message(Command("topup"))
async def cmd_topup(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🪙 Пополнить криптовалютой",
                    callback_data="pay:crypto"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Оплатить картой (Telegram Pay)",
                    callback_data="pay:telegram",
                )
            ],
        ]
    )

    await message.answer(
        "💸 <b>Пополнение баланса</b>\n\n"
        "Выберите удобный способ оплаты:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@router.callback_query(lambda c: c.data == "pay:crypto")
async def pay_crypto(callback: CallbackQuery):
    await callback.message.answer(
        "Оплата криптовалютой скоро появится!"
    )
    await callback.answer()

AVAILABLE_AMOUNTS = [100, 200, 300, 500]


@router.callback_query(lambda c: c.data == "pay:telegram")
async def pay_telegram(callback: CallbackQuery, state: FSMContext, bot: Bot):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{amt} ₽", callback_data=f"topup:{amt}")]
            for amt in AVAILABLE_AMOUNTS
        ]
    )
    await callback.message.answer(
        "Выберите сумму пополнения:",
        reply_markup=keyboard
    )
    await callback.answer()


@router.callback_query(F.data.startswith("topup:"))
async def got_topup_amount(callback: CallbackQuery, bot: Bot):
    try:
        amount = float(callback.data.split(":")[1])
        assert amount in AVAILABLE_AMOUNTS
    except (ValueError, AssertionError):
        await callback.answer("Некорректная сумма.", show_alert=True)
        return

    service = TelegramPayService(bot, settings.telegram_pay_token)
    await service.send_invoice(callback.message.chat.id, amount)


@router.message(Command("configs"))
async def cmd_configs(message: Message):
    """List all configs (active and suspended) with inline buttons."""
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    active = await config_service.list_active(owner_id=user.id)
    suspended = await config_service.list_suspended(owner_id=user.id)
    configs = active + suspended
    if not configs:
        await message.answer("У вас нет конфигураций")
        return
    buttons = []
    for cfg in configs:
        title = cfg.display_name
        if cfg.suspended:
            title += " (приостановлена)"
        buttons.append(
            [InlineKeyboardButton(text=title, callback_data=f"cfg:{cfg.id}")]
        )
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Ваши конфигурации:", reply_markup=kb)


@router.pre_checkout_query()
async def process_pre_checkout_query(pcq: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pcq.id, ok=True)


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message) -> None:
    qty = message.successful_payment.total_amount / 100
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    await billing_service.top_up(user.id, qty)
    await message.answer(
        f"✅ Платёж успешно завершён! Баланс пополнен на {qty} рублей."
    )


@router.message(Command("create_config"))
async def cmd_create_config(message: Message, state: FSMContext):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    servers = await server_service.list()
    if not servers:
        await message.answer("Нет доступных серверов")
        return
    buttons = [
        [
            InlineKeyboardButton(
                text=" ".join([s.location, s.name]), callback_data=f"server:{s.id}"
            )
        ]
        for s in servers
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(
        "Выберите сервер для новой конфигурации.\n\n"
        "Стоимость создания конфигурации \u2014 10 рублей (списывается сразу). "
        "Ежемесячная плата за использование составляет 50 рублей и списывается постепенно.",
        reply_markup=kb,
    )
    await state.set_state(CreateConfig.choosing_server)


@router.callback_query(lambda c: c.data and c.data.startswith("server:"))
async def choose_server(callback: CallbackQuery, state: FSMContext):
    server_id = int(callback.data.split(":", 1)[1])
    await state.update_data(server_id=server_id)
    await callback.message.answer(
        "📝 *Введите название для конфигурации*\n\n"
        "Это имя будет использоваться для идентификации вашей конфигурации в VPN-клиенте, "
        "а также будет отображаться в списке ваших конфигураций.\n\n"
        "✏️ Вы всегда сможете изменить его позже."
    )
    await state.set_state(CreateConfig.entering_name)
    await callback.answer()


@router.message(CreateConfig.entering_name)
async def got_name(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    server_id = data.get("server_id")
    if not server_id:
        await message.answer("Сервер не выбран")
        await state.clear()
        return
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    unique_name = uuid.uuid4().hex
    try:
        cfg = await billing_service.create_paid_config(
            server_id=server_id,
            owner_id=user.id,
            name=unique_name,
            display_name=message.text,
            creation_cost=settings.config_creation_cost,
        )
    except InsufficientBalanceError:
        await message.answer("Недостаточно средств. Пополните баланс")
        await state.clear()
        return
    except ServiceError:
        await message.answer("Произошла ошибка. Попробуйте позже")
        await state.clear()
        return
    try:
        content = await config_service.download_config(cfg.id)
    except ServiceError:
        await message.answer("Произошла ошибка. Попробуйте позже")
        await state.clear()
        return
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        await bot.send_document(
            message.chat.id,
            FSInputFile(tmp_path, filename=f"{message.text}.ovpn"),
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    await message.answer("Конфигурация создана")
    await message.answer(
        "Вы можете управлять конфигурацией через команду /configs или "
        "Ознакомиться с инструкцией по подключению к VPN с помощью команды /how_to_use."
    )
    await state.clear()


@router.callback_query(lambda c: c.data and c.data.startswith("cfg:"))
async def show_config(callback: CallbackQuery):
    config_id = int(callback.data.split(":", 1)[1])
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    cfg = await config_service.get(config_id)
    if not cfg or cfg.owner_id != user.id:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    server = await server_service.get(cfg.server_id)
    text = (
        f"<b>{cfg.display_name}</b>\n"
        f"Сервер: {server.name} ({server.location})\n"
        f"Статус: {'приостановлена' if cfg.suspended else 'активна'}"
    )
    buttons = []
    # if cfg.suspended:
    #     buttons.append([InlineKeyboardButton(text="Возобновить", callback_data=f"uns:{cfg.id}")])
    # else:
    #     buttons.append([InlineKeyboardButton(text="Приостановить", callback_data=f"sus:{cfg.id}")])
    buttons.append(
        [InlineKeyboardButton(text="Удалить", callback_data=f"del:{cfg.id}")]
    )
    buttons.append([InlineKeyboardButton(text="Скачать", callback_data=f"dl:{cfg.id}")])
    buttons.append(
        [InlineKeyboardButton(text="Переименовать", callback_data=f"rn:{cfg.id}")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("sus:"))
async def suspend_config_cb(callback: CallbackQuery):
    config_id = int(callback.data.split(":", 1)[1])
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    cfg = await config_service.get(config_id)
    if not cfg or cfg.owner_id != user.id:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    try:
        await config_service.suspend_config(config_id)
        await callback.message.answer("Конфигурация приостановлена")
    except ServiceError:
        await callback.message.answer("Произошла ошибка. Попробуйте позже")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("uns:"))
async def unsuspend_config_cb(callback: CallbackQuery):
    config_id = int(callback.data.split(":", 1)[1])
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    if user.balance <= 0:
        await callback.message.answer("Недостаточно средств. Пополните баланс")
        await callback.answer()
        return
    cfg = await config_service.get(config_id)
    if not cfg or cfg.owner_id != user.id:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    try:
        await config_service.unsuspend_config(config_id)
        await callback.message.answer("Конфигурация возобновлена")
    except ServiceError:
        await callback.message.answer("Произошла ошибка. Попробуйте позже")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("del:"))
async def delete_config_cb(callback: CallbackQuery):
    config_id = int(callback.data.split(":", 1)[1])
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    cfg = await config_service.get(config_id)
    if not cfg or cfg.owner_id != user.id:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    try:
        await config_service.revoke_config(config_id)
        await callback.message.answer("Конфигурация удалена")
    except ServiceError:
        await callback.message.answer("Произошла ошибка. Попробуйте позже")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("dl:"))
async def download_config_cb(callback: CallbackQuery, bot: Bot):
    config_id = int(callback.data.split(":", 1)[1])
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    cfg = await config_service.get(config_id)
    if not cfg or cfg.owner_id != user.id:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    try:
        content = await config_service.download_config(config_id)
    except ServiceError:
        await callback.message.answer("Произошла ошибка. Попробуйте позже")
        await callback.answer()
        return
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        await bot.send_document(
            callback.message.chat.id,
            FSInputFile(tmp_path, filename=f"{cfg.display_name}.ovpn"),
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("rn:"))
async def rename_config_cb(callback: CallbackQuery, state: FSMContext):
    config_id = int(callback.data.split(":", 1)[1])
    await state.update_data(config_id=config_id)
    await callback.message.answer("Введите новое название")
    await state.set_state(RenameConfig.entering_name)
    await callback.answer()


@router.message(RenameConfig.entering_name)
async def got_new_name(message: Message, state: FSMContext):
    data = await state.get_data()
    config_id = data.get("config_id")
    if not config_id:
        await message.answer("Конфигурация не выбрана")
        await state.clear()
        return
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    cfg = await config_service.get(config_id)
    if not cfg or cfg.owner_id != user.id:
        await message.answer("Конфигурация не найдена")
        await state.clear()
        return
    try:
        await config_service.rename_config(config_id, message.text)
        await message.answer("Конфигурация переименована")
    except ServiceError:
        await message.answer("Произошла ошибка. Попробуйте позже")
    await state.clear()
