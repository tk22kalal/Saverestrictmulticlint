#Join me at telegram @dev_gagan

import os

from pyrogram import Client

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

import logging, time, sys
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

# ── Credentials: env vars first, hardcoded fallback ──────────────────────────
API_ID    = int(os.environ.get("API_ID",    "24058425"))
API_HASH  = os.environ.get("API_HASH",      "694b063e55c24287a3d30aed90191373")
BOT_TOKEN = os.environ.get("BOT_TOKEN",     "8600580531:AAFnpo9I-3e2PH9NnpfEy0KG3i8_zJMLR90")
SESSION   = os.environ.get("SESSION",       "").strip()
FORCESUB  = os.environ.get("FORCESUB",      "forsesubpavo3")
AUTH      = os.environ.get("AUTH",          "7390527029")

# ── Optional extra bot tokens (BOT_TOKEN2, BOT_TOKEN3, BOT_TOKEN4) ───────────
_EXTRA_TOKENS = [
    os.environ.get("BOT_TOKEN2", "").strip(),
    os.environ.get("BOT_TOKEN3", "").strip(),
    os.environ.get("BOT_TOKEN4", "").strip(),
]

SUDO_USERS = set()
if AUTH.strip():
    SUDO_USERS = {int(x.strip()) for x in AUTH.split()}

# ── Telethon bot (always required) ────────────────────────────────────────────
bot = TelegramClient('bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ── Pyrogram userbot (optional — shared by ALL bots) ─────────────────────────
userbot = None
if SESSION:
    try:
        userbot = Client("myacc", api_id=API_ID, api_hash=API_HASH,
                         session_string=SESSION)
        userbot.start()
        print("Global userbot started successfully.")
    except BaseException as e:
        print(f"Warning: Could not start global userbot: {e}")
        print("SESSION env var may be invalid or expired.")
        print("Users can authenticate via /login instead.")
        userbot = None
else:
    print("No SESSION provided — global userbot disabled.")
    print("Users must use /login to access restricted content.")

# ── Pyrogram bot (always required) ────────────────────────────────────────────
Bot = Client(
    "SaveRestricted",
    bot_token=BOT_TOKEN,
    api_id=int(API_ID),
    api_hash=API_HASH
)

try:
    Bot.start()
except Exception as e:
    print(f"Fatal: Could not start Bot client: {e}")
    sys.exit(1)

# ── Extra bots (optional — BOT_TOKEN2 / BOT_TOKEN3 / BOT_TOKEN4) ─────────────
# extra_clients is a list of (TelegramClient, PyrogramClient) tuples.
# All extra bots share the same `userbot` session defined above.
extra_clients = []

for _idx, _token in enumerate(_EXTRA_TOKENS, start=2):
    if not _token:
        continue
    try:
        _tel = TelegramClient(f'bot{_idx}', API_ID, API_HASH).start(bot_token=_token)
        _pyro = Client(
            f"SaveRestricted{_idx}",
            bot_token=_token,
            api_id=int(API_ID),
            api_hash=API_HASH,
        )
        _pyro.start()
        extra_clients.append((_tel, _pyro))
        print(f"Extra bot #{_idx} started successfully.")
    except Exception as _e:
        print(f"Warning: Could not start extra bot #{_idx}: {_e}")
