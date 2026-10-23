"""Entrypoint. Long polling by default; webhook mode when WEBHOOK_BASE_URL is set.

Run locally:  python -m app.main
"""
from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from app.config import Settings, load_settings
from app.crypto import KeyBox
from app.db import Database
from app.handlers import admin as admin_handlers
from app.handlers import dm as dm_handlers
from app.handlers import group as group_handlers
from app.handlers import v2 as v2_handlers
from app.handlers import settings as settings_handlers
from app.handlers import mod_v2 as mod_v2_handlers
from app.handlers import federate as federate_handlers
from app.handlers import fallback as fallback_handlers
from app.handlers import byod_dm as byod_dm_handlers
from app.handlers import rose as rose_handlers
from app.handlers import rose_b as rose_b_handlers
from app.handlers import rose_c as rose_c_handlers
from app.scheduler import run_scheduler

log = logging.getLogger("assistant-bot")

PRIVATE_COMMANDS = [
    BotCommand(command="start", description="open the bot / manage your groups"),
    BotCommand(command="settings", description="group settings (dm only)"),
    BotCommand(command="help", description="all commands"),
    BotCommand(command="privacy", description="privacy policy"),
]
ADMIN_COMMANDS = [
    BotCommand(command="stats", description="usage across all groups (super admin)"),
    BotCommand(command="groups", description="list connected groups (super admin)"),
    BotCommand(command="heat", description="usage heat map (super admin)"),
    BotCommand(command="health", description="system health (super admin)"),
    BotCommand(command="disconnect", description="disconnect a group (super admin)"),
    BotCommand(command="bangroup", description="ban a group (super admin)"),
    BotCommand(command="unbangroup", description="unban a group (super admin)"),
    BotCommand(command="broadcast", description="broadcast to all groups (super admin)"),
]

GROUP_COMMANDS = [
    BotCommand(command="ask", description="ask the ai a question"),
    BotCommand(command="summary", description="summarize recent chat"),
    BotCommand(command="points", description="your points"),
    BotCommand(command="lb", description="leaderboard"),
    BotCommand(command="rules", description="group rules"),
    BotCommand(command="help", description="all commands"),
    BotCommand(command="privacy", description="privacy policy"),
    BotCommand(command="antiflood", description="flood limit (admins)"),
    BotCommand(command="antiraid", description="raid protection (admins)"),
    BotCommand(command="approval", description="join approval (admins)"),
    BotCommand(command="blocklist", description="blocked words (admins)"),
    BotCommand(command="filter", description="auto-reply filter (admins)"),
    BotCommand(command="filters", description="list filters"),
    BotCommand(command="stop", description="remove a filter (admins)"),
    BotCommand(command="save", description="save a note (admins)"),
    BotCommand(command="notes", description="list notes"),
    BotCommand(command="get", description="get a note"),
    BotCommand(command="pin", description="pin the replied message (admins)"),
    BotCommand(command="unpin", description="unpin (admins)"),
    BotCommand(command="pinned", description="show pinned message"),
    BotCommand(command="antipin", description="auto-unpin member pins (admins)"),
    BotCommand(command="report", description="report a message to admins"),
    BotCommand(command="reports", description="reports on/off (admins)"),
    BotCommand(command="logchannel", description="log channel (admins)"),
    BotCommand(command="disable", description="disable a command (admins)"),
    BotCommand(command="enable", description="enable a command (admins)"),
    BotCommand(command="disabled", description="list disabled commands"),
    BotCommand(command="lang", description="bot language (admins)"),
    BotCommand(command="export", description="export settings to dm (admins)"),
    BotCommand(command="import", description="import settings (admins)"),
    BotCommand(command="goodbye", description="goodbye on/off (admins)"),
    BotCommand(command="setgoodbye", description="set goodbye text (admins)"),
    BotCommand(command="resetgoodbye", description="reset goodbye text (admins)"),
    BotCommand(command="cleangoodbye", description="clean last goodbye (admins)"),
    BotCommand(command="stopall", description="remove all filters (admins)"),
    BotCommand(command="disableadmin", description="disable admin commands (admins)"),
    BotCommand(command="disableall", description="disable member commands (admins)"),
    BotCommand(command="log", description="log category on (admins)"),
    BotCommand(command="nolog", description="log category off (admins)"),
    BotCommand(command="logcategories", description="log categories"),
    BotCommand(command="clearall", description="delete all notes (admins)"),
    BotCommand(command="privatenotes", description="notes to dm (admins)"),
    BotCommand(command="runs", description="a random reply"),
    BotCommand(command="id", description="show ids"),
    BotCommand(command="info", description="user info"),
    BotCommand(command="limits", description="bot limits"),
    BotCommand(command="markdownhelp", description="markdown and fillings help"),
    BotCommand(command="purgey", description="purge last n messages (admins)"),
    BotCommand(command="purgeconfirm", description="purge confirmation (admins)"),
    BotCommand(command="purgefrom", description="mark purge start (admins)"),
    BotCommand(command="purgeto", description="purge to marked message (admins)"),
    BotCommand(command="pinloud", description="pins notify (admins)"),
    BotCommand(command="pinmute", description="pins silent (admins)"),
    BotCommand(command="unpinall", description="unpin all (admins)"),
    BotCommand(command="antichannelpin", description="undo channel pins (admins)"),
    BotCommand(command="channelsettings", description="pin and channel settings (admins)"),
    BotCommand(command="dbanned", description="ban deleted accounts (admins)"),
    BotCommand(command="setrules", description="set rules (admins)"),
    BotCommand(command="resetrules", description="clear rules (admins)"),
    BotCommand(command="privaterules", description="rules to dm (admins)"),
    BotCommand(command="setrulesbutton", description="rules button text (admins)"),
    BotCommand(command="resetrulesbutton", description="remove rules button (admins)"),
    BotCommand(command="settopic", description="default topic (admins)"),
    BotCommand(command="gettopic", description="default topic"),
    BotCommand(command="newtopic", description="create topic (admins)"),
    BotCommand(command="renametopic", description="rename topic (admins)"),
    BotCommand(command="closetopic", description="close topic (admins)"),
    BotCommand(command="reopentopic", description="reopen topic (admins)"),
    BotCommand(command="deletetopic", description="delete topic (admins)"),
    BotCommand(command="swarn", description="silent warn (admins)"),
    BotCommand(command="rwarn", description="remove a warning (admins)"),
    BotCommand(command="resetwarns", description="reset a user warns (admins)"),
    BotCommand(command="resetallwarns", description="reset all warns (admins)"),
    BotCommand(command="warns", description="warnings count"),
    BotCommand(command="setwarnaction", description="warn action (admins)"),
    BotCommand(command="warnlimit", description="warn limit (admins)"),
    BotCommand(command="warntime", description="warn expiry (admins)"),
    BotCommand(command="cleanservice", description="clean service messages (admins)"),
    BotCommand(command="keepservice", description="keep service messages (admins)"),
    BotCommand(command="cleanservicetypes", description="cleaned service types"),
    BotCommand(command="cleancommand", description="clean a command (admins)"),
    BotCommand(command="keepcommand", description="keep a command (admins)"),
    BotCommand(command="cleancommandtypes", description="cleaned commands"),
    BotCommand(command="reset", description="reset group (admins)"),
    BotCommand(command="anonadmin", description="anonymous admin (admins)"),
    BotCommand(command="adminerror", description="admin error notices (admins)"),
]


