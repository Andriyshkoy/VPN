from aiogram.fsm.state import State, StatesGroup


class CreateConfig(StatesGroup):
    choosing_server = State()
    entering_name = State()


class RenameConfig(StatesGroup):
    entering_name = State()


class TopUpTelegram(StatesGroup):
    waiting_amount = State()


class AdminUserSearch(StatesGroup):
    waiting_query = State()


class AdminBalanceChange(StatesGroup):
    waiting_amount = State()


class AdminServerCreate(StatesGroup):
    name = State()
    ip = State()
    port = State()
    host = State()
    location = State()
    api_key = State()
    cost = State()


class AdminBillingSettings(StatesGroup):
    waiting_value = State()
