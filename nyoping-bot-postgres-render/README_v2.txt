# Nyoping Dashboard Admin UI v2

Changes:
- Quick actions user selector (members list) via Discord bot token
- Role selector (roles list) via Discord bot token
- Prevent 422 JSON pages by parsing IDs from text inputs and redirecting with friendly errors
- Top 10 XP shows nickname + Discord username when available

Render Environment (add):
- DISCORD_BOT_TOKEN = (same bot token as DISCORD_TOKEN)
Optional:
- DISABLE_DISCORD_OAUTH=1 (keep, if you want to avoid OAuth rate-limit)
