"""
bot.py — the Telegram interface.

One command, ``/chapter <url>``, which fetches a WTR-Lab chapter and returns it
as a text file. Long-running extractions stream progress edits so the user is
not staring at a frozen message for twenty seconds.

Design notes worth knowing before editing:

- The bot is stateless per user. Nothing is cached, because a chapter URL is
  cheap to fetch and a cache is just somewhere for a stale copy to hide.
- Errors are written for the person who will read them on a phone, not for a
  log file. "That page is served by JavaScript, so the text is not in the HTML"
  tells a user what to do next; a traceback does not.
- The SSRF guard in ``url_guard`` runs before any network call. It is the only
  thing standing between a public bot and the cloud metadata endpoint, so it is
  not optional and not wrapped in a try/except that swallows it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from extractor import ExtractionError, fetch_chapter, probe
from url_guard import UnsafeURL

log = logging.getLogger("wtrl")

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
DELIVERY_DIR_ENV = "DELIVERY_DIR"
MAX_FILE_BYTES = 20 * 1024 * 1024  # Telegram's own document ceiling

router = Router()


def _token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token or ":" not in token:
        raise SystemExit(
            f"{TOKEN_ENV} is not set. Get a token from @BotFather and export it:\n"
            f"  export {TOKEN_ENV}='123456:ABC-your-token-here'"
        )
    return token


def _delivery_dir() -> Path:
    path = Path(os.environ.get(DELIVERY_DIR_ENV, "./deliveries"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _user_facing(exc: BaseException) -> str:
    """Turn an exception into something worth reading on a phone."""
    if isinstance(exc, UnsafeURL):
        return f"That URL will not work: {exc}"
    if isinstance(exc, ExtractionError):
        return f"Could not read that page. {exc}"
    if isinstance(exc, asyncio.TimeoutError):
        return "The page took too long to answer. Try again, or check the URL."
    return "Something went wrong on my side. The error has been logged."


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Send me a WTR-Lab chapter URL and I will fetch it and send back a "
        "clean .txt file.\n\n"
        "Commands:\n"
        "/chapter <url> - fetch one chapter\n"
        "/probe <url>   - check whether a page needs a real browser\n"
        "/help          - this message\n\n"
        "I only fetch public pages. Anything pointing at a private or internal "
        "address is refused."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await cmd_start(message)


@router.message(Command("probe"))
async def cmd_probe(message: Message, command: CommandObject) -> None:
    url = (command.args or "").strip()
    if not url:
        await message.answer("Usage: /probe <url>")
        return
    status = await message.answer("Checking that page...")
    try:
        result = await probe(url)
        await status.edit_text(
            f"URL: {result['url']}\n"
            f"Resolves to: {result['ip']}\n"
            f"HTML size: {result['html_bytes']:,} bytes\n"
            f"Visible text: {result['visible_chars']:,} characters\n"
            f"Fetch time: {result['elapsed_s']}s\n\n"
            f"{result['verdict']}"
        )
    except Exception as exc:  # noqa: BLE001 - the user needs the reason
        log.exception("probe failed for %s", url)
        await status.edit_text(_user_facing(exc))


@router.message(Command("chapter"))
async def cmd_chapter(message: Message, command: CommandObject) -> None:
    url = (command.args or "").strip()
    if not url:
        await message.answer("Usage: /chapter <url>")
        return

    status = await message.answer("Fetching the chapter...")
    try:
        chapter = await fetch_chapter(url)
    except Exception as exc:  # noqa: BLE001
        log.exception("chapter fetch failed for %s", url)
        await status.edit_text(_user_facing(exc))
        return

    # A chapter this short is a login wall or an error page, not a chapter.
    if chapter.word_count < 40:
        await status.edit_text(
            f"I got the page but only {chapter.word_count} words of chapter text "
            "out of it. That usually means the content is behind a login, or the "
            "layout changed. If you send me the page source I can tell you which."
        )
        return

    payload = chapter.text.encode("utf-8")
    if len(payload) > MAX_FILE_BYTES:
        await status.edit_text(
            f"That chapter is {len(payload) / 1024 / 1024:.1f} MB, over the "
            f"{MAX_FILE_BYTES / 1024 / 1024:.0f} MB limit Telegram allows. "
            "I can send it in parts if you want."
        )
        return

    delivery = _delivery_dir() / chapter.filename
    delivery.write_text(chapter.text, encoding="utf-8")
    log.info("wrote %s (%d words) for user %s", delivery, chapter.word_count, message.from_user.id)

    heading = " ".join(p for p in (chapter.number, chapter.title) if p) or "chapter"
    caption = (
        f"{heading}\n"
        f"{chapter.word_count:,} words, {chapter.paragraphs} paragraphs\n"
        f"source: {chapter.url}"
    )
    if chapter.notes:
        caption += "\n" + "; ".join(chapter.notes)

    await status.edit_text("Done. Sending the file.")
    await message.answer_document(
        document=BufferedInputFile(payload, filename=chapter.filename),
        caption=caption,
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text & ~Command())
async def anything_else(message: Message) -> None:
    """A bare message that is not a command is almost always a pasted URL."""
    text = (message.text or "").strip()
    if text.startswith(("http://", "https://")):
        await cmd_chapter(message, CommandObject(prefix="/", command="chapter", args=text))
        return
    await message.answer(
        "Send me a chapter URL starting with http:// or https://, or use /help."
    )


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = _token()
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    log.info("starting, delivery dir %s", _delivery_dir())
    try:
        # allowed_updates is explicit so a new Bot API version does not silently
        # change which updates we receive.
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        log.info("stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("interrupted")
        sys.exit(0)
