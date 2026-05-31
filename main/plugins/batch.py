import logging
import os
import sys
import asyncio
import json
import re

from .. import bot as gagan
from .. import userbot, Bot, API_ID, API_HASH

from main.plugins.pyroplug import download_msg, upload_downloaded, prefetch_msg
from main.plugins.helpers import get_link

from telethon import events, Button
from pyrogram import Client
from pyrogram.errors import FloodWait


# ── Per-user session helper ───────────────────────────────────────────────────

async def _get_user_session(user_id):
    """Async — reads from MongoDB if configured, else falls back to file."""
    from main.plugins.session_store import get_user_session as _gs
    return await _gs(user_id)


# ── Logging / stdout redirect ─────────────────────────────────────────────────

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

temp_log_file = "logs.txt"

if not os.path.exists(temp_log_file):
    with open(temp_log_file, "w"):
        pass


class StreamToLogger:
    def __init__(self, lg, level, path):
        self.logger = lg
        self.log_level = level
        self.log_file = path

    def write(self, buf):
        with open(self.log_file, 'a') as f:
            f.write(buf)
        for line in buf.rstrip().splitlines():
            self.logger.log(self.log_level, line.rstrip())

    def flush(self):
        pass

    def fileno(self):
        return 0


for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(filename=temp_log_file, level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

sys.stdout = StreamToLogger(logging.getLogger('STDOUT'), logging.INFO, temp_log_file)
sys.stderr = StreamToLogger(logging.getLogger('STDERR'), logging.ERROR, temp_log_file)


def _reset_log():
    try:
        open(temp_log_file, "w").close()
    except Exception:
        pass


async def _log_loop():
    while True:
        await asyncio.sleep(180)
        _reset_log()


try:
    asyncio.get_running_loop().create_task(_log_loop())
except RuntimeError:
    pass


# ── /logs ─────────────────────────────────────────────────────────────

@gagan.on(events.NewMessage(incoming=True, pattern='/logs'))
async def send_log(event):
    if os.path.exists(temp_log_file):
        await gagan.send_file(event.sender_id, temp_log_file,
                              caption="Log file (last 3 min).")
    else:
        await event.respond("Log file not found.")


# ── active batch tracker ──────────────────────────────────────────────────────
# user_id → False = running, True = cancelled

active_batches = {}


# ── /cancel ─────────────────────────────────────────────────────────────

@gagan.on(events.NewMessage(incoming=True, pattern='/cancel'))
async def cancel_command(event):
    uid = event.sender_id
    if active_batches.get(uid) is False:
        active_batches[uid] = True
        await event.respond("✅ Batch cancelled.")
    else:
        await event.respond("There is no running batch to cancel.")


# ── Link parsers ───────────────────────────────────────────────────────────

def _parse_single_link(link: str):
    """
    Parse one complete Telegram message URL.
    Returns (chat_ref, topic_id_or_None, msg_id) or None.
    """
    clean = link.rstrip("/").split("?")[0]
    parts = clean.split("/")
    if len(parts) < 5:
        return None

    if "t.me/c/" in link:
        try:
            chat_ref = int("-100" + parts[4])
        except (ValueError, IndexError):
            return None
        if len(parts) >= 7:
            try:
                topic_id = int(parts[5])
                msg_id   = int(parts[6])
            except (ValueError, IndexError):
                return None
            return chat_ref, topic_id, msg_id
        else:
            try:
                msg_id = int(parts[5])
            except (ValueError, IndexError):
                return None
            return chat_ref, None, msg_id
    else:
        try:
            username = parts[3]
            msg_id   = int(parts[-1])
        except (ValueError, IndexError):
            return None
        if not username or username.lower() in ("c", "b"):
            return None
        return username, None, msg_id


def _parse_range(raw: str):
    """
    Parse a batch range in either of two formats:

    NEW  —  two full URLs joined by a hyphen:
      https://t.me/c/CHATID/TOPICID/MSGID-https://t.me/c/CHATID/TOPICID/MSGID
      https://t.me/c/CHATID/MSGID-https://t.me/c/CHATID/MSGID
      https://t.me/USERNAME/MSGID-https://t.me/USERNAME/MSGID

    OLD  —  single URL whose last segment is START-END:
      https://t.me/c/CHATID/TOPICID/START-END
      https://t.me/c/CHATID/START-END
      https://t.me/USERNAME/START-END

    Returns (chat_ref, start_topic, start_msg, end_topic, end_msg) or None.
    """
    raw = raw.strip()

    if raw.count('https://') >= 2 or raw.count('http://') >= 2:
        for sep in ('-https://', '- https://'):
            idx = raw.find(sep)
            if idx != -1:
                link1 = raw[:idx].strip()
                link2 = 'https://' + raw[idx + len(sep):]
                p1 = _parse_single_link(link1)
                p2 = _parse_single_link(link2)
                if p1 and p2 and p1[0] == p2[0]:
                    return p1[0], p1[1], p1[2], p2[1], p2[2]
        return None

    link = get_link(raw) or raw
    clean = link.rstrip("/").split("?")[0]
    parts = clean.split("/")
    if not parts:
        return None

    last = parts[-1]
    if "-" in last:
        segs = last.split("-", 1)
        try:
            start_msg = int(segs[0])
            end_msg   = int(segs[1])
        except ValueError:
            return None
        base_link = "/".join(parts[:-1] + [str(start_msg)])
    else:
        try:
            start_msg = end_msg = int(last)
        except ValueError:
            return None
        base_link = link

    p = _parse_single_link(base_link)
    if not p:
        return None
    chat_ref, topic_id, _ = p
    return chat_ref, topic_id, start_msg, topic_id, end_msg


# ── /batch ─────────────────────────────────────────────────────────────

@gagan.on(events.NewMessage(incoming=True, pattern=r'^/batch(?:\s|$|@)'))
async def _bulk(event):
    uid = event.sender_id

    if active_batches.get(uid) is False:
        return await event.reply("A batch is already running. Use /cancel to stop it first.")

    parsed = None

    async with gagan.conversation(event.chat_id, timeout=120) as conv:
        try:
            await conv.send_message(
                "Send the message link with a **start–end range**.\n\n"
                "**Format — paste START link, a hyphen, then END link:**\n"
                "• `https://t.me/c/2133410746/926447-https://t.me/c/2133410746/926450`\n"
                "  _(private channel — msgs 926447 → 926450)_\n\n"
                "• `https://t.me/c/3765531856/4/23-https://t.me/c/3765531856/8/270`\n"
                "  _(supergroup topics 4→8, from msg 23 up to msg 270)_\n\n"
                "• `https://t.me/username/100-https://t.me/username/125`\n"
                "  _(public channel — msgs 100 → 125)_\n\n"
                "**Old compact format still works too:**\n"
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
                    "❌ Could not parse a range from that input.\n\n"
                    "Use the format:\n"
                    "`START_LINK-END_LINK`\n"
                    "e.g. `https://t.me/c/3765531856/4/23-https://t.me/c/3765531856/8/270`"
                )
                return

            chat_ref, start_topic, start_msg, end_topic, end_msg = parsed

            if end_msg < start_msg and start_topic == end_topic:
                await conv.send_message("End message ID must be ≥ start message ID.")
                return

            active_batches[uid] = False
            if start_topic is not None and start_topic != end_topic:
                desc = (f"Topics `{start_topic}` → `{end_topic}`\n"
                        f"From msg `{start_msg}` … to msg `{end_msg}`")
            else:
                desc = f"Msgs `{start_msg}` → `{end_msg}`"

            status = await conv.send_message(
                f"🚀 **Batch starting**\n"
                f"Chat: `{chat_ref}`\n"
                f"{desc}\n\n"
                "⏳ Scanning messages…"
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
                    f"batch_{uid}",
                    session_string=sess,
                    api_id=int(API_ID),
                    api_hash=API_HASH,
                    in_memory=True
                )
                await personal_acc.start()
                acc = personal_acc
            except Exception as e:
                await Bot.send_message(uid, f"⚠️ Could not start your session: `{e}`")
                personal_acc = None

    if acc is None and isinstance(chat_ref, int):
        active_batches.pop(uid, None)
        return await Bot.send_message(
            uid,
            "❌ **No user session available.**\n\n"
            "A Telegram user account is required to access private/restricted channels.\n"
            "👉 Use /login to authenticate, then try /batch again."
        )

    try:
        await _run_batch(acc, Bot, uid, chat_ref,
                         start_topic, start_msg, end_topic, end_msg,
                         status_msg=status)
    finally:
        if personal_acc:
            try:
                await personal_acc.stop()
            except Exception:
                pass
        active_batches.pop(uid, None)


# ── Fast last-message-ID resolver (used by no-prescan stream mode) ────────────

async def _fetch_topic_last_id(acc, chat_ref, topic_id: int) -> int:
    """
    Return the ID of the most recent message in a forum topic via GetReplies.
    Returns 0 on any error (caller should skip the topic).
    Properly handles FloodWait — waits and retries instead of silently returning 0.
    """
    from pyrogram.raw.functions.messages import GetReplies
    from pyrogram.errors import FloodWait

    async def _invoke_once():
        peer   = await acc.resolve_peer(chat_ref)
        result = await acc.invoke(
            GetReplies(
                peer=peer,
                msg_id=topic_id,
                offset_id=0,
                offset_date=0,
                add_offset=0,
                limit=1,
                max_id=0,
                min_id=0,
                hash=0,
            )
        )
        if result.messages:
            return result.messages[0].id
        return 0

    try:
        return await _invoke_once()
    except FloodWait as fw:
        wait = fw.value + 2
        logger.warning(f"_fetch_topic_last_id FloodWait {wait}s (topic={topic_id})")
        await asyncio.sleep(wait)
        try:
            return await _invoke_once()
        except Exception as e2:
            logger.warning(f"_fetch_topic_last_id retry failed (topic={topic_id}): {e2}")
    except Exception as e:
        logger.warning(f"_fetch_topic_last_id(topic={topic_id}): {e}")
    return 0


# ── Forum topic discovery (GetForumTopics) ────────────────────────────────────

_TOPIC_FETCH_TIMEOUT    = 20   # seconds — per-topic GetReplies (full fetch) max wait
_TOPIC_DISCOVER_TIMEOUT = 10   # seconds — per-probe GetReplies(limit=1) max wait


_DISCOVER_CONCURRENCY = 3    # parallel topic-existence checks (low to avoid FloodWait)


async def _discover_forum_topics(acc, peer, start_id: int, end_id: int) -> "list[int] | None":
    """
    Return a sorted list of topic IDs within [start_id, end_id] that actually
    have at least one message (i.e. are real, non-empty topics).

    Uses GetReplies(limit=1) probes run _DISCOVER_CONCURRENCY at a time so
    discovery is ~8x faster than the sequential per-topic scan while keeping
    API load reasonable.  Each probe is capped at _TOPIC_DISCOVER_TIMEOUT
    seconds to prevent hangs on network stalls.

    Returns None on unexpected failure so the caller falls back to sequential
    probing.  An empty list means no messages were found in any topic in range.
    """
    from pyrogram.raw.functions.messages import GetReplies
    from pyrogram.errors import FloodWait

    sem = asyncio.Semaphore(_DISCOVER_CONCURRENCY)

    async def _check(tid: int) -> "int | None":
        """Return tid if the topic has messages, else None."""
        async with sem:
            for _attempt in range(3):
                try:
                    r = await asyncio.wait_for(
                        acc.invoke(
                            GetReplies(
                                peer=peer,
                                msg_id=tid,
                                offset_id=0,
                                offset_date=0,
                                add_offset=0,
                                limit=1,        # only need to know "exists?"
                                max_id=0,
                                min_id=0,
                                hash=0,
                            )
                        ),
                        timeout=_TOPIC_DISCOVER_TIMEOUT,
                    )
                    return tid if r.messages else None
                except asyncio.TimeoutError:
                    logger.debug(f"_discover: topic {tid} probe timed out")
                    return None
                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 1)
                    continue
                except Exception:
                    return None
            return None

    all_ids = list(range(start_id, end_id + 1))
    try:
        results = await asyncio.gather(*[_check(tid) for tid in all_ids])
    except Exception as e:
        logger.warning(f"_discover_forum_topics: gather failed: {e}")
        return None

    found = sorted(tid for tid in results if tid is not None)
    return found   # empty list is valid: caller decides what to do


