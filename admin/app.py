from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import settings
from core.db.unit_of_work import uow
from core.services import BillingService, ConfigService, ServerService, UserService

from . import exception_handlers
from .routers import auth as auth_router
from .routers import configs as config_router
from .routers import servers as server_router
from .routers import users as user_router

app = FastAPI()

server_service = ServerService(uow)
config_service = ConfigService(uow)
user_service = UserService(uow)
billing_service = BillingService(uow, per_config_cost=settings.per_config_cost)

origins = [
    "http://localhost",
    "http://localhost:5173",
    "https://andriyshkoy.ru",
    "https://vpn.andriyshkoy.ru",
    "https://admin.vpn.andriyshkoy.ru",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register exception handlers
exception_handlers.register_exception_handlers(app)

# Include routers
app.include_router(auth_router.router)
app.include_router(server_router.router)
app.include_router(user_router.router)
app.include_router(config_router.router)
