from aiogram.fsm.state import State, StatesGroup


class CreateConfig(StatesGroup):
    choosing_server = State()
    entering_name = State()
