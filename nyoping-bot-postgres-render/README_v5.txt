Nyoping Dashboard Admin UI v5 (Rate-limit hardened)

Apply (Root Directory 기준):
- dashboard/main.py
- dashboard/templates/admin.html
- dashboard/templates/index.html

What changes:
- Stores cooldown when 429 occurs and stops calling Discord until cooldown expires.
- roles cached 10min, member search cached 60sec.
- UI shows remaining seconds.

If 429 happens immediately for a long time:
- Render shared IP may be blocked.
- Best fix: host dashboard on a different provider/IP or paid plan/dedicated egress.
