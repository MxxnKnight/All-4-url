"""Generate a SESSION_STRING for the restricted downloader.

Run this ONCE, on your own computer (not on the host):

    pip install wzgram python-dotenv
    python gen_session.py

It asks for your API_ID / API_HASH (from https://my.telegram.org/apps),
your phone number, the login code Telegram sends you, and your 2FA
password if you have one. It then prints one long string.

⚠️  That string is a login to your Telegram account — a password
equivalent. Paste it ONLY into your host's SESSION_STRING environment
variable. Never commit it, never put it in a file inside this repo,
never send it to anyone.
"""

import asyncio

from pyrogram import Client


async def main() -> None:
    print(__doc__)
    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()
    app = Client(
        name="all4url_session_gen",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
    )
    await app.start()
    session = await app.export_session_string()
    await app.stop()
    print("\n================ SESSION_STRING ================")
    print(session)
    print("================================================\n")
    print("Store it as the SESSION_STRING env var on your host, "
          "then delete it from anywhere else you pasted it.")


if __name__ == "__main__":
    asyncio.run(main())
