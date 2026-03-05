Nyoping Dashboard Admin UI v4

Why you saw 429:
- v3 loaded up to 1000 members/roles every /admin refresh.

v4:
- members/roles fetched on-demand only.
- members search uses /members/search with query (min 2 chars).
- roles loaded by button.
- top10 names resolved by button (max 5 per click), cached in dashboard_kv.

Render env:
- DISCORD_BOT_TOKEN (bot token)