# ── Direct topic-message-ID fetcher (GetReplies pagination) ──────────────────

async def _fetch_topic_msg_ids(acc, chat_ref, topic_id: int,
                                min_id: int = 1, max_id: int = 0,
                                peer=None) -> list:
    """
    Return all message IDs that actually exist in a forum topic, sorted ascending.

    Uses GetReplies pagination (newest-first) and collects every ID >= min_id
    (and <= max_id when max_id > 0).  This avoids scanning the entire global
    message-ID space for a topic whose IDs may start at a very high number.

    peer — pass an already-resolved peer to avoid a redundant resolve_peer call.
    Returns an empty list when the topic is empty / deleted / inaccessible.
    Each GetReplies invoke is guarded by _TOPIC_FETCH_TIMEOUT to prevent hangs.
    """
    from pyrogram.raw.functions.messages import GetReplies
    from pyrogram.errors import FloodWait

    all_ids   = []
    offset_id = 0          # 0 → start from newest message
    seen      = set()

    if peer is None:
        try:
            peer = await acc.resolve_peer(chat_ref)
        except Exception as e:
            logger.warning(f"_fetch_topic_msg_ids resolve_peer failed: {e}")
            return []

    page_retries = 0
    while True:
        try:
            result = await asyncio.wait_for(
                acc.invoke(
                    GetReplies(
                        peer=peer,
                        msg_id=topic_id,
                        offset_id=offset_id,
                        offset_date=0,
                        add_offset=0,
                        limit=100,
                        max_id=0,
                        min_id=max(0, min_id - 1),   # exclusive lower bound
                        hash=0,
                    )
                ),
                timeout=_TOPIC_FETCH_TIMEOUT,
            )
            page_retries = 0   # successful page — reset retry counter
        except asyncio.TimeoutError:
            page_retries += 1
            if page_retries <= 3:
                logger.warning(
                    f"_fetch_topic_msg_ids: timeout page retry {page_retries}/3 "
                    f"(topic={topic_id}, offset={offset_id})"
                )
                await asyncio.sleep(2 * page_retries)   # short back-off before retry
                continue
            # Gave up on this page — stop here rather than silently lose older msgs
            logger.warning(
                f"_fetch_topic_msg_ids: page timed out 3x, stopping "
                f"(topic={topic_id}, offset={offset_id}, collected={len(all_ids)})"
            )
            break
        except FloodWait as fw:
            wait = fw.value + 2
            logger.warning(f"_fetch_topic_msg_ids FloodWait {wait}s (topic={topic_id})")
            await asyncio.sleep(wait)
            continue
        except Exception as e:
            logger.warning(f"_fetch_topic_msg_ids error (topic={topic_id}): {e}")
            break

        if not result.messages:
            break

        batch_min = offset_id   # track oldest ID in this batch
        for m in result.messages:
            mid = m.id
            if mid in seen:
                continue
            seen.add(mid)
            if mid < min_id:
                continue
            if max_id > 0 and mid > max_id:
                continue
            all_ids.append(mid)
            if batch_min == 0 or mid < batch_min:
                batch_min = mid

        # Stop if we've reached or passed the min_id boundary
        if batch_min <= min_id:
            break

        # Next page: fetch messages older than the oldest we just saw
        offset_id = batch_min

    return sorted(all_ids)


