# Telegram Dating & Random Chat Bot

Users create a profile (name, age, gender, city, bio, photo) and then can:

- 💬 **Random Chat** – anonymous 1-to-1 chat with a compatible stranger (gender preference respected). `⏭ Next` / `⏹ Stop`. All media types are relayed.
- ❤️ **Browse Profiles** – swipe-style Like/Skip. A mutual like = **match**, both get each other's Telegram contact.
- ❤️ After a random chat you can **Like** the partner too – if they like you back, it's a match and identities are revealed.
- 🚩 Report / 🚫 Block (5 reports = auto-ban). Admin: `/ban <id>`, `/unban <id>`, `/stats`.

18+ only (age gate at signup).

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # put BOT_TOKEN from @BotFather, your ADMIN_IDS
python -m bot.main
```
Uses long polling + SQLite (`bot.db`), no extra infra needed. For production run under systemd/Docker.
