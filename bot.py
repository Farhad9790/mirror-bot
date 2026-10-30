"""Telegram mirror bot entry point."""

from __future__ import annotations

import asyncio
import html
import logging
import mimetypes
import secrets
import shutil
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

# Telegram Bot API only serves files up to 20 MB to cloud bots.
TELEGRAM_FILE_LIMIT = 20 * 1024 * 1024

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
def _discard_upload(path: Optional[Path]) -> None:
    if not path:
        return
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _attachment_filename(message: Message, attachment) -> str:
    """Pick a stable, safe filename for a file sent in Telegram."""
    name = getattr(attachment, "file_name", None)
    if name:
        return sanitize_filename(name, "file.bin")

    mime_type = (getattr(attachment, "mime_type", None) or "").split(";")[0].strip().lower()
    extension = mimetypes.guess_extension(mime_type) if mime_type else None
    if extension == ".jpe":
        extension = ".jpg"

    unique = getattr(attachment, "file_unique_id", None) or secrets.token_hex(4)
    if message.photo:
        prefix, extension = "photo", extension or ".jpg"
    elif message.video:
        prefix, extension = "video", extension or ".mp4"
    elif message.audio:
        prefix, extension = "audio", extension or ".mp3"
    elif message.voice:
        # Telegram voice notes are always OGG/Opus, even though mimetypes may
        # suggest the equivalent ".oga" extension.
        prefix, extension = "voice", ".ogg"
    elif message.video_note:
        prefix, extension = "video_note", extension or ".mp4"
    elif message.animation:
        prefix, extension = "animation", extension or ".mp4"
    elif message.sticker:
        prefix = "sticker"
        if message.sticker.is_animated:
            extension = ".tgs"
        elif message.sticker.is_video:
            extension = ".webm"
        else:
            extension = extension or ".webp"
    else:
        prefix, extension = "file", extension or ".bin"

    return sanitize_filename(f"{prefix}_{unique}{extension}", f"{prefix}{extension or '.bin'}")


def _message_attachment(message: Message):
    """Return the file-like attachment of a Telegram message, if any."""
    if message.photo:
        return max(
            message.photo,
            key=lambda photo: photo.file_size or (photo.width * photo.height),
        )
    for attribute in (
        "document",
        "video",
        "audio",
        "voice",
        "video_note",
        "animation",
        "sticker",
    ):
        attachment = getattr(message, attribute, None)
        if attachment:
            return attachment
    return None


async def _save_telegram_file(
    message: Message,
    attachment,
    filename: str,
    status: Message,
    settings: Settings,
) -> Optional[Path]:
    uploads = (settings.download_dir / "uploads").resolve()
    uploads.mkdir(parents=True, exist_ok=True)
    folder = uploads / secrets.token_hex(8)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename

    try:
        telegram_file = await attachment.get_file()
        await telegram_file.download_to_drive(custom_path=str(path))
    except (TelegramError, OSError) as exc:
        _discard_upload(path)
        error = f"Could not download that file: {html.escape(str(exc))}"
        try:
            await status.edit_text(error, parse_mode=ParseMode.HTML)
        except TelegramError:
            await message.reply_text(error, parse_mode=ParseMode.HTML)
        return None

    if not path.exists() or path.stat().st_size == 0:
        _discard_upload(path)
        error = "The file Telegram sent was empty."
        try:
            await status.edit_text(error)
        except TelegramError:
            await message.reply_text(error)
        return None

    return path


async def _start_job(
    context: ContextTypes.DEFAULT_TYPE,
    job: Job,
    message: Optional[Message],
) -> None:
    if not context.application.bot_data.get("accepting_jobs", True):
        _discard_upload(job.local_path)
        text = "The bot is finishing its current run. Send the file again in a few minutes."
        if message is not None:
            try:
                await message.edit_text(text)
                return
            except TelegramError:
                pass
        await context.bot.send_message(job.chat_id, text)
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
    local_path: Optional[Path] = None,
    status_message: Optional[Message] = None,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message

    async def reject(text: str) -> None:
        _discard_upload(local_path)
        if status_message is not None:
            try:
                await status_message.edit_text(text, parse_mode=ParseMode.HTML)
                return
            except TelegramError:
                pass
        await message.reply_text(text, parse_mode=ParseMode.HTML)

    if not context.application.bot_data.get("accepting_jobs", True):
        await reject("The bot is finishing its current run. Send the file again in a few minutes.")
        return

    usable, reason = get_uploader("gofile", settings).available()
    if not usable:
        await reject(f"⚠️ {html.escape(reason)}")
        return

    job = new_job(
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        source=source,
        host="gofile",
        source_kind=source_kind,
        local_path=local_path,
    )
    await _start_job(context, job, status_message)