# ── Stream-mode batch (no prescan) ────────────────────────────────────────────

async def _run_batch_noScan(acc, client, sender, chat_ref, raw_chat,
                             start_topic, start_msg, end_topic, end_msg,
                             _bdict, status_msg, collected_ids,
                             checkpoint_fn=None):
    """
    Fast extraction without ID-range scanning.

    For topics: first tries GetForumTopics to discover which topic IDs actually
    exist in the range, then uses GetReplies only for those real topics.
    Falls back to sequential probing when GetForumTopics is unavailable.
    Every API call is guarded by a timeout so bots can never hang forever.

    For plain channels (no topic): uses a direct ID range as before.
    """
    has_topics   = start_topic is not None and start_topic != end_topic
    single_topic = start_topic is not None and start_topic == end_topic

    saved     = 0
    skipped   = 0
    cancelled = False

    def _make_link(topic, mid):
        if isinstance(chat_ref, int):
            if topic is not None:
                return f"https://t.me/c/{raw_chat}/{topic}/{mid}"
            return f"https://t.me/c/{raw_chat}/{mid}"
        return f"https://t.me/{raw_chat}/{mid}"

    # ── Pre-resolve peer ONCE for all topic/message lookups ──────────────────
    # Avoids a redundant resolve_peer() call per topic (which is an API round-trip).
    shared_peer = None
    if has_topics or single_topic:
        try:
            shared_peer = await acc.resolve_peer(chat_ref)
        except Exception as e:
            logger.warning(f"_run_batch_noScan: resolve_peer failed: {e}")

    # ── Discover real topic IDs in range (multi-topic mode) ───────────────────
    if has_topics and shared_peer is not None:
        n_ids = end_topic - start_topic + 1
        try:
            await status_msg.edit_text(
                f"🔍 Scanning {n_ids} topic IDs ({start_topic}→{end_topic}) "
                f"with {_DISCOVER_CONCURRENCY} parallel probes…"
            )
        except Exception:
            pass

        discovered = await _discover_forum_topics(
            acc, shared_peer, start_topic, end_topic
        )

        if discovered is not None and len(discovered) > 0:
            topics = discovered
            logger.info(
                f"_run_batch_noScan: discovered {len(topics)} real topics "
                f"in range {start_topic}→{end_topic} "
                f"(skipped {(end_topic - start_topic + 1) - len(topics)} non-existent IDs)"
            )
            try:
                await status_msg.edit_text(
                    f"✅ Found {len(topics)} topic(s) in range {start_topic}→{end_topic}\n"
                    f"⏳ Starting extraction…"
                )
            except Exception:
                pass
        elif discovered is not None and len(discovered) == 0:
            # Discovery returned empty — could be FloodWait killing all probes
            # (not just a truly empty range).  Fall back to sequential scan so
            # we never silently skip real content.
            topics = list(range(start_topic, end_topic + 1))
            logger.warning(
                f"_run_batch_noScan: discovery returned 0 topics in "
                f"{start_topic}→{end_topic} — falling back to sequential scan "
                f"({len(topics)} IDs)"
            )
            try:
                await status_msg.edit_text(
                    f"⚠️ Discovery found 0 topics — scanning {len(topics)} IDs sequentially…"
                )
            except Exception:
                pass
        else:
            # Discovery unavailable (not a forum or API error) — fall back
            topics = list(range(start_topic, end_topic + 1))
            logger.info(
                f"_run_batch_noScan: discovery unavailable — "
                f"probing {len(topics)} IDs sequentially"
            )
    elif has_topics:
        # No peer resolved — fall back to full range
        topics = list(range(start_topic, end_topic + 1))
    elif single_topic:
        topics = [start_topic]
    else:
        topics = [None]

    total_topics    = len(topics)
    consecutive_empty = 0   # track consecutive skipped/empty topics for status

    for topic_num, topic in enumerate(topics, 1):
        if _bdict.get(sender):
            cancelled = True
            break

        # ── Yield to event loop so other coroutines stay alive ────────────
        await asyncio.sleep(0)

        # ── Status update — only on real progress or every 10 skips ──────
        if total_topics > 1:
            show_status = (consecutive_empty == 0 or consecutive_empty % 10 == 0
                           or topic_num == 1 or topic_num == total_topics)
            if show_status:
                skip_note = (f" | ⏭️ {consecutive_empty} empty"
                             if consecutive_empty > 0 else "")
                try:
                    await status_msg.edit_text(
                        f"⏳ Topic `{topic}` ({topic_num}/{total_topics}){skip_note}\n"
                        f"✅ Saved: `{saved}` | ⏭️ Skipped: `{skipped}`"
                    )
                except Exception:
                    pass

        # ── Resolve the list of message IDs to process ─────────────────────
        if topic is not None:
            # For topics: use GetReplies to get only real IDs — avoids scanning
            # the entire global ID space from 1..last_mid for non-start topics.
            first_mid    = start_msg if topic == start_topic else 1
            last_mid_cap = end_msg   if (topic == end_topic and end_msg != 999_999_999) else 0

            msg_ids = await _fetch_topic_msg_ids(
                acc, chat_ref, topic,
                min_id=first_mid,
                max_id=last_mid_cap,
                peer=shared_peer,          # reuse pre-resolved peer — no extra API call
            )
            if not msg_ids:
                # Topic empty, deleted, timed-out, or doesn't exist — skip instantly
                consecutive_empty += 1
                skipped += 1
                continue

            consecutive_empty = 0          # reset consecutive counter on found topic
            end_link = _make_link(topic, msg_ids[-1])

        else:
            # Plain channel / no topic — keep original range approach
            first_mid = start_msg
            last_mid  = end_msg
            end_link  = _make_link(None, last_mid)
            msg_ids   = list(range(first_mid, last_mid + 1))

        # ── Process msg_ids in groups of GROUP_SIZE ─────────────────────────
        for group_start in range(0, len(msg_ids), GROUP_SIZE):
            if _bdict.get(sender):
                cancelled = True
                break

            group_ids = msg_ids[group_start : group_start + GROUP_SIZE]
            g         = len(group_ids)

            ready_ev = [asyncio.Event() for _ in range(g)]
            go_ev    = [asyncio.Event() for _ in range(g)]
            done_ev  = [asyncio.Event() for _ in range(g)]

            group_tasks = []

            for g_idx, mid in enumerate(group_ids):
                if _bdict.get(sender):
                    cancelled = True
                    for rem in range(g_idx, g):
                        ready_ev[rem].set()
                        done_ev[rem].set()
                    break

                link = _make_link(topic, mid)

                prefetched = await prefetch_msg(acc, link, mid)

                # ── Skip empty / deleted / non-media messages ──────────────
                if prefetched is None:
                    ready_ev[g_idx].set()
                    done_ev[g_idx].set()
                    skipped += 1
                    continue

                # ── Skip non-video content (images, documents, text, etc.) ──
                # Only extract VIDEO, VIDEO_NOTE, and ANIMATION to avoid errors
                # caused by non-media or unsupported file types.
                try:
                    from pyrogram.enums import MessageMediaType as _MMT
                    _VIDEO_TYPES = {_MMT.VIDEO, _MMT.VIDEO_NOTE, _MMT.ANIMATION}
                    if prefetched.media not in _VIDEO_TYPES:
                        ready_ev[g_idx].set()
                        done_ev[g_idx].set()
                        skipped += 1
                        continue
                except Exception:
                    pass   # if check fails, proceed normally

                # For topic-based msg_ids we already filtered by topic via
                # GetReplies, so no secondary topic-match check is needed.
                # For the None-topic (plain channel) case there is no topic to check.

                msg_footer = f"🔗 {link}\n📋 End: {end_link}"

                try:
                    pm = await client.send_message(
                        sender, f"⬇️ Downloading…\n{link}"
                    )
                except Exception as e:
                    logger.error(f"stream: progress msg send failed ({link}): {e}")
                    ready_ev[g_idx].set()
                    done_ev[g_idx].set()
                    skipped += 1
                    continue

                dl = await download_msg(
                    acc, client, sender, link, mid,
                    source_link="⬇️ Downloading",
                    batch_range=msg_footer,
                    prefetched_msg=prefetched,
                    progress_msg=pm,
                )

                if dl is None:
                    try:
                        await pm.delete()
                    except Exception:
                        pass
                    ready_ev[g_idx].set()
                    done_ev[g_idx].set()
                    skipped += 1
                    continue

                msg_obj, file_str, returned_pm = dl

                # ── Barrier progress callback ──────────────────────────────
                from main.plugins.progress import progress_for_pyrogram as _base_prog
                _signalled = [False]
                _rdy       = ready_ev[g_idx]
                _go        = go_ev[g_idx]

                async def _pf(current, total, bot, ud_type, message, start, footer,
                               __signalled=_signalled, __rdy=_rdy, __go=_go):
                    remaining = total - current
                    if not __signalled[0] and (
                        total <= NEAR_FINISH_THRESHOLD or
                        remaining <= NEAR_FINISH_THRESHOLD or
                        current == total
                    ):
                        __signalled[0] = True
                        __rdy.set()
                        await __go.wait()
                    await _base_prog(current, total, bot, ud_type, message, start, footer)

                async def _upload_task(
                    _g_idx=g_idx, _link=link, _footer=msg_footer,
                    _msg=msg_obj, _fs=file_str, _pm=returned_pm, _pf_fn=_pf,
                    _done=done_ev[g_idx], _rdy=ready_ev[g_idx],
                    _mid=mid,                   # source msg ID for checkpointing
                    _topic=topic,               # source topic ID for checkpointing
                    _cpfn=checkpoint_fn,         # checkpoint callback (may be None)
                ):
                    try:
                        sent = await upload_downloaded(
                            acc, client, sender, _msg, _fs, _pm,
                            source_link="⬆️ Uploading",
                            batch_range=_footer,
                            _progress_fn=_pf_fn,
                        )
                        if sent is not None and collected_ids is not None:
                            try:
                                collected_ids.append(sent.id)
                            except Exception:
                                pass
                        # Checkpoint AFTER confirmed upload — guarantees crash-safe resume.
                        # Pass both msg ID and topic ID so resume knows the exact position.
                        if sent is not None and _cpfn is not None:
                            try:
                                await _cpfn(_mid, _topic)
                            except Exception as _ce:
                                logger.warning(f"checkpoint_fn error mid={_mid} topic={_topic}: {_ce}")
                        return bool(sent)
                    except Exception as e:
                        logger.error(f"stream upload error g_idx={_g_idx}: {e}")
                        return False
                    finally:
                        # Always unblock the coordinator even if _pf was never
                        # called (e.g. photos bypass the progress callback path).
                        _rdy.set()
                        _done.set()

                group_tasks.append((g_idx, asyncio.create_task(_upload_task())))

            # ── Group coordinator: ordered release ─────────────────────────
            async def _coord(_g=g, _rev=ready_ev, _gev=go_ev, _dev=done_ev):
                for ev in _rev:
                    await ev.wait()
                for i in range(_g):
                    _gev[i].set()
                    await _dev[i].wait()

            await _coord()
            if group_tasks:
                results = await asyncio.gather(
                    *[t for _, t in group_tasks], return_exceptions=True
                )
                # Count actual confirmed successful uploads
                for r in results:
                    if r is True:
                        saved += 1

        if cancelled:
            break

    summary = (
        f"{'🚫 Batch cancelled' if cancelled or _bdict.get(sender) else '✅ **Batch complete!**'}\n\n"
        f"📦 **Saved:** `{saved}`\n"
        f"⏭️ **Skipped** (empty / error): `{skipped}`"
    )
    try:
        await status_msg.edit_text(summary)
    except Exception:
        await client.send_message(sender, summary)