def build_dispatcher(db: Database, cfg: Settings, keys: KeyBox) -> Dispatcher:
    dp = Dispatcher(db=db, cfg=cfg, keys=keys)
    # Order matters: super-admin DM commands, then DM menu, then group handlers.
    dp.include_router(admin_handlers.router)
    dp.include_router(dm_handlers.router)
    dp.include_router(group_handlers.router)
    dp.include_router(v2_handlers.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(mod_v2_handlers.router)
    dp.include_router(federate_handlers.router)
    dp.include_router(fallback_handlers.router)
    dp.include_router(byod_dm_handlers.router)
    dp.include_router(rose_handlers.dm_router)
    dp.include_router(rose_handlers.router)
    dp.include_router(rose_b_handlers.router)
    dp.include_router(rose_c_handlers.router)
    dp.include_router(rose_c_handlers.dm_router)
    # disabled commands and clean-service/command rules apply to every group message, all routers
    dp.message.outer_middleware(rose_handlers.DisabledCommands())
    dp.message.outer_middleware(rose_c_handlers.CleanMiddleware())
    return dp


async def run() -> None:
    cfg = load_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db = Database(cfg.database_url)
    await db.init()
    keys = KeyBox(cfg.encryption_key)
    bot = Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(db, cfg, keys)

    # Bot menu commands. Failures here (rate limits, no rights) must not stop the bot.
    try:
        from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
        await bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
        # super admins get the admin menu in their private chat on top of the normal one
        from aiogram.types import BotCommandScopeChat
        for admin_id in cfg.super_admin_ids:
            try:
                await bot.set_my_commands(
                    ADMIN_COMMANDS + PRIVATE_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id, type="chat")
                )
            except TelegramBadRequest as exc:
                log.warning("could not set admin commands for %s: %s", admin_id, exc)
    except TelegramBadRequest as exc:
        log.warning("could not set bot commands: %s", exc)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    scheduler_task = asyncio.create_task(run_scheduler(bot, db, stop))
    allowed = ["message", "callback_query", "chat_member", "my_chat_member"]

    try:
        if cfg.webhook_base_url:
            # Webhook mode (Render, Railway, Koyeb, any HTTPS host).
            path = "/telegram/webhook"
            secret = cfg.webhook_secret or None
            await bot.set_webhook(
                f"{cfg.webhook_base_url}{path}", secret_token=secret,
                allowed_updates=allowed, drop_pending_updates=False,
            )
            app = web.Application()
            SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=path)
            app.router.add_get("/healthz", lambda r: web.json_response({"ok": True}))
            setup_application(app, dp, bot=bot)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
            log.info("webhook mode on port %s", cfg.port)
            await stop.wait()
            await runner.cleanup()
        else:
            # Long polling (local, VPS, or any host without a public URL).
            await bot.delete_webhook(drop_pending_updates=False)
            log.info("polling mode")
            polling = asyncio.create_task(dp.start_polling(bot, allowed_updates=allowed, handle_signals=False))
            await stop.wait()
            polling.cancel()
    finally:
        stop.set()
        scheduler_task.cancel()
        await bot.session.close()
        await db.close()
        log.info("bye")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
