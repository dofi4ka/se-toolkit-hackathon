from aiogram import Router

from sous_chef.handlers.bot_handlers import router as bot_router


def setup_routers() -> Router:
    root = Router()
    root.include_router(bot_router)
    return root
