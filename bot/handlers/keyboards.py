from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def main_menu_kb(is_admin: bool) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="👤 Личный кабинет")],
        [KeyboardButton(text="💳 Пополнить баланс")],
        [KeyboardButton(text="👥 Рефералы")],
        [KeyboardButton(text="ℹ️ Инструкция")],
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="🛠 Админка")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users")],
            [InlineKeyboardButton(text="🖥 Серверы", callback_data="admin:servers")],
            [InlineKeyboardButton(text="⚙️ Тарифы и бонусы", callback_data="admin:settings")],
        ]
    )


def cabinet_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Мои конфиги", callback_data="cfg:list:0")],
            [InlineKeyboardButton(text="➕ Новый конфиг", callback_data="cfg:create")],
            [InlineKeyboardButton(text="📑 Детализация счета", callback_data="tx:summary:0:all")],
        ]
    )


def config_actions_kb(
    *,
    config_id: int,
    suspended: bool,
    include_admin_back: bool,
) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Скачать", callback_data=f"dl:{config_id}")],
        [InlineKeyboardButton(text="Переименовать", callback_data=f"rn:{config_id}")],
        [InlineKeyboardButton(text="Удалить", callback_data=f"del:{config_id}")],
    ]
    if suspended:
        buttons.insert(0, [InlineKeyboardButton(text="Возобновить", callback_data=f"uns:{config_id}")])
    else:
        buttons.insert(0, [InlineKeyboardButton(text="Приостановить", callback_data=f"sus:{config_id}")])
    back_text = "🛠 Админка" if include_admin_back else "👤 Личный кабинет"
    back_cb = "admin:home" if include_admin_back else "cabinet:home"
    buttons.append([InlineKeyboardButton(text=back_text, callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_user_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Конфиги", callback_data=f"cfg:list:{user_id}:0")],
            [InlineKeyboardButton(text="➕ Пополнить", callback_data=f"admin:user:{user_id}:topup")],
            [InlineKeyboardButton(text="➖ Списать", callback_data=f"admin:user:{user_id}:withdraw")],
            [InlineKeyboardButton(text="📑 История", callback_data=f"tx:summary:0:all:{user_id}")],
            [InlineKeyboardButton(text="⬅️ Админка", callback_data="admin:home")],
        ]
    )


def admin_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Создание конфига",
                    callback_data="admin:settings:config_creation_cost",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Месячный тариф",
                    callback_data="admin:settings:monthly_config_cost",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Бонус 1-го депозита",
                    callback_data="admin:settings:referral_first_deposit_bonus_pct",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Бонус повторных депозитов",
                    callback_data="admin:settings:referral_recurring_bonus_pct",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Админка", callback_data="admin:home")],
        ]
    )


def transactions_filters_kb(
    *,
    page: int,
    current_filter: str,
    has_prev: bool,
    has_next: bool,
    user_id: int | None = None,
) -> InlineKeyboardMarkup:
    def make_cb(page_value: int, filter_key: str) -> str:
        base = f"tx:summary:{page_value}:{filter_key}"
        if user_id is not None:
            return f"{base}:{user_id}"
        return base

    filter_buttons = [
        InlineKeyboardButton(
            text=("✅ Все" if current_filter == "all" else "Все"),
            callback_data=make_cb(0, "all"),
        ),
        InlineKeyboardButton(
            text=("✅ Пополнения" if current_filter == "topup" else "Пополнения"),
            callback_data=make_cb(0, "topup"),
        ),
        InlineKeyboardButton(
            text=("✅ Расходы" if current_filter == "expense" else "Расходы"),
            callback_data=make_cb(0, "expense"),
        ),
        InlineKeyboardButton(
            text=("✅ Бонусы" if current_filter == "bonus" else "Бонусы"),
            callback_data=make_cb(0, "bonus"),
        ),
    ]

    nav_buttons = []
    if has_prev:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=make_cb(page - 1, current_filter)))
    if has_next:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=make_cb(page + 1, current_filter)))

    back_text = "🛠 Админка" if user_id is not None else "👤 Личный кабинет"
    back_cb = "admin:home" if user_id is not None else "cabinet:home"

    inline_keyboard = [
        filter_buttons[:2],
        filter_buttons[2:],
    ]
    if nav_buttons:
        inline_keyboard.append(nav_buttons)
    inline_keyboard.append([InlineKeyboardButton(text=back_text, callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
