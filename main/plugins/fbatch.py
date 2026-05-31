"""
fbatch.py — /fbatch: Forum Batch Topic Scanner

Usage: /fbatch <start_link>-<end_link>
  e.g. /fbatch https://t.me/c/2932205861/116/117-https://t.me/c/2932205861/1040/1642

Scans every message ID in the given range, groups them by forum topic,
and returns a clean list of all active topics with their first/last message links.
Deleted messages and non-existent topic IDs are automatically skipped.
"""

import asyncio
import logging

from pyrogram import Client
from telethon import events

from .. import bot as gagan, userbot, Bot, API_ID, API_HASH  # Bot used for personal_acc error msg
from main.plugins.batch import _parse_range, _get_user_session

logger = logging.getLogger(__name__)

# ── How many message IDs to fetch in one get_messages() call ─────────────────
_FBATCH_CHUNK = 100

# ── Delay between chunks to stay well under flood limits ─────────────────────
_FBATCH_DELAY = 0.4   # seconds


# ── Topic-ID extractor ────────────────────────────────────────────────────────

def _topic_id_of(msg) -> "int | None":
    """
    Return the forum topic ID that *msg* belongs to, or None if the message
    is not part of a forum topic.

    Tries several attribute paths used by different pyrogram/pyrofork builds:
      1. forum_topic_created — the message IS the topic header → topic = msg.id
      2. reply_to_top_message_id — explicit top-of-thread field
      3. message_thread_id — python-telegram-bot style alias
      4. reply_to_message_id when it appears to be a thread root reference
    """
    # Case 1: this message is itself a topic-creation service message
    if getattr(msg, "forum_topic_created", None) is not None:
        return msg.id

    # Case 2: pyrogram / pyrofork standard field
    tid = getattr(msg, "reply_to_top_message_id", None)
    if tid:
        return tid

    # Case 3: python-telegram-bot style
    tid = getattr(msg, "message_thread_id", None)
    if tid:
        return tid

    return None


# ── URL builder ───────────────────────────────────────────────────────────────

def _make_url(chat_ref, topic_id: int, msg_id: int) -> str:
    if isinstance(chat_ref, int):
        # chat_ref is e.g. -1002932205861 → strip leading -100 to get channel ID
        raw = str(abs(chat_ref))[3:]
        return f"https://t.me/c/{raw}/{topic_id}/{msg_id}"
    return f"https://t.me/{chat_ref}/{topic_id}/{msg_id}"


def _topic_url(chat_ref, topic_id: int) -> str:
    """Direct link to the topic itself (its root/header message)."""
    if isinstance(chat_ref, int):
        raw = str(abs(chat_ref))[3:]
        return f"https://t.me/c/{raw}/{topic_id}"
    return f"https://t.me/{chat_ref}/{topic_id}"


# ── Main scan logic ───────────────────────────────────────────────────────────

async def _scan_topics(acc, chat_ref: "int | str", scan_start: int, scan_end: int,
                       status_msg) -> "dict[int, dict]":
    """
    Fetch all messages from scan_start to scan_end in chunks of _FBATCH_CHUNK.
    Returns a dict: { topic_id: { 'min': int, 'max': int, 'name': str|None } }
    """
    topics: "dict[int, dict]" = {}
    total    = scan_end - scan_start + 1
    done     = 0
    last_pct = -1

    for chunk_start in range(scan_start, scan_end + 1, _FBATCH_CHUNK):
        ids   = list(range(chunk_start, min(chunk_start + _FBATCH_CHUNK, scan_end + 1)))
        msgs  = []

        try:
            result = await acc.get_messages(chat_ref, ids)
            msgs = result if isinstance(result, list) else [result]
        except Exception as e:
            logger.warning(f"fbatch: get_messages({chunk_start}…) error: {e}")
            await asyncio.sleep(1)
            continue

        for msg in msgs:
            if msg is None or getattr(msg, "empty", True):
                continue
            tid = _topic_id_of(msg)
            if tid is None:
                continue
            mid = msg.id
            if tid not in topics:
                topics[tid] = {"min": mid, "max": mid, "name": None}
            else:
                if mid < topics[tid]["min"]:
                    topics[tid]["min"] = mid
                if mid > topics[tid]["max"]:
                    topics[tid]["max"] = mid

        done += len(ids)
        pct   = (done * 100) // total
        # Update status every 10 %
        if pct // 10 != last_pct // 10:
            last_pct = pct
            try:
                await status_msg.edit(
                    f"🔍 Scanning… {pct}% ({done}/{total} IDs)\n"
                    f"Topics found so far: **{len(topics)}**"
                )
            except Exception:
                pass

        await asyncio.sleep(_FBATCH_DELAY)

    return topics


async def _fetch_topic_names(acc, chat_ref, topics: dict) -> None:
    """
    For each topic ID in *topics*, fetch the header message to get the topic
    title and store it under topics[tid]['name'].  Errors are silently ignored
    (name stays None and the output shows the raw topic ID instead).
    """
    for tid in list(topics.keys()):
        try:
            msg = await acc.get_messages(chat_ref, tid)
            if msg and not getattr(msg, "empty", True):
                # Topic name lives in the forum_topic_created action
                ftc = getattr(msg, "forum_topic_created", None)
                if ftc:
                    topics[tid]["name"] = getattr(ftc, "name", None) or getattr(ftc, "title", None)
                if topics[tid]["name"] is None:
                    # Some builds store it directly on the service message
                    topics[tid]["name"] = (
                        getattr(msg, "topic_name", None)
                        or getattr(msg, "title", None)
                    )
        except Exception:
            pass
        await asyncio.sleep(0.2)


