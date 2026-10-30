"""Telegram mirror bot entry point."""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import signal
import time
from pathlib import Path
from typing import Optional

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from mirror.config import Settings, load_settings
from mirror.jobs import Job, JobManager, new_job
from mirror.state import load_state, save_state
from mirror.uploaders import get_uploader
from mirror.utils import (
    extract_url,
    human_size,
    progress_bar,
    sanitize_filename,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- UI
def _authorized(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    if user is None:
        return False
    return not settings.allowed_user_ids or user.id in settings.allowed_user_ids


async def _deny(update: Update, settings: Settings) -> None:
    user = update.effective_user
    if not user:
        return
    text = (
        "This bot is private.\n"
        f"Your Telegram ID is <code>{user.id}</code> — add it to ALLOWED_USER_IDS to get access."
    )
    if update.callback_query:
        await update.callback_query.answer("Not authorized.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


def _cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Cancel job", callback_data=f"cancel:{job_id}")]]
    )


def render_job(job: Job) -> str:
    snap = job.snapshot()
    titles = {
        "queued": "Queued",
        "resolving": "Resolving link",
        "downloading": "Downloading",
        "uploading": "Uploading",
        "done": "Done",
        "failed": "Failed",
        "cancelled": "Cancelled",
    }
    lines = [f"<b>{titles.get(snap['stage'], snap['stage'].title())}</b> · <code>{snap['id']}</code>"]

    if snap["status"] == "queued":
        lines.append("Waiting for a free worker…")
    if snap["percent"] is not None:
        lines.append(f"<code>{progress_bar(snap['percent'])}</code> {snap['percent']:.0f}%")
    if snap["detail"]:
        lines.append(html.escape(str(snap["detail"])))
    if snap["extras"]:
        lines.append(f"<i>{snap['extras']} extra file(s) will be skipped (largest is uploaded).</i>")
    lines.append(f"<b>Host:</b> {snap['host'].title()}")
    return "\n".join(lines)


def render_final(job: Job) -> str:
    snap = job.snapshot()
    if snap["status"] == "done" and snap["result"]:
        result = snap["result"]
        lines = [
            "<b>✅ Mirror complete</b>",
            f"<b>File:</b> {html.escape(snap['file_name'] or 'file')}",
            f"<b>Size:</b> {human_size(snap['file_size'])}",
            f"<b>Host:</b> {html.escape(result.host)}",
        ]
        if snap["extras"]:
            lines.append(f"<i>{snap['extras']} extra file(s) were skipped.</i>")
        url = html.escape(result.url, quote=True)
        lines.append(f'<b>Link:</b> <a href="{url}">{url}</a>')
        return "\n".join(lines)

    if snap["status"] == "cancelled":
        return f"<b>⏹ Cancelled</b> · <code>{snap['id']}</code>"

    reason = snap["error"] or "Unknown error."
    return (
        f"<b>❌ Mirror failed</b> · <code>{snap['id']}</code>\n"
        f"<b>Reason:</b> {html.escape(str(reason))}"
    )


# ---------------------------------------------------------------------- helpers
async def _start_job(
    context: ContextTypes.DEFAULT_TYPE,
    job: Job,
    message: Optional[Message],
) -> None:
    if not context.application.bot_data.get("accepting_jobs", True):
        if job.torrent_path:
            job.torrent_path.unlink(missing_ok=True)
        await context.bot.send_message(
            job.chat_id,
            "The bot is finishing its current run. Send the link again in a few minutes.",
        )
        return

    manager: JobManager = context.application.bot_data["manager"]
    await manager.submit(job)

    text = render_job(job)
    status_message = message
    try:
        if status_message is None:
            status_message = await context.bot.send_message(
                job.chat_id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=_cancel_keyboard(job.id),
            )
        else:
            await status_message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=_cancel_keyboard(job.id),
            )
    except TelegramError:
        logger.warning("Could not update the status message", exc_info=True)
        try:
            status_message = await context.bot.send_message(
                job.chat_id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=_cancel_keyboard(job.id),
            )
        except TelegramError:
            logger.exception("Could not send job status message")
            return

    reporters: set[asyncio.Task] = context.application.bot_data.setdefault("reporters", set())
    task = asyncio.create_task(_report_job(context, job, status_message))
    reporters.add(task)
    task.add_done_callback(reporters.discard)


async def _report_job(
    context: ContextTypes.DEFAULT_TYPE,
    job: Job,
    message: Message,
) -> None:
    last_text = ""
    while not job.finished:
        await asyncio.sleep(4)
        text = render_job(job)
        if text == last_text:
            continue
        try:
            await message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=_cancel_keyboard(job.id),
            )
            last_text = text
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                logger.debug("Status edit failed: %s", exc)
        except TelegramError:
            logger.debug("Status message is no longer editable", exc_info=True)
            break

    await asyncio.sleep(0.4)
    final = render_final(job)
    try:
        await message.edit_text(
            final,
            parse_mode=ParseMode.HTML,
            reply_markup=None,
            disable_web_page_preview=True,
        )
    except TelegramError:
        try:
            await context.bot.send_message(
                job.chat_id,
                final,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError:
            logger.exception("Could not deliver final status for job %s", job.id)


async def _submit_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    source: str,
    source_kind: str = "url",
    torrent_path: Optional[Path] = None,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not context.application.bot_data.get("accepting_jobs", True):
        if torrent_path:
            torrent_path.unlink(missing_ok=True)
        await update.effective_message.reply_text(
            "The bot is finishing its current run. Send the link again in a few minutes."
        )
        return

    usable, reason = get_uploader("gofile", settings).available()
    if not usable:
        if torrent_path:
            torrent_path.unlink(missing_ok=True)
        await update.effective_message.reply_text(
            f"⚠️ {html.escape(reason)}", parse_mode=ParseMode.HTML
        )
        return

    job = new_job(
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        source=source,
        host="gofile",
        source_kind=source_kind,
        torrent_path=torrent_path,
    )
    await _start_job(context, job, None)


# -------------------------------------------------------------------- commands
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        await _deny(update, settings)
        return
    await update.effective_message.reply_text(
        "<b>Mirror bot</b>\n\n"
        "Send me a link, a magnet link, or a <code>.torrent</code> file. "
        "I download it and upload it to Gofile, then reply with the public link.\n\n"
        "Direct files, pages with a download button, and video sites are supported.\n\n"
        "<b>Commands</b>\n"
        "/gf &lt;link&gt; — upload to Gofile\n"
        "/mirror &lt;link&gt; — same as /gf\n"
        "/status — active jobs\n"
        "/cancel — cancel your jobs\n"
        "/id — show your Telegram ID",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    await update.effective_message.reply_text(
        f"User ID: <code>{user.id}</code>\nChat ID: <code>{chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def _cmd_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    command: str,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        await _deny(update, settings)
        return

    source = None
    for arg in context.args or []:
        found = extract_url(arg)
        if found:
            source = found
            break

    if not source:
        await update.effective_message.reply_text(
            f"Usage: <code>/{command} &lt;link&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await _submit_job(update, context, source=source)


async def cmd_mirror(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_link(update, context, command="mirror")


async def cmd_gf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_link(update, context, command="gf")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        await _deny(update, settings)
        return
    manager: JobManager = context.application.bot_data["manager"]
    jobs = manager.active_for(update.effective_user.id)
    if not jobs:
        await update.effective_message.reply_text("No active jobs.")
        return

    lines = ["<b>Active jobs</b>"]
    for job in jobs[:10]:
        snap = job.snapshot()
        stage = snap["stage"].title()
        percent = f" · {snap['percent']:.0f}%" if snap["percent"] is not None else ""
        lines.append(f"<code>{snap['id']}</code> · {stage}{percent}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        await _deny(update, settings)
        return
    manager: JobManager = context.application.bot_data["manager"]
    count = manager.cancel_user(update.effective_user.id)
    if count:
        await update.effective_message.reply_text(f"Cancelling {count} job(s)…")
    else:
        await update.effective_message.reply_text("You have no active jobs.")


# --------------------------------------------------------------------- messages
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not message.text:
        return
    # In groups, only react to commands to avoid replying to every link.
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        return
    source = extract_url(message.text)
    if not source:
        if _authorized(update, settings):
            await message.reply_text("Send me a link, a magnet link, or a .torrent file.")
        return
    if not _authorized(update, settings):
        await _deny(update, settings)
        return
    await _submit_job(update, context, source=source)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    document = message.document if message else None
    if not document:
        return

    name = document.file_name or "file.torrent"
    is_torrent = name.lower().endswith(".torrent") or document.mime_type in {
        "application/x-bittorrent",
        "application/x-bittorrent; charset=utf-8",
    }
    if not is_torrent:
        return
    if not _authorized(update, settings):
        await _deny(update, settings)
        return
    if document.file_size and document.file_size > 20 * 1024 * 1024:
        await message.reply_text("Telegram only lets bots download files up to 20 MB.")
        return

    uploads = (settings.download_dir / "uploads").resolve()
    uploads.mkdir(parents=True, exist_ok=True)
    path = uploads / f"{secrets.token_hex(8)}.torrent"
    try:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=str(path))
    except TelegramError as exc:
        path.unlink(missing_ok=True)
        await message.reply_text(f"Could not download that .torrent file: {exc}")
        return

    await _submit_job(
        update,
        context,
        source=sanitize_filename(name, "file.torrent"),
        source_kind="torrent_file",
        torrent_path=path,
    )


# ---------------------------------------------------------------------- buttons
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    query = update.callback_query
    if query is None or query.data is None:
        return
    if not _authorized(update, settings):
        await _deny(update, settings)
        return

    parts = query.data.split(":")
    if parts[0] == "pick":
        await query.answer(
            "That menu is no longer used. Send the link again.", show_alert=True
        )
        return

    if parts[0] == "cancel" and len(parts) == 2:
        manager: JobManager = context.application.bot_data["manager"]
        job = manager.get(parts[1])
        if not job or job.user_id != update.effective_user.id:
            await query.answer("Job not found or already finished.", show_alert=True)
            return
        job.cancel.cancel()
        await query.answer("Cancelling…")
        return

    await query.answer()


# ------------------------------------------------------------------- lifecycle
async def track_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    lock: asyncio.Lock = context.application.bot_data["state_lock"]
    async with lock:
        last = int(context.application.bot_data.get("last_update_id", 0))
        if update.update_id <= last:
            raise ApplicationHandlerStop
        context.application.bot_data["last_update_id"] = update.update_id
        save_state(settings.state_file, {"last_update_id": update.update_id})


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled update error", exc_info=context.error)


async def post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Welcome and usage"),
                BotCommand("gf", "Upload to Gofile: /gf <link>"),
                BotCommand("mirror", "Upload to Gofile: /mirror <link>"),
                BotCommand("status", "Show active jobs"),
                BotCommand("cancel", "Cancel your jobs"),
                BotCommand("id", "Show your Telegram ID"),
            ]
        )
    except TelegramError:
        logger.warning("Could not set bot commands", exc_info=True)


def _register(app: Application, settings: Settings, manager: JobManager) -> None:
    app.bot_data["settings"] = settings
    app.bot_data["manager"] = manager
    app.bot_data["reporters"] = set()
    app.bot_data["state_lock"] = asyncio.Lock()
    app.bot_data["accepting_jobs"] = True

    app.add_handler(TypeHandler(Update, track_update), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("gf", cmd_gf))
    app.add_handler(CommandHandler("mirror", cmd_mirror))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)


async def run_bot(settings: Settings) -> None:
    manager = JobManager(settings)
    app = (
        Application.builder()
        .token(settings.telegram_token)
        .post_init(post_init)
        .build()
    )
    _register(app, settings, manager)
    state = load_state(settings.state_file)
    app.bot_data["last_update_id"] = int(state.get("last_update_id", 0) or 0)

    if not settings.allowed_user_ids:
        logger.warning("ALLOWED_USER_IDS is empty: anyone who finds the bot can use it.")

    # Remove .torrent files left behind by a previous interrupted run.
    uploads = settings.download_dir / "uploads"
    if uploads.exists():
        cutoff = time.time() - 24 * 60 * 60
        for old_file in uploads.glob("*.torrent"):
            try:
                if old_file.stat().st_mtime < cutoff:
                    old_file.unlink(missing_ok=True)
            except OSError:
                pass

    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    logger.info(
        "Bot polling started (run_seconds=%s, max_file_size=%s, uploader=Gofile)",
        settings.run_seconds or "unlimited",
        human_size(settings.max_file_size),
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        if settings.run_seconds > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.run_seconds)
            except asyncio.TimeoutError:
                pass
        else:
            await stop_event.wait()
    finally:
        logger.info("Stopping bot…")
        app.bot_data["accepting_jobs"] = False

        # On GitHub Actions, finish jobs that were already running before the
        # process exits. The workflow has enough timeout budget for this grace
        # period. Local runs (run_seconds=0) stop immediately on Ctrl+C.
        if settings.run_seconds > 0:
            drain_deadline = time.monotonic() + 20 * 60
            while manager.active_jobs() and time.monotonic() < drain_deadline:
                await asyncio.sleep(5)
            if manager.active_jobs():
                logger.warning("Grace period ended with active jobs; cancelling them.")

        for job in manager.active_jobs():
            job.cancel.cancel()

        try:
            await app.updater.stop()
        except TelegramError:
            pass
        try:
            await app.stop()
        except TelegramError:
            pass
        await app.shutdown()


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)
    try:
        asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