# ── Batch scan ────────────────────────────────────────────────────────────

SCAN_BATCH = 100          # max messages per get_messages call
MAX_EMPTY_BATCHES = 2     # stop a topic scan after this many all-empty batches


async def _scan_topic(acc, chat_id, topic_id, start_mid, end_mid, seen_ids):
    """
    Scan [start_mid, end_mid] (or open-ended if end_mid is None) and return
    sorted list of msg_ids that exist, belong to topic_id, and aren't in seen_ids.
    Updates seen_ids in place to prevent cross-topic duplicates.
    """
    valid = []
    empty_batches = 0
    mid = start_mid

    while True:
        if end_mid is not None and mid > end_mid:
            break
        # Stop on consecutive empty batches regardless of whether end_mid is set.
        # This prevents 999_999_999-style open-upper-bound scans from looping
        # through millions of non-existent message IDs.
        if empty_batches >= MAX_EMPTY_BATCHES:
            break

        chunk_end = (mid + SCAN_BATCH - 1) if end_mid is None else min(mid + SCAN_BATCH - 1, end_mid)
        ids = list(range(mid, chunk_end + 1))

        try:
            msgs_list = await acc.get_messages(chat_id, ids)
        except Exception as e:
            logger.error(f"_scan_topic get_messages error (chat={chat_id}, ids={ids[0]}-{ids[-1]}): {e}")
            mid = chunk_end + 1
            empty_batches += 1
            continue

        found_in_chunk = False
        for m in sorted(msgs_list, key=lambda x: x.id):
            if m.empty or m.service:
                continue
            mid_val = m.id
            if mid_val in seen_ids:
                continue

            # Topic verification for forum supergroups.
            if topic_id is not None:
                tid = (
                    getattr(m, 'message_thread_id', None)
                    or getattr(m, 'reply_to_top_message_id', None)
                )
                if tid is not None and tid != topic_id:
                    continue

            seen_ids.add(mid_val)
            valid.append(mid_val)
            found_in_chunk = True

        empty_batches = 0 if found_in_chunk else empty_batches + 1
        mid = chunk_end + 1

    return sorted(valid)


