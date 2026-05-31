"""
fbatch.py — /fbatch: Forum Batch Topic Scanner (pure Pyrogram)
"""

import asyncio
import logging
import os
import tempfile
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
from pyrogram.errors import FloodWait

# These are assumed to be already defined in your project
# from .. import bot as app, userbot, API_ID, API_HASH
# from main.plugins.batch import _parse_range, _get_user_session

# Replace with your actual app / session setup
app = Client(...)          # your main bot client
userbot = None             # optional user account, if you have one

_CHUNK      = 100
_DELAY      = 0.35
_NAME_DELAY = 0.2


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _raw_chat(chat_ref) -> str:
    if isinstance(chat_ref, int):
        return str(abs(chat_ref))[3:]
    return str(chat_ref)

def _msg_url(chat_ref, topic_id: int, msg_id: int) -> str:
    rc = _raw_chat(chat_ref)
    if isinstance(chat_ref, int):
        return f"https://t.me/c/{rc}/{topic_id}/{msg_id}"
    return f"https://t.me/{rc}/{topic_id}/{msg_id}"

def _topic_url(chat_ref, topic_id: int) -> str:
    rc = _raw_chat(chat_ref)
    if isinstance(chat_ref, int):
        return f"https://t.me/c/{rc}/{topic_id}"
    return f"https://t.me/{rc}/{topic_id}"

def _get_topic_id(msg: Message) -> "int | None":
    """
    Extract forum-topic ID from a Pyrogram Message.
    Handles different Pyrogram / Pyrofork builds.
    """
    # 1. Service message that created the topic
    if msg.forum_topic_created is not None:
        return msg.id

    # 2. Standard field
    if msg.reply_to_top_message_id:
        return msg.reply_to_top_message_id

    # 3. Alternative field
    if msg.message_thread_id:
        return msg.message_thread_id

    # 4. Fallback: raw reply_to header
    try:
        raw = msg._raw  # may not exist in all builds
        if raw and raw.reply_to:
            if raw.reply_to.reply_to_top_id:
                return raw.reply_to.reply_to_top_id
            if raw.reply_to.reply_to_msg_id:
                return raw.reply_to.reply_to_msg_id
    except Exception:
        pass

    return None


# ─── Core scan ────────────────────────────────────────────────────────────────

async def _scan(acc: Client, chat_ref: str | int, scan_start: int, scan_end: int,
                status_msg: Message) -> "dict[int, dict]":
    topics = {}
    total = scan_end - scan_start + 1
    done = 0
    last_upd = -10

    for chunk_start in range(scan_start, scan_end + 1, _CHUNK):
        ids = list(range(chunk_start, min(chunk_start + _CHUNK, scan_end + 1)))
        try:
            result = await acc.get_messages(chat_ref, ids)
            msgs = result if isinstance(result, list) else [result]
        except Exception as e:
            logger.warning(f"fbatch get_messages error: {e}")
            await asyncio.sleep(1.5)
            continue

        for msg in msgs:
            if msg is None or msg.empty:
                continue
            tid = _get_topic_id(msg)
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
        pct = done * 100 // total
        if pct >= last_upd + 10:
            last_upd = pct
            try:
                await status_msg.edit(
                    f"🔍 Scanning… {pct}% ({done}/{total} IDs)\n"
                    f"Topics found so far: **{len(topics)}**"
                )
            except Exception:
                pass

        await asyncio.sleep(_DELAY)

    return topics


async def _fetch_names(acc: Client, chat_ref: str | int, topics: dict) -> None:
    for tid in list(topics.keys()):
        try:
            msg = await acc.get_messages(chat_ref, tid)
            if msg and not msg.empty and msg.forum_topic_created:
                ftc = msg.forum_topic_created
                topics[tid]["name"] = ftc.name or ftc.title or None
        except Exception:
            pass
        await asyncio.sleep(_NAME_DELAY)


# ─── /fbatch command (pure Pyrogram) ──────────────────────────────────────────

