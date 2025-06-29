from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .base import get_or_create_user, router

__all__ = ["cmd_start", "cmd_help", "cmd_how_to_use", "cmd_balance"]


@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject | None = None) -> None:
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
        "• /referrals — реферальная программа\n\n"
        "👥 Приглашайте друзей и получайте бонусы — используйте /referrals для ссылки\n\n"
        "ℹ️ Для подробной информации используйте команду /help"
    )
    await message.answer(welcome_text, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    help_text = (
        "📌 <b>Список доступных команд:</b>\n\n"
        "• /start - начало работы с ботом\n"
        "• /how_to_use - инструкция по подключению к VPN\n"
        "• /help - показать эту справку\n"
        "• /balance - проверить ваш текущий баланс\n"
        "• /topup - информация о пополнении баланса\n"
        "• /configs - список ваших активных VPN конфигураций\n"
        "• /create_config - создать новую VPN конфигурацию\n"
        "• /referrals - реферальная программа\n\n"
        "<b>Стоимость услуг:</b>\n"
        "• создание конфигурации — 10 рублей (списывается сразу)\n"
        "• использование конфигурации — 50 рублей в месяц, списывается постепенно\n\n"
        "<b>Как пользоваться ботом:</b>\n"
        "1. Проверьте баланс с помощью /balance\n"
        "2. При необходимости пополните баланс через /topup\n"
        "3. Создайте конфигурацию используя /create_config\n"
        "4. Скачайте .ovpn файл и импортируйте его в ваш VPN клиент\n"
        "5. Просматривайте свои конфигурации через /configs"
    )
    await message.answer(help_text, parse_mode="HTML")


@router.message(Command("how_to_use"))
async def cmd_how_to_use(message: Message) -> None:
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
async def cmd_balance(message: Message) -> None:
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(f"Ваш баланс: {user.balance}")