async def _build_items(acc, client, sender, chat_ref, raw_chat,
                       start_topic, start_msg, end_topic, end_msg,
                       status_msg):
    """
    Collect all (topic, msg_id, link_str) triples in chronological order.
    """
    has_topics = start_topic is not None
    seen_ids = set()
    items = []

    def _make_link(topic, mid):
        if isinstance(chat_ref, int):
            if topic is not None:
                return f"https://t.me/c/{raw_chat}/{topic}/{mid}"
            return f"https://t.me/c/{raw_chat}/{mid}"
        return f"https://t.me/{raw_chat}/{mid}"

    if has_topics and start_topic != end_topic:
        topics = list(range(start_topic, end_topic + 1))
        total_topics = len(topics)

        for i, topic in enumerate(topics):
            first_mid = start_msg if topic == start_topic else 1
            last_mid  = end_msg   if topic == end_topic   else None

            try:
                await status_msg.edit_text(
                    f"🔍 Scanning topic `{topic}` ({i+1}/{total_topics})…"
                )
            except Exception:
                pass

            valid_ids = await _scan_topic(
                acc, chat_ref, topic, first_mid, last_mid, seen_ids
            )

            for mid in valid_ids:
                items.append((topic, mid, _make_link(topic, mid)))

    elif has_topics:
        valid_ids = await _scan_topic(
            acc, chat_ref, start_topic, start_msg, end_msg, seen_ids
        )
        for mid in valid_ids:
            items.append((start_topic, mid, _make_link(start_topic, mid)))

    else:
        for mid in range(start_msg, end_msg + 1):
            items.append((None, mid, _make_link(None, mid)))

    return items