@app.on_message(filters.command("fbatch") & filters.private)   # or filters.group if needed
async def fbatch_command(client: Client, message: Message):
    uid = message.from_user.id

    # Step 1: ask for range link
    try:
        await message.reply(
            "📋 **Forum Topic Scanner**\n\n"
            "Send the **start–end link range** to scan:\n\n"
            "**Format:** `START_LINK-END_LINK`\n\n"
            "**Example:**\n"
            "`https://t.me/c/2932205861/116/117-https://t.me/c/2932205861/1040/1642`",
            reply_markup=ForceReply(selective=True)
        )
        range_msg = await client.ask(
            message.chat.id,
            "Send the link range",
            timeout=120,
            filters=filters.text
        )
        raw_range = range_msg.text.strip()
    except asyncio.TimeoutError:
        await message.reply("⏳ Timed out. Send /fbatch to try again.")
        return
    except Exception as e:
        logger.warning(f"fbatch conv error: {e}")
        return

    if not raw_range:
        await message.reply("❌ No link received. Send /fbatch to try again.")
        return

    # Step 2: parse (use your existing _parse_range)
    parsed = _parse_range(raw_range)      # assumes it's importable
    if not parsed:
        await message.reply(
            "❌ Could not parse that link.\n\n"
            "Use the format:\n"
            "`https://t.me/c/CHATID/TOPIC/MSGID-https://t.me/c/CHATID/TOPIC2/MSGID2`"
        )
        return

    chat_ref, _st, scan_start, _et, scan_end = parsed

    if scan_end < scan_start:
        await message.reply("❌ End message ID must be ≥ start message ID.")
        return

    # Step 3: get user session (fallback to userbot or personal client)
    acc = userbot
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
                await message.reply(f"⚠️ Could not start your session: `{e}`")
                return

    if acc is None:
        await message.reply(
            "❌ **No user session available.**\n\n"
            "A Telegram user account is required to read private channels.\n"
            "Use /login to authenticate, then try /fbatch again."
        )
        return

    # Step 4: scan
    total_ids = scan_end - scan_start + 1
    status = await message.reply(
        f"🔍 **Forum Topic Scanner** — started\n\n"
        f"Chat : `{chat_ref}`\n"
        f"Range: `{scan_start}` → `{scan_end}` ({total_ids} IDs)\n\n"
        f"⏳ Scanning…"
    )

    try:
        topics = await _scan(acc, chat_ref, scan_start, scan_end, status)
    except Exception as e:
        logger.error(f"fbatch scan: {e}")
        await status.edit(f"❌ Scan failed: `{e}`")
        return

    if not topics:
        await status.edit(
            f"⚠️ **No forum topics found** in range `{scan_start}` → `{scan_end}`.\n\n"
            "• Make sure the account can read this group.\n"
            "• Confirm it is a forum supergroup (Topics enabled).\n"
            "• All messages in range may be deleted."
        )
        if personal_acc:
            try: await personal_acc.stop()
            except Exception: pass
        return

    # Step 5: fetch topic names
    try:
        await status.edit(f"✅ Found **{len(topics)}** topic(s) — fetching names…")
        await _fetch_names(acc, chat_ref, topics)
    except Exception as e:
        logger.warning(f"fbatch names: {e}")
    finally:
        if personal_acc:
            try: await personal_acc.stop()
            except Exception: pass

    # Step 6: build .txt output
    lines = []
    lines.append(
        f"Forum Topic Scan Results\n"
        f"Chat     : {chat_ref}\n"
        f"Range    : {scan_start} → {scan_end}  ({total_ids} IDs scanned)\n"
        f"Topics   : {len(topics)} active\n"
        f"{'=' * 55}\n"
    )

    for tid in sorted(topics.keys()):
        info      = topics[tid]
        name      = info["name"] or f"Topic {tid}"
        first_mid = info["min"]
        last_mid  = info["max"]

        t_link     = _topic_url(chat_ref, tid)
        first_link = _msg_url(chat_ref, tid, first_mid)
        last_link  = _msg_url(chat_ref, tid, last_mid)

        lines.append(
            f"Topic  : {name}\n"
            f"ID     : {tid}\n"
            f"URL    : {t_link}\n"
            f"First  : {first_link}\n"
            f"Last   : {last_link}\n"
            f"{'-' * 55}"
        )

    txt_content = "\n".join(lines)

    # Step 7: send the file
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix=f"fbatch_{uid}_")
        os.close(fd)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(txt_content)

        caption = f"✅ **{len(topics)} topics** found in range `{scan_start}` → `{scan_end}`"
        await client.send_document(
            uid, tmp_path,
            caption=caption
        )
    except Exception as e:
        logger.error(f"fbatch send_file: {e}")
        await message.reply(f"❌ Could not send result file: `{e}`")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except Exception: pass

    try:
        await status.delete()
    except Exception:
        pass
