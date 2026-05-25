#Join me @dev_gagan

import asyncio
import logging
import time

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

botStartTime = time.time()

print("Successfully deployed!")
print("Bot Deployed : Team SPY")

if __name__ == "__main__":
    from . import bot, extra_clients
    import glob
    from pathlib import Path
    from main.utils import load_plugins

    path = "main/plugins/*.py"
    files = glob.glob(path)
    for name in files:
        with open(name) as a:
            patt = Path(a.name)
            plugin_name = patt.stem
            load_plugins(plugin_name.replace(".py", ""))

    logger.info("Bot Started :)")

    extra_count = len(extra_clients)
    if extra_count:
        logger.info(f"Running {1 + extra_count} bots in parallel.")
    else:
        logger.info("Running 1 bot (no extra tokens configured).")

    async def _run_all():
        tasks = [bot.run_until_disconnected()]
        for tel_bot, _ in extra_clients:
            tasks.append(tel_bot.run_until_disconnected())
        await asyncio.gather(*tasks)

    bot.loop.run_until_complete(_run_all())