# ── /fbatch command ───────────────────────────────────────────────────────────

@gagan.on(events.NewMessage(incoming=True, pattern=r'^/fbatch'))
async def fbatch_command(event):
    uid  = event.sender_id
    text = event.raw_text.strip()

    # ── Parse inline argument or ask user ────────────────────────────────────
    parts = text.split(None, 1)
    raw_range = parts[1].strip() if len(parts) >= 2 else None

    if not raw_range:
        from telethon import Button as _Btn
        ask = await event.respond(
            "📬 **Forum Batch Topic Scanner**\n\n"
            "Send the **start–end link range** for the forum you want to scan:\n\n"
            "`https://t.me/c/CHATID/TOPIC/START-https://t.me/c/CHATID/TOPIC2/END`\n\n"
            "_Example:_\n"
            "`https://t.me/c/2932205861/116/117-https://t.me/c/2932205861/1040/1642`",
            buttons=_Btn.force_reply()
        )
        try:
            async with gagan.conversation(event.chat_id, timeout=120) as conv:
                reply = await conv.get_reply()
                raw_range = reply.text.strip() if reply.text else ""
        except asyncio.TimeoutError:
            await ask.edit("⏳ Timed out. Send /fbatch <link> to try again.")
            return
        except Exception:
            return
        try:
            await ask.delete()
        except Exception:
            pass

    if not raw_range:
        await event.respond("❌ No link provided.")
        return

    parsed = _parse_range(raw_range)
    if not parsed:
        await event.respond(
            "❌ Could not parse that link.\n\n"
            "Format: `https://t.me/c/CHATID/TOPIC/MSGID-https://t.me/c/CHATID/TOPIC2/MSGID2`"
        )
        return

    chat_ref, start_topic, start_msg, end_topic, end_msg = parsed

    # ── Resolve actual scan bounds ────────────────────────────────────────────
    # start_msg / end_msg are global message IDs from the parsed URLs.
    scan_start = start_msg
    scan_end   = end_msg

    if scan_end <= 0 or scan_end < scan_start:
        await event.respond("❌ End message ID must be greater than start message ID.")
        return

    # ── Get user account (userbot or personal session) ────────────────────────
    acc          = userbot
    personal_acc = None

    if acc is None:
        sess = await _get_user_session(uid)
        if sess:
            try:
                personal_acc = Client(
                    f"fbatch_{uid}",
                    session_string=sess,
                    api_id=int(API_ID),
                    api_hash=API_HASH,
                    in_memory=True,
                )
                await personal_acc.start()
                acc = personal_acc
            except Exception as e:
                await Bot.send_message(uid, f"⚠️ Could not start your session: `{e}`")
                personal_acc = None

    if acc is None:
        await event.respond(
            "❌ **No user session available.**\n\n"
            "A Telegram user account is required to read private channels.\n"
            "Use /login to authenticate, then try /fbatch again."
        )
        return

    total_ids = scan_end - scan_start + 1
    status = await event.respond(
        f"🔍 **Forum Topic Scanner starting…**\n\n"
        f"Chat: `{chat_ref}`\n"
        f"Range: msg `{scan_start}` → `{scan_end}` ({total_ids} IDs)\n\n"
        f"⏳ Scanning for topics…"
    )

    # ── Scan ─────────────────────────────────────────────────────────────────
    try:
        topics = await _scan_topics(acc, chat_ref, scan_start, scan_end, status)
    except Exception as e:
        logger.error(f"fbatch: scan error: {e}")
        await status.edit(f"❌ Scan failed: `{e}`")
        return
    finally:
        if personal_acc:
            try:
                await personal_acc.stop()
            except Exception:
                pass

    if not topics:
        await status.edit(
            f"⚠️ **No forum topics found** in range `{scan_start}` → `{scan_end}`.\n\n"
            "Possible reasons:\n"
            "• This is not a forum supergroup\n"
            "• The bot/account has no access to those messages\n"
            "• All messages in range are deleted"
        )
        return

    # ── Fetch topic names ─────────────────────────────────────────────────────
    try:
        await status.edit(
            f"✅ Found **{len(topics)}** topic(s). Fetching names…"
        )
        await _fetch_topic_names(acc, chat_ref, topics)
    except Exception as e:
        logger.warning(f"fbatch: name fetch error: {e}")

    # ── Format output ─────────────────────────────────────────────────────────
    lines = [
        f"✅ **Found {len(topics)} active topic(s)** in range `{scan_start}` → `{scan_end}`\n"
    ]

    for tid in sorted(topics.keys()):
        info  = topics[tid]
        name  = info["name"] or f"Topic {tid}"
        first = info["min"]
        last  = info["max"]

        t_link     = _topic_url(chat_ref, tid)
        first_link = _make_url(chat_ref, tid, first)
        last_link  = _make_url(chat_ref, tid, last)

        lines.append(
            f"📌 **{name}**\n"
            f"   Thread ID : [{tid}]({t_link})\n"
            f"   First Msg : [{first}]({first_link})\n"
            f"   Last Msg  : [{last}]({last_link})"
        )

    # Telegram message limit is 4096 chars — split into multiple messages if needed
    chunk_lines: "list[str]" = []
    chunk_len = 0
    LIMIT     = 3800

    async def _send_chunk():
        if chunk_lines:
            await gagan.send_message(uid, "\n\n".join(chunk_lines), link_preview=False)

    for line in lines:
        if chunk_len + len(line) + 2 > LIMIT and chunk_lines:
            await _send_chunk()
            chunk_lines.clear()
            chunk_len = 0
        chunk_lines.append(line)
        chunk_len += len(line) + 2

    await _send_chunk()

    try:
        await status.delete()
    except Exception:
        pass
