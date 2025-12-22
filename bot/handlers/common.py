from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .base import billing_service, get_or_create_user, is_admin, router
from .keyboards import main_menu_kb

__all__ = ["cmd_start", "cmd_help", "cmd_how_to_use"]


@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject | None = None) -> None:
    ref_id = command.args if command and command.args else None
    user = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        ref_id=ref_id,
    )
    if not user:
        await message.answer(
            "Регистрация доступна только по валидной реферальной ссылке.\n"
            "Попросите приглашение у действующего пользователя."
        )
        return
    settings = await billing_service.get_settings()
    welcome_text = (
        "👋 <b>Добро пожаловать в VPN бот!</b>\n\n"
        "🔐 <b>Что такое OVPN?</b>\n"
        "OVPN — это конфигурационный файл для подключения к VPN через OpenVPN. "
        "Вы просто импортируете его в приложение — и подключаетесь к защищённому соединению.\n\n"
        "💡 <b>Преимущества нашего сервиса:</b>\n"
        f"• Дешевле большинства аналогов: всего {settings.config_creation_cost}₽ "
        f"за создание конфигурации и {settings.monthly_config_cost}₽ в месяц\n"
        "• Несколько локаций на выбор\n"
        "• Простое и быстрое подключение\n"
        "• Поддержка всех платформ: Windows, macOS, Linux, Android, iOS, Smart TV и ТВ-приставок\n"
        "• Управление конфигурациями прямо в Telegram\n"
    )
    await message.answer(
        welcome_text,
        parse_mode="HTML",
        reply_markup=main_menu_kb(is_admin(message.from_user.id)),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    settings = await billing_service.get_settings()
    help_text = (
        "📌 <b>Как пользоваться ботом</b>\n\n"
        "• Управление происходит через кнопки меню\n"
        "• В личном кабинете вы можете создавать и управлять конфигурациями\n"
        "• В разделе платежей можно пополнить баланс\n\n"
        "<b>Стоимость услуг:</b>\n"
        f"• создание конфигурации — {settings.config_creation_cost} рублей (списывается сразу)\n"
        f"• использование конфигурации — {settings.monthly_config_cost} рублей в месяц, списывается постепенно\n"
    )
    await message.answer(
        help_text,
        parse_mode="HTML",
        reply_markup=main_menu_kb(is_admin(message.from_user.id)),
    )


@router.message(Command("how_to_use"))
async def cmd_how_to_use(message: Message) -> None:
    await _send_how_to_use(message)


@router.message(F.text == "ℹ️ Инструкция")
async def how_to_use_button(message: Message) -> None:
    await _send_how_to_use(message)


async def _send_how_to_use(message: Message) -> None:
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
    await message.answer(
        help_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