# ── Batch runner (GROUP-BASED, ordered finish per group) ─────────────────────

GROUP_SIZE = 3                     # process 3 files at a time
NEAR_FINISH_THRESHOLD = 3 * 1024 * 1024   # 3 MB


async def _run_batch(acc, client, sender, chat_ref,
                     start_topic, start_msg, end_topic, end_msg,
                     batches_dict=None, status_msg=None, collected_ids=None,
                     no_prescan=False, checkpoint_fn=None):
    _bdict = batches_dict if batches_dict is not None else active_batches

    if isinstance(chat_ref, int):
        raw_chat = str(chat_ref).replace("-100", "")
    else:
        raw_chat = str(chat_ref)

    # ── Send (or reuse) the single status message ─────────────────────────────
    if status_msg is None:
        if start_topic is not None and start_topic != end_topic:
            display = (f"Topics `{start_topic}`→`{end_topic}`, "
                       f"msgs `{start_msg}`→…→`{end_msg}`")
        elif start_topic is not None:
            display = f"Topic `{start_topic}`, msgs `{start_msg}`→`{end_msg}`"
        else:
            display = f"Msgs `{start_msg}` → `{end_msg}`"

        status_msg = await client.send_message(
            sender,
            f"🔍 **Batch starting**\nRange: {display}\n⏳ Starting…"
        )

    # ── Fast stream mode: skip scan phase entirely ────────────────────────────
    if no_prescan:
        await _run_batch_noScan(
            acc, client, sender, chat_ref, raw_chat,
            start_topic, start_msg, end_topic, end_msg,
            _bdict, status_msg, collected_ids,
            checkpoint_fn=checkpoint_fn,
        )
        return

    # ── Phase 1: scan ─────────────────────────────────────────────────────────
    items = await _build_items(
        acc, client, sender, chat_ref, raw_chat,
        start_topic, start_msg, end_topic, end_msg,
        status_msg
    )

    n = len(items)
    if n == 0:
        try:
            await status_msg.edit_text("⚠️ No messages found in the given range.")
        except Exception:
            await client.send_message(sender, "⚠️ No messages found in the given range.")
        return

    try:
        await status_msg.edit_text(
            f"✅ Found **{n}** message(s) — processing groups of {GROUP_SIZE}\n"
            f"⬇️ Downloads serial, ⬆️ uploads parallel | ordered finish per group"
        )
    except Exception:
        pass

    overall_range = f"{items[0][2]}-{items[-1][2]}"

    # ── Phase 2: pre-fetch ALL messages in parallel (so we don't wait later) ──
    fetched_all = await asyncio.gather(*[
        prefetch_msg(acc, link, mid)
        for (topic, mid, link) in items
    ], return_exceptions=True)
    fetched_all = [
        v if not isinstance(v, BaseException) else None for v in fetched_all
    ]

    # Barrier events for every item (global arrays)
    ready_events = [asyncio.Event() for _ in range(n)]
    go_events    = [asyncio.Event() for _ in range(n)]
    done_events  = [asyncio.Event() for _ in range(n)]

    def _make_barrier_progress(idx):
        """Progress callback that pauses near the finish line for item 'idx'."""
        from main.plugins.progress import progress_for_pyrogram as _base_prog
        signalled = [False]
        rdy = ready_events[idx]
        go  = go_events[idx]

        async def _prog(current, total, bot, ud_type, message, start, footer):
            nonlocal signalled
            remaining = total - current
            if not signalled[0] and (
                total <= NEAR_FINISH_THRESHOLD or
                remaining <= NEAR_FINISH_THRESHOLD or
                current == total
            ):
                signalled[0] = True
                rdy.set()
                await go.wait()
            await _base_prog(current, total, bot, ud_type, message, start, footer)

        return _prog

    saved = 0
    skipped = 0
    cancelled = False

    # ── Phase 3: process groups ───────────────────────────────────────────────
    for group_start in range(0, n, GROUP_SIZE):
        if _bdict.get(sender):
            cancelled = True
            skipped += n - group_start
            break

        group_end = min(group_start + GROUP_SIZE, n)
        group_indices = list(range(group_start, group_end))

        # ── Download each item in the group (sequential) ──────────────────
        group_tasks = []   # (idx, upload_task)

        for idx in group_indices:
            if _bdict.get(sender):
                cancelled = True
                # Mark remaining items as skipped
                for rem_idx in range(idx, group_end):
                    ready_events[rem_idx].set()
                    done_events[rem_idx].set()
                    skipped += 1
                break

            prefetched = fetched_all[idx]
            if prefetched is None:
                # No media – mark as done and skip
                ready_events[idx].set()
                done_events[idx].set()
                skipped += 1
                continue

            topic, mid, link = items[idx]

            # Create progress message
            try:
                pm = await client.send_message(sender, f"⬇️ Downloading…\n`{link}`")
            except Exception as e:
                logger.error(f"Failed to send progress msg for {link}: {e}")
                ready_events[idx].set()
                done_events[idx].set()
                skipped += 1
                continue

            # Download (blocks)
            dl = await download_msg(
                acc, client, sender, link, mid,
                source_link=link,
                batch_range=overall_range,
                prefetched_msg=prefetched,
                progress_msg=pm,
            )

            if dl is None:
                try:
                    await pm.delete()
                except Exception:
                    pass
                ready_events[idx].set()
                done_events[idx].set()
                skipped += 1
                continue

            msg_obj, file_str, returned_pm = dl

            # Spawn upload task immediately (runs in parallel with next downloads)
            pf = _make_barrier_progress(idx)

            # IMPORTANT: msg_obj / file_str / returned_pm / pf are loop variables —
            # capture them as default-argument values so each closure snapshot is
            # independent.  Without this, all tasks in the group share the same
            # (last iteration) values via Python's late-binding closure.
            async def _upload_and_signal(
                idx=idx, link=link,
                _msg=msg_obj, _fs=file_str, _pm=returned_pm, _pf=pf,
            ):
                try:
                    sent = await upload_downloaded(
                        acc, client, sender, _msg, _fs, _pm,
                        source_link=link,
                        batch_range=overall_range,
                        _progress_fn=_pf,
                    )
                    if sent is not None and collected_ids is not None:
                        try:
                            collected_ids.append(sent.id)
                        except Exception:
                            pass
                    return bool(sent)
                except Exception as e:
                    logger.error(f"Upload error idx={idx}: {e}")
                    return False
                finally:
                    done_events[idx].set()

            group_tasks.append((idx, asyncio.create_task(_upload_and_signal())))
            saved += 1   # tentative

        # ── Wait for all uploads in this group to finish (ordered release) ─

        async def _group_coordinator():
            """Release go_events in strict index order, but only after
            all uploads in the group have signalled ready (hit the barrier)."""
            # Wait for all ready events of this group
            for idx in group_indices:
                await ready_events[idx].wait()

            # Now release them in order, waiting for each to complete
            for idx in group_indices:
                go_events[idx].set()
                await done_events[idx].wait()

        # Run the coordinator (it will run until all uploads in the group finish)
        await _group_coordinator()

        # Also wait for all upload tasks to be truly complete (redundant but safe)
        if group_tasks:
            await asyncio.gather(*[t for _, t in group_tasks], return_exceptions=True)

        # Status update after each group
        if (group_end) % 5 == 0 or group_end == n:
            try:
                await status_msg.edit_text(
                    f"🔄 **Processing** ({group_end}/{n})\n"
                    f"✅ Saved: `{saved}` | ⏭️ Skipped: `{skipped}`"
                )
            except Exception:
                pass

    # ── Final summary ─────────────────────────────────────────────────────────
    summary = (
        f"{'🚫 Batch cancelled' if cancelled or _bdict.get(sender) else '✅ **Batch complete!**'}\n\n"
        f"📦 **Saved:** `{saved}`\n"
        f"⏭️ **Skipped** (empty / error): `{skipped}`\n"
        f"📋 **Total scanned:** `{n}`"
    )
    try:
        await status_msg.edit_text(summary)
    except Exception:
        await client.send_message(sender, summary)
        