# -------------------------------------------------------------------- commands
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        await _deny(update, settings)
        return
    await update.effective_message.reply_text(
        "<b>Mirror bot</b>\n\n"
        "Send me a link, a magnet link, a <code>.torrent</code> file, or any file. "
        "I download it and upload it to Gofile, then reply with the public link.\n\n"
        "Files sent directly in Telegram are uploaded too. Telegram bots can "
        "download attachments up to 20 MB; send a link for anything larger. "
        "You can also reply to a file with /gf.\n\n"
        "Any file type, Gofile share links, pages with a download button, and "
        "video sites are supported.\n\n"
        "<b>Commands</b>\n"
        "/gf &lt;link&gt; — upload a link, or reply to a file with /gf\n"
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

    replied = getattr(update.effective_message, "reply_to_message", None)
    if not source and replied is not None:
        attachment = _message_attachment(replied)
        if attachment is not None:
            await _mirror_attachment(update, context, replied, attachment)
            return
        if replied.text:
            source = extract_url(replied.text)

    if not source:
        await update.effective_message.reply_text(
            f"Usage: <code>/{command} &lt;link&gt;</code>\n"
            f"Or reply to a file or link with <code>/{command}</code>.",
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
            await message.reply_text(
                "Send me a link, a magnet link, a .torrent file, or any file."
            )
        return
    if not _authorized(update, settings):
        await _deny(update, settings)
        return
    await _submit_job(update, context, source=source)


async def _mirror_attachment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    attachment_message: Message,
    attachment,
) -> None:
    """Download a Telegram attachment and submit it as a Gofile job."""
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None:
        return

    document = attachment_message.document
    if document:
        name = document.file_name or "file.torrent"
        is_torrent = name.lower().endswith(".torrent") or document.mime_type in {
            "application/x-bittorrent",
            "application/x-bittorrent; charset=utf-8",
        }
        if is_torrent:
            if document.file_size and document.file_size > TELEGRAM_FILE_LIMIT:
                await message.reply_text(
                    "Telegram only lets bots download files up to 20 MB. "
                    "Send a direct link to a larger .torrent file instead."
                )
                return
            filename = sanitize_filename(name, "file.torrent")
            status = await message.reply_text(
                f"Downloading <code>{html.escape(filename)}</code> from Telegram…",
                parse_mode=ParseMode.HTML,
            )
            path = await _save_telegram_file(
                message, document, filename, status, settings
            )
            if path is None:
                return
            await _submit_job(
                update,
                context,
                source=filename,
                source_kind="torrent_file",
                local_path=path,
                status_message=status,
            )
            return

    size = getattr(attachment, "file_size", None)
    if size and size > TELEGRAM_FILE_LIMIT:
        await message.reply_text(
            "Telegram only lets bots download files up to 20 MB. "
            "Send a direct link to a larger file instead."
        )
        return
    if size and size > settings.max_file_size:
        await message.reply_text(
            f"That file is {human_size(size)}, above the "
            f"{human_size(settings.max_file_size)} limit."
        )
        return

    filename = _attachment_filename(attachment_message, attachment)
    status = await message.reply_text(
        f"Downloading <code>{html.escape(filename)}</code> from Telegram…",
        parse_mode=ParseMode.HTML,
    )
    path = await _save_telegram_file(message, attachment, filename, status, settings)
    if path is None:
        return
    await _submit_job(
        update,
        context,
        source=filename,
        source_kind="telegram_file",
        local_path=path,
        status_message=status,
    )


async def on_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None:
        return
    attachment = _message_attachment(message)
    if attachment is None:
        return
    if not _authorized(update, settings):
        await _deny(update, settings)
        return
    await _mirror_attachment(update, context, message, attachment)


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
    app.add_handler(MessageHandler(filters.ATTACHMENT, on_attachment))
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

    # Remove files left behind by a previous interrupted run.
    uploads = settings.download_dir / "uploads"
    if uploads.exists():
        cutoff = time.time() - 24 * 60 * 60
        for old_path in uploads.iterdir():
            try:
                if old_path.stat().st_mtime >= cutoff:
                    continue
                if old_path.is_dir():
                    shutil.rmtree(old_path, ignore_errors=True)
                else:
                    old_path.unlink(missing_ok=True)
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
