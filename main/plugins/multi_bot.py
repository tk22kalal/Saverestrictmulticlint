"""
multi_bot.py — registers all standard handlers on each extra bot
(BOT_TOKEN2 / BOT_TOKEN3 / BOT_TOKEN4).

All extra bots share the same `userbot` (Pyrogram user session) so only
one login is ever needed.  Parallel /batch jobs are possible because each
bot is an independent Telegram connection.
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
from main.plugins.batch import _parse_link, _run_batch, active_batches, temp_log_file
from main.plugins.helpers import get_link, join
from main.plugins.pyroplug import ggn_new, user_chat_ids

logger = logging.getLogger(__name__)

# ── Constants shared with other plugins ──────────────────────────────────────
_COMMANDS = [
    '/dl', '/batch', '/cancel', '/login', '/logout', '/mysession',
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

➡️ /batch - to process multiple links at once by taking start link, iterating though multiple message ids.

➡️ /setchat - Forward messages directly to a groupID, channelID (with -100), or user (they must have started the bot) bot must be admin in channel or group.

```Use: /setchat channelID```

➡️ /remthumb - Delete your thumbnail.

➡️ /cancel - Cancel ongoing batch process.

➡️ /dl - Download videos directly from Youtube, Linkedin, etc.

Note: To set your custom thumbnail just send a photo without any command.

[GitHub Repository](%s)
""" % _REPO_URL


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_user_session(user_id):
    path = "user_sessions.json"
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f).get(str(user_id))
        except Exception:
            return None
    return None


def _is_range_link(link: str) -> bool:
    last = link.rstrip("/").split("/")[-1].replace("?single", "")
    if "-" in last:
        parts = last.split("-", 1)
        return parts[0].isdigit() and parts[1].isdigit()
    return False


# ── Handler factory ───────────────────────────────────────────────────────────

def _register(tel_bot, pyro_bot, bot_index: int):
    """Bind all standard command/message handlers to one extra bot pair."""

    # /cancel ─────────────────────────────────────────────────────────────────
    @tel_bot.on(events.NewMessage(incoming=True, pattern='/cancel'))
    async def _cancel(event):
        uid = event.sender_id
        if active_batches.get(uid) is False:
            active_batches[uid] = True
            await event.respond("✅ Batch cancelled.")
        else:
            await event.respond("There is no running batch to cancel.")

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
    @tel_bot.on(events.NewMessage(incoming=True, pattern='/batch'))
    async def _bulk(event):
        uid = event.sender_id

        if active_batches.get(uid) is False:
            return await event.reply(
                "A batch is already running. Use /cancel to stop it first."
            )

        chat_ref = None
        start_msg = end_msg = 0

        async with tel_bot.conversation(event.chat_id, timeout=120) as conv:
            try:
                await conv.send_message(
                    "Send the message link with a **start–end range**.\n\n"
                    "**Examples:**\n"
                    "• `https://t.me/c/2133410746/926447-926450` — private channel\n"
                    "• `https://t.me/c/3765531856/4/23-25` — supergroup topic\n"
                    "• `https://t.me/username/100-120` — public channel\n\n"
                    "_For a single file, just send the normal link without a range._",
                    buttons=Button.force_reply()
                )
                link_msg = await conv.get_reply()

                raw = link_msg.text.strip() if link_msg.text else ""
                _link = get_link(raw) or raw

                if not _link:
                    await conv.send_message(
                        "No valid link found. Please try /batch again."
                    )
                    return

                parsed = _parse_link(_link)
                if not parsed:
                    await conv.send_message(
                        "❌ Could not read a message range from that link.\n"
                        "Make sure it ends like `…/START-END` (e.g. `…/100-150`)."
                    )
                    return

                chat_ref, start_msg, end_msg = parsed
                total = end_msg - start_msg + 1

                if total < 1:
                    await conv.send_message("End ID must be ≥ start ID.")
                    return
                if total > 10000:
                    await conv.send_message(
                        "Max range is 10 000 messages per batch."
                    )
                    return

                active_batches[uid] = False
                await conv.send_message(
                    f"🚀 **Batch starting** (Bot #{bot_index})\n"
                    f"Chat: `{chat_ref}`\n"
                    f"Range: `{start_msg}` → `{end_msg}` ({total} messages)\n\n"
                    "Use /cancel to stop."
                )

            except asyncio.TimeoutError:
                await event.respond("⏳ Timed out. Please try /batch again.")
                return
            except Exception as e:
                logger.info(e)
                await event.respond(f"Error: {e}")
                return

        # Resolve user account (shared userbot first, then per-user session)
        acc = userbot
        personal_acc = None

        if acc is None:
            sess = _get_user_session(uid)
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
            active_batches.pop(uid, None)
            return await pyro_bot.send_message(
                uid,
                "❌ **No user session available.**\n\n"
                "A Telegram user account is required to access private/restricted channels.\n"
                "👉 Use /login to authenticate on the **main bot**, then try /batch again.",
            )

        try:
            await _run_batch(acc, pyro_bot, uid, chat_ref, start_msg, end_msg)
        finally:
            if personal_acc:
                try:
                    await personal_acc.stop()
                except Exception:
                    pass
            active_batches.pop(uid, None)

    # Single-link clone (any private message that looks like a Telegram link) ─
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
            try:
                link = get_link(line)
                if not link:
                    return
            except TypeError:
                return

            if _is_range_link(link):
                return

            if "|" in line:
                parts = line.split("|")
                if len(parts) == 2:
                    file_name = parts[1].strip()

            edit = await event.respond("Processing!")

            acc = userbot
            tmp_client = None

            if acc is None:
                sess = _get_user_session(event.sender_id)
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
                    "Use /login to log in with the main bot."
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
                await tel_bot.send_message(
                    event.sender_id,
                    f"Error: {str(e)}"
                )
                await edit.delete()
            finally:
                if tmp_client:
                    try:
                        await tmp_client.stop()
                    except Exception:
                        pass

            time.sleep(1)


# ── Register handlers on every configured extra bot ───────────────────────────

if extra_clients:
    for _i, (_tel, _pyro) in enumerate(extra_clients, start=2):
        _register(_tel, _pyro, _i)
    print(f"[multi_bot] Registered handlers on {len(extra_clients)} extra bot(s).")
else:
    print("[multi_bot] No extra bots configured (BOT_TOKEN2/3/4 not set).")

