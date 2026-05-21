import os
import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import telethon
import telethon.tl.types

_env_path = Path(__file__).resolve().parent / ".env"
with open(_env_path, encoding="utf-8-sig") as _f:
    for _line in _f:
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

CHANNEL = "unclelimjourney"
START_DATE = datetime(2020, 1, 1, tzinfo=timezone.utc)
END_DATE = datetime(2026, 5, 21, tzinfo=timezone.utc)

OUTPUT_DIR = Path("output")
IMAGES_DIR = OUTPUT_DIR / "images"
MESSAGES_FILE = OUTPUT_DIR / "messages.json"


async def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    IMAGES_DIR.mkdir(exist_ok=True)

    async with telethon.TelegramClient("uncle_lim", API_ID, API_HASH) as client:
        print(f"Fetching messages from @{CHANNEL} between {START_DATE.date()} and {END_DATE.date()}...")

        messages = []
        count = 0

        async for msg in client.iter_messages(
            CHANNEL,
            offset_date=END_DATE,
            reverse=False,
        ):
            if msg.date < START_DATE:
                break

            image_path = None

            if msg.media and isinstance(msg.media, telethon.tl.types.MessageMediaPhoto):
                filename = IMAGES_DIR / f"{msg.id}.jpg"
                if not filename.exists():
                    await client.download_media(msg, file=str(filename))
                image_path = str(filename)

            messages.append({
                "id": msg.id,
                "date": msg.date.isoformat(),
                "text": msg.text or "",
                "image_path": image_path,
            })

            count += 1
            if count % 100 == 0:
                print(f"  {count} messages processed...")

        # reverse so output is chronological
        messages.sort(key=lambda m: m["date"])

        with open(MESSAGES_FILE, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)

        print(f"\nDone. {len(messages)} messages saved to {MESSAGES_FILE}")
        print(f"Images saved to {IMAGES_DIR}/")


if __name__ == "__main__":
    asyncio.run(main())
