"""
multi_bot.py — registers all standard handlers on each extra bot
(BOT_TOKEN2 / BOT_TOKEN3 / BOT_TOKEN4).

Key design:
- Every extra bot has its OWN active_batches dict (fully isolated).
  Bot2's /cancel only cancels Bot2's batch; Bot1 is unaffected.
- All extra bots share the same userbot session — login once, works everywhere.
- Parallel /batch jobs are possible: each bot is an independent Telegram connection.
"""

import asyncio
import json
import logging
import os
import time

from telethon import events, Button
from pyrogram import Client
from pyrogram.errors import FloodWait

from .. import extra_clients, userbot, API_ID, API_HASH
from main.plugins.batch import _parse_range, _run_batch, temp_log_file
from main.plugins.helpers import get_link, join
from main.plugins.pyroplug import ggn_new, user_chat_ids

logger = logging.getLogger(__name__)

_COMMANDS = [
    '/dl', '/batch', '/sbatch', '/cancel', '/login', '/logout', '/mysession',
    '/start', '/help', '/logs', '/setchat', '/remthumb', '/ivalid',
]

_START_PIC = "https://graph.org/file/1dfb96bd8f00a7c05f164.gif"
_START_TEXT = (
    "Send me the Link of any message of Restricted Channels to Clone it here.\n"
    "For private channel's messages, send the Invite Link first.\n\n"
    "👉🏻 Execute /batch for bulk process upto 10K files range."
)
_REPO_URL = "https://github.com/devgaganin"
_HELP_TEXT = """Here are the available commands:

➡️ /batch - Bulk process up to 10K message range.

➡️ /setchat - Forward messages to a group/channel/user.
```Use: /setchat <chatID>```

➡️ /remthumb - Delete your custom thumbnail.

➡️ /cancel - Cancel your running batch on this bot.

➡️ /dl - Download from YouTube, LinkedIn, etc.

Note: Send a photo (no command) to set a custom thumbnail.

[GitHub](%s)
""" % _REPO_URL


async def _get_user_session(user_id):
    from main.plugins.session_store import get_user_session as _gs
    return await _gs(user_id)


def _is_range_link(raw: str) -> bool:
    """
    Return True if the text is a batch range — must NOT be processed as a single file.

    Handles two formats:
      NEW  — two full URLs joined by a hyphen:
               https://t.me/c/CHAT/TOPIC/143-https://t.me/c/CHAT/TOPIC/144
      OLD  — single URL whose last segment is START-END:
               https://t.me/c/CHAT/TOPIC/23-25
    """
    if raw.count('https://') >= 2 or raw.count('http://') >= 2:
        return True
    clean = raw.strip().rstrip("/").split("?")[0]
    last = clean.split("/")[-1]
    if "-" in last:
        parts = last.split("-", 1)
        return parts[0].isdigit() and parts[1].isdigit()
    return False


def _register(tel_bot, pyro_bot, bot_index: int):
    """Bind all handlers to one extra (tel_bot, pyro_bot) pair.

    Each call creates a fresh _batches dict that is private to this bot.
    No cross-bot interference possible.
    """

    # ── THIS BOT'S OWN batch-tracking dict ────────────────────────────────────
    _batches = {}   # uid → False (running) | True (cancel requested)

    # /cancel ─────────────────────────────────────────────────────────────────
    @tel_bot.on(events.NewMessage(incoming=True, pattern='/cancel'))
    async def _cancel(event):
        uid = event.sender_id
        if _batches.get(uid) is False:
            _batches[uid] = True
            await event.respond("✅ Batch cancelled.")
        else:
            await event.respond("No running batch to cancel on this bot.")

    # /logs ───────────────────────────────────────────────────────────────────
    @tel_bot.on(events.NewMessage(incoming=True, pattern='/logs'))
    async def _send_log(event):
        if os.path.exists(temp_log_file):
            await tel_bot.send_file(event.sender_id, temp_log_file,
                                    caption="Log file (last 3 min).")
        else:
            await event.respond("Log file not found.")

    # /start ──────────────────────────────────────────────────────────────────
    @tel_bot.on(events.NewMessage(incoming=True, pattern='^/start'))
    async def _start(event):
        buttons = [
            [Button.url("Join Channel", url="https://t.me/devggn")],
            [Button.url("Contact Me", url="https://t.me/ggnhere")],
        ]
        await tel_bot.send_file(event.chat_id, file=_START_PIC,
                                caption=_START_TEXT, buttons=buttons)

    # /help ───────────────────────────────────────────────────────────────────
    @tel_bot.on(events.NewMessage(incoming=True, pattern='/help'))
    async def _help(event):
        buttons = [[Button.url("REPO", url=_REPO_URL)]]
        await event.respond(_HELP_TEXT, buttons=buttons, link_preview=False)

    # /setchat ────────────────────────────────────────────────────────────────
    @tel_bot.on(events.NewMessage(incoming=True, pattern='/setchat'))
    async def _setchat(event):
        try:
            chat_id = int(event.raw_text.split(" ", 1)[1])
            user_chat_ids[event.sender_id] = chat_id
            await event.reply("Chat ID set successfully!")
        except (ValueError, IndexError):
            await event.reply("Usage: /setchat <chat_id>")

    # /remthumb ───────────────────────────────────────────────────────────────
    @tel_bot.on(events.NewMessage(incoming=True, pattern='/remthumb'))
    async def _remthumb(event):
        user_id = event.sender_id
        try:
            os.remove(f'{user_id}.jpg')
            await event.respond('Thumbnail removed successfully!')
        except FileNotFoundError:
            await event.respond("No thumbnail found to remove.")

    # Photo → save as thumbnail ───────────────────────────────────────────────
    @tel_bot.on(events.NewMessage(incoming=True,
                                  func=lambda e: e.photo and e.is_private))
    async def _save_thumb(event):
        user_id = event.sender_id
        temp_path = await tel_bot.download_media(event.media)
        if os.path.exists(f'{user_id}.jpg'):
            os.remove(f'{user_id}.jpg')
        os.rename(temp_path, f'./{user_id}.jpg')
        await event.respond('Thumbnail saved successfully!')

    # /batch ──────────────────────────────────────────────────────────────────
    @tel_bot.on(events.NewMessage(incoming=True, pattern=r'^/batch(?:\s|$|@)'))
    async def _bulk(event):
        uid = event.sender_id

        if _batches.get(uid) is False:
            return await event.reply(
                "A batch is already running on this bot. Use /cancel to stop it."
            )

        parsed = None

        async with tel_bot.conversation(event.chat_id, timeout=120) as conv:
            try:
                await conv.send_message(
                    "Send the message link with a **start–end range**.\n\n"
                    "**Format — START link, a hyphen, then END link:**\n"
                    "• `https://t.me/c/2133410746/926447-https://t.me/c/2133410746/926450`\n"
                    "• `https://t.me/c/3765531856/4/23-https://t.me/c/3765531856/8/270`\n"
                    "  _(supergroup: topics 4→8)_\n"
                    "• `https://t.me/username/100-https://t.me/username/125`\n\n"
                    "**Old compact format also works:**\n"
                    "• `https://t.me/c/2133410746/926447-926450`\n\n"
                    "_For a single file, send the normal link without a range._",
                    buttons=Button.force_reply()
                )
                link_msg = await conv.get_reply()

                raw = link_msg.text.strip() if link_msg.text else ""
                if not raw:
                    await conv.send_message("No link received. Please try /batch again.")
                    return

                parsed = _parse_range(raw)
                if not parsed:
                    await conv.send_message(
                        "❌ Could not parse a range from that input.\n"
                        "Use: `START_LINK-END_LINK`"
                    )
                    return

                chat_ref, start_topic, start_msg, end_topic, end_msg = parsed

                if end_msg < start_msg and start_topic == end_topic:
                    await conv.send_message("End message ID must be ≥ start message ID.")
                    return

                _batches[uid] = False
                if start_topic is not None and start_topic != end_topic:
                    desc = (f"Topics `{start_topic}` → `{end_topic}`\n"
                            f"From msg `{start_msg}` … to msg `{end_msg}`")
                else:
                    desc = f"Msgs `{start_msg}` → `{end_msg}`"

                await conv.send_message(
                    f"🚀 **Batch starting** (Bot #{bot_index})\n"
                    f"Chat: `{chat_ref}`\n"
                    f"{desc}\n\nUse /cancel to stop."
                )

            except asyncio.TimeoutError:
                await event.respond("⏳ Timed out. Please try /batch again.")
                return
            except Exception as e:
                logger.info(e)
                await event.respond(f"Error: {e}")
                return

        chat_ref, start_topic, start_msg, end_topic, end_msg = parsed

        acc = userbot
        personal_acc = None

        if acc is None:
            sess = await _get_user_session(uid)
            if sess:
                try:
                    personal_acc = Client(
                        f"batch_{uid}_b{bot_index}",
                        session_string=sess,
                        api_id=int(API_ID),
                        api_hash=API_HASH,
                        in_memory=True,
                    )
                    await personal_acc.start()
                    acc = personal_acc
                except Exception as e:
                    await pyro_bot.send_message(
                        uid, f"⚠️ Could not start your session: `{e}`"
                    )
                    personal_acc = None

        if acc is None and isinstance(chat_ref, int):
            _batches.pop(uid, None)
            return await pyro_bot.send_message(
                uid,
                "❌ **No user session available.**\n\n"
                "A Telegram user account is required to access private/restricted channels.\n"
                "👉 Use /login on the **main bot** to authenticate, then try /batch again.",
            )

        try:
            await _run_batch(acc, pyro_bot, uid, chat_ref,
                             start_topic, start_msg, end_topic, end_msg,
                             batches_dict=_batches)
        finally:
            if personal_acc:
                try:
                    await personal_acc.stop()
                except Exception:
                    pass
            _batches.pop(uid, None)

    # Single-link clone ───────────────────────────────────────────────────────
    @tel_bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _clone(event):
        file_name = ''

        if event.message.text and any(
            event.message.text.strip().startswith(cmd) for cmd in _COMMANDS
        ):
            return

        lit = event.text
        if not lit:
            return

        lines = lit.split("\n")
        if len(lines) > 10:
            await event.respond("max 10 links per message")
            return

        for line in lines:
            # Check raw line first — covers both dual-URL and compact range formats
            if _is_range_link(line):
                return

            try:
                link = get_link(line)
                if not link:
                    return
            except TypeError:
                return

            if "|" in line:
                parts = line.split("|")
                if len(parts) == 2:
                    file_name = parts[1].strip()

            edit = await event.respond("Processing!")

            acc = userbot
            tmp_client = None

            if acc is None:
                sess = await _get_user_session(event.sender_id)
                if sess:
                    try:
                        tmp_client = Client(
                            f"tmp_{event.sender_id}_b{bot_index}",
                            session_string=sess,
                            api_id=int(API_ID),
                            api_hash=API_HASH,
                            in_memory=True,
                        )
                        await tmp_client.start()
                        acc = tmp_client
                    except Exception as e:
                        logger.warning(f"Could not start personal session: {e}")
                        acc = None

            if acc is None and 't.me/c/' in link:
                await edit.edit(
                    "❌ No session available to access restricted content.\n"
                    "Use /login on the main bot to log in."
                )
                return

            try:
                if 't.me/' not in link:
                    await edit.edit("invalid link")
                    return

                if 't.me/+' in link:
                    _client = acc if acc else pyro_bot
                    q = await join(_client, link)
                    await edit.edit(q)
                    return

                msg_id = 0
                try:
                    msg_id = int(link.split("/")[-1])
                except ValueError:
                    if '?single' in link:
                        msg_id = int(link.split("?single")[0].split("/")[-1])
                    else:
                        msg_id = -1

                _acc = acc if acc else userbot
                await ggn_new(_acc, pyro_bot, event.sender_id,
                              edit.id, link, msg_id, file_name)

            except FloodWait as fw:
                await tel_bot.send_message(
                    event.sender_id,
                    f'Try again after {fw.value}s due to FloodWait.'
                )
                await edit.delete()
            except Exception as e:
                logger.info(e)
                await tel_bot.send_message(event.sender_id, f"Error: {str(e)}")
                await edit.delete()
            finally:
                if tmp_client:
                    try:
                        await tmp_client.stop()
                    except Exception:
                        pass

            time.sleep(1)


# ── Register on every configured extra bot ────────────────────────────────────

if extra_clients:
    for _i, (_tel, _pyro) in enumerate(extra_clients, start=2):
        _register(_tel, _pyro, _i)
    print(f"[multi_bot] Registered handlers on {len(extra_clients)} extra bot(s).")
else:
    print("[multi_bot] No extra bots configured (BOT_TOKEN2/3/4 not set).")
