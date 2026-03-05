# -*- coding: utf-8 -*-
"""
Nyoping Dashboard Admin UI v5 (Rate-limit hardened)

- Persist per-endpoint cooldown in Postgres when Discord returns 429.
- While cooldown active, do NOT call Discord again (prevents endless extension).
- Cache roles (10min) and member search results (60s) in DB.

Required env:
- DATABASE_URL
- DASHBOARD_BASE_URL
- DASHBOARD_SESSION_SECRET
- DASHBOARD_ADMIN_PASSWORD
- DISCORD_BOT_TOKEN
"""
from __future__ import annotations

import os
import time
import secrets
import re
import json
from typing import Optional, Dict, Any, List

import asyncpg
import requests
from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates
from pathlib import Path

BASE_URL = os.getenv('DASHBOARD_BASE_URL','').rstrip('/')
SESSION_SECRET = os.getenv('DASHBOARD_SESSION_SECRET','') or secrets.token_urlsafe(32)
ADMIN_PASSWORD = os.getenv('DASHBOARD_ADMIN_PASSWORD','')
DATABASE_URL = os.getenv('DATABASE_URL','')
BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN','')
DISABLE_DISCORD_OAUTH = os.getenv('DISABLE_DISCORD_OAUTH','1') == '1'
DISCORD_API_BASE = 'https://discord.com/api/v10'

ROLES_CACHE_TTL = 600
MEMBER_SEARCH_CACHE_TTL = 60
RL_MAX_SECONDS = 3600

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=BASE_URL.startswith('https://'))
templates = Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))

@app.on_event('startup')
async def _startup():
    if not DATABASE_URL:
        app.state.pg_pool = None
        return
    app.state.pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3, command_timeout=20)
    async with app.state.pg_pool.acquire() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS guild_settings(
            guild_id BIGINT PRIMARY KEY,
            checkin_xp INT NOT NULL DEFAULT 20,
            message_xp INT NOT NULL DEFAULT 2,
            message_cooldown_sec INT NOT NULL DEFAULT 60,
            voice_xp_per_min INT NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS level_role_rules(
            guild_id BIGINT NOT NULL,
            level INT NOT NULL,
            add_role_id BIGINT NOT NULL,
            remove_role_id BIGINT,
            PRIMARY KEY (guild_id, level)
        );''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS user_xp(
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            xp BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (guild_id, user_id)
        );''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS checkins(
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            ymd TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, ymd)
        );''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS dashboard_kv(
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );''')

@app.on_event('shutdown')
async def _shutdown():
    pool = getattr(app.state,'pg_pool',None)
    if pool:
        await pool.close()

def _render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(name, {'request': request, **ctx})

def _is_admin(request: Request) -> bool:
    return bool(request.session.get('admin'))

def _bot_headers() -> Dict[str,str]:
    return {'Authorization': f'Bot {BOT_TOKEN}'}

def _parse_id(value: str) -> Optional[int]:
    if not value:
        return None
    m = re.search(r'(\d{10,25})', value)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None

async def _kv_get(key: str) -> Optional[str]:
    pool = app.state.pg_pool
    if pool is None:
        return None
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT v FROM dashboard_kv WHERE k=$1', key)

async def _kv_set(key: str, value: str) -> None:
    pool = app.state.pg_pool
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO dashboard_kv(k,v,updated_at) VALUES ($1,$2,NOW()) '
            'ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, updated_at=NOW()',
            key, value
        )

async def _rl_remaining(key: str) -> int:
    raw = await _kv_get(f'rl:{key}:until')
    if not raw:
        return 0
    try:
        until = float(raw)
    except Exception:
        return 0
    return max(0, int(until - time.time()))

async def _rl_set(key: str, seconds: float) -> None:
    seconds = float(seconds)
    seconds = min(max(seconds, 5.0), RL_MAX_SECONDS)
    until = time.time() + seconds + 1.0

    cur = await _kv_get(f'rl:{key}:until')
    try:
        cur_until = float(cur) if cur else 0.0
    except Exception:
        cur_until = 0.0

    if until > cur_until:
        await _kv_set(f'rl:{key}:until', str(until))

def _retry_after_seconds(resp: requests.Response) -> float:
    ra = 0.0
    try:
        j = resp.json()
        ra = float(j.get('retry_after', 0.0) or 0.0)
    except Exception:
        pass
    if ra <= 0:
        try:
            ra = float(resp.headers.get('Retry-After', '0') or '0')
        except Exception:
            ra = 0.0
    if ra <= 0:
        ra = 60.0
    return ra

def _member_label(m: Dict[str,Any]) -> str:
    user = m.get('user') or {}
    uid = user.get('id','')
    username = user.get('username','')
    disc = user.get('discriminator','')
    nick = m.get('nick') or ''
    gname = user.get('global_name') or ''
    if nick:
        return f"{nick} ({username}#{disc}) ({uid})"
    if gname:
        return f"{gname} ({username}#{disc}) ({uid})"
    return f"{username}#{disc} ({uid})"

def _role_label(r: Dict[str,Any]) -> str:
    return f"{r.get('name','')} ({r.get('id','')})"

# --- DB domain helpers ---
async def _db_fetch_settings(gid:int) -> Dict[str,Any]:
    pool = app.state.pg_pool
    if pool is None:
        return {'checkin_xp':20,'message_xp':2,'message_cooldown_sec':60,'voice_xp_per_min':1}
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT checkin_xp,message_xp,message_cooldown_sec,voice_xp_per_min FROM guild_settings WHERE guild_id=$1',
            gid
        )
        if not row:
            await conn.execute('INSERT INTO guild_settings(guild_id) VALUES ($1) ON CONFLICT DO NOTHING', gid)
            return {'checkin_xp':20,'message_xp':2,'message_cooldown_sec':60,'voice_xp_per_min':1}
        return dict(row)

async def _db_save_settings(gid:int, cx:int, mx:int, cd:int, vx:int) -> None:
    pool = app.state.pg_pool
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            '''INSERT INTO guild_settings(guild_id,checkin_xp,message_xp,message_cooldown_sec,voice_xp_per_min,updated_at)
               VALUES ($1,$2,$3,$4,$5,NOW())
               ON CONFLICT (guild_id) DO UPDATE SET
                 checkin_xp=EXCLUDED.checkin_xp,
                 message_xp=EXCLUDED.message_xp,
                 message_cooldown_sec=EXCLUDED.message_cooldown_sec,
                 voice_xp_per_min=EXCLUDED.voice_xp_per_min,
                 updated_at=NOW()''',
            gid, cx, mx, cd, vx
        )

async def _db_fetch_rules(gid:int) -> List[Dict[str,Any]]:
    pool = app.state.pg_pool
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT level,add_role_id,remove_role_id FROM level_role_rules WHERE guild_id=$1 ORDER BY level ASC',
            gid
        )
        return [dict(r) for r in rows]

async def _db_upsert_rule(gid:int, level:int, add_id:int, rem_id:Optional[int]) -> None:
    pool = app.state.pg_pool
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            '''INSERT INTO level_role_rules(guild_id,level,add_role_id,remove_role_id)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (guild_id,level) DO UPDATE SET
                 add_role_id=EXCLUDED.add_role_id,
                 remove_role_id=EXCLUDED.remove_role_id''',
            gid, level, add_id, rem_id
        )

async def _db_delete_rule(gid:int, level:int) -> None:
    pool = app.state.pg_pool
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM level_role_rules WHERE guild_id=$1 AND level=$2', gid, level)

async def _db_top10(gid:int) -> List[Dict[str,Any]]:
    pool = app.state.pg_pool
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT user_id,xp FROM user_xp WHERE guild_id=$1 ORDER BY xp DESC LIMIT 10',
            gid
        )
        out = []
        for r in rows:
            xp = int(r['xp'])
            out.append({'user_id':int(r['user_id']), 'xp':xp, 'level':xp//100})
        return out

async def _db_reset_checkin(gid:int, uid:int, ymd:str) -> int:
    pool = app.state.pg_pool
    if pool is None:
        return 0
    async with pool.acquire() as conn:
        res = await conn.execute('DELETE FROM checkins WHERE guild_id=$1 AND user_id=$2 AND ymd=$3', gid, uid, ymd)
        return int(res.split()[-1])

async def _db_set_level(gid:int, uid:int, level:int) -> None:
    pool = app.state.pg_pool
    if pool is None:
        return
    xp = max(0,int(level))*100
    async with pool.acquire() as conn:
        await conn.execute(
            '''INSERT INTO user_xp(guild_id,user_id,xp,updated_at)
               VALUES ($1,$2,$3,NOW())
               ON CONFLICT (guild_id,user_id) DO UPDATE SET xp=EXCLUDED.xp, updated_at=NOW()''',
            gid, uid, xp
        )

# --- Pages ---
@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    return _render(request,'index.html', base_url=BASE_URL, admin_enabled=bool(ADMIN_PASSWORD), disable_discord_oauth=DISABLE_DISCORD_OAUTH)

@app.post('/admin-login')
async def admin_login(request: Request, password: str = Form(...)):
    if not ADMIN_PASSWORD or password != ADMIN_PASSWORD:
        return _render(request,'index.html', base_url=BASE_URL, admin_enabled=bool(ADMIN_PASSWORD),
                       disable_discord_oauth=DISABLE_DISCORD_OAUTH, admin_error='비밀번호가 올바르지 않습니다.')
    request.session['admin'] = True
    return RedirectResponse(url='/admin', status_code=302)

@app.get('/admin', response_class=HTMLResponse)
async def admin(request: Request, guild_id: Optional[int]=None, msg: str=''):
    if not _is_admin(request):
        return RedirectResponse(url='/', status_code=302)
    ctx = {'settings':None, 'rules':[], 'top10':[], 'error':'', 'msg':msg}
    if guild_id:
        ctx['settings'] = await _db_fetch_settings(guild_id)
        ctx['rules'] = await _db_fetch_rules(guild_id)
        ctx['top10'] = await _db_top10(guild_id)
        for row in ctx['top10']:
            row['name'] = (await _kv_get(f"name:{guild_id}:{row['user_id']}") ) or '-'
        if not BOT_TOKEN:
            ctx['error'] = 'Render 환경변수 DISCORD_BOT_TOKEN이 비어 있습니다. (유저/역할 미리보기 불가)'
    return _render(request,'admin.html', base_url=BASE_URL, guild_id=guild_id or '', **ctx)

# --- Actions ---
@app.post('/admin/save-settings')
async def save_settings(request: Request, guild_id: str=Form(...), checkin_xp:int=Form(...), message_xp:int=Form(...), message_cooldown_sec:int=Form(...), voice_xp_per_min:int=Form(...)):
    if not _is_admin(request):
        return RedirectResponse(url='/', status_code=302)
    gid = _parse_id(guild_id)
    if not gid:
        return RedirectResponse(url='/admin?msg=Guild+ID가+올바르지+않습니다', status_code=302)
    await _db_save_settings(gid, checkin_xp, message_xp, message_cooldown_sec, voice_xp_per_min)
    return RedirectResponse(url=f'/admin?guild_id={gid}&msg=저장+완료', status_code=302)

@app.post('/admin/quick-checkin-reset')
async def quick_checkin_reset(request: Request, guild_id:str=Form(...), user_pick:str=Form(...), ymd:str=Form(...)):
    if not _is_admin(request):
        return RedirectResponse(url='/', status_code=302)
    gid=_parse_id(guild_id); uid=_parse_id(user_pick)
    if not gid or not uid:
        return RedirectResponse(url=f'/admin?guild_id={gid or ""}&msg=유저/서버+ID가+올바르지+않습니다', status_code=302)
    deleted = await _db_reset_checkin(gid, uid, ymd)
    return RedirectResponse(url=f'/admin?guild_id={gid}&msg=출석기록+삭제:{deleted}', status_code=302)

@app.post('/admin/quick-set-level')
async def quick_set_level(request: Request, guild_id:str=Form(...), user_pick:str=Form(...), level:int=Form(...)):
    if not _is_admin(request):
        return RedirectResponse(url='/', status_code=302)
    gid=_parse_id(guild_id); uid=_parse_id(user_pick)
    if not gid or not uid:
        return RedirectResponse(url=f'/admin?guild_id={gid or ""}&msg=유저/서버+ID가+올바르지+않습니다', status_code=302)
    await _db_set_level(gid, uid, level)
    return RedirectResponse(url=f'/admin?guild_id={gid}&msg=레벨+적용+완료', status_code=302)

@app.post('/admin/rules-upsert')
async def rules_upsert(request: Request, guild_id:str=Form(...), level:int=Form(...), add_role_pick:str=Form(...), remove_role_pick:str=Form('')):
    if not _is_admin(request):
        return RedirectResponse(url='/', status_code=302)
    gid=_parse_id(guild_id); add_id=_parse_id(add_role_pick); rem_id=_parse_id(remove_role_pick) if remove_role_pick else None
    if not gid or not add_id:
        return RedirectResponse(url=f'/admin?guild_id={gid or ""}&msg=역할/서버+ID가+올바르지+않습니다', status_code=302)
    await _db_upsert_rule(gid, int(level), add_id, rem_id)
    return RedirectResponse(url=f'/admin?guild_id={gid}&msg=규칙+저장+완료', status_code=302)

@app.post('/admin/rules-delete')
async def rules_delete(request: Request, guild_id:str=Form(...), level:int=Form(...)):
    if not _is_admin(request):
        return RedirectResponse(url='/', status_code=302)
    gid=_parse_id(guild_id)
    if not gid:
        return RedirectResponse(url='/admin?msg=Guild+ID가+올바르지+않습니다', status_code=302)
    await _db_delete_rule(gid, int(level))
    return RedirectResponse(url=f'/admin?guild_id={gid}&msg=규칙+삭제+완료', status_code=302)

# --- Admin APIs (rate-limit hardened) ---
@app.get('/admin/api/roles')
async def api_roles(request: Request, guild_id:int=Query(...)):
    if not _is_admin(request):
        return JSONResponse({'error':'unauthorized'}, status_code=401)
    if not BOT_TOKEN:
        return JSONResponse({'error':'DISCORD_BOT_TOKEN missing'}, status_code=400)

    rem = await _rl_remaining('roles')
    cached = await _kv_get(f'cache:roles:{guild_id}')
    cached_ts_raw = await _kv_get(f'cache:roles:{guild_id}:ts')
    cached_ts = float(cached_ts_raw) if cached_ts_raw else 0.0
    cache_fresh = (time.time() - cached_ts) < ROLES_CACHE_TTL

    if rem > 0:
        if cached:
            return JSONResponse(json.loads(cached))
        return JSONResponse({'error':'rate_limited_roles', 'remaining': rem}, status_code=429)

    if cache_fresh and cached:
        return JSONResponse(json.loads(cached))

    try:
        r = requests.get(f'{DISCORD_API_BASE}/guilds/{guild_id}/roles', headers=_bot_headers(), timeout=15)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)

    if r.status_code == 429:
        ra = _retry_after_seconds(r)
        await _rl_set('roles', ra)
        rem = await _rl_remaining('roles')
        if cached:
            return JSONResponse(json.loads(cached))
        return JSONResponse({'error':'rate_limited_roles', 'remaining': rem}, status_code=429)

    if not r.ok:
        return JSONResponse({'error': f'http_{r.status_code}', 'body': r.text[:200]}, status_code=400)

    roles = r.json()
    roles.sort(key=lambda x: x.get('position',0), reverse=True)
    payload = [{'id': int(x['id']), 'label': _role_label(x)} for x in roles if x.get('id')]
    await _kv_set(f'cache:roles:{guild_id}', json.dumps(payload, ensure_ascii=False))
    await _kv_set(f'cache:roles:{guild_id}:ts', str(time.time()))
    return JSONResponse(payload)

@app.get('/admin/api/members_search')
async def api_members_search(request: Request, guild_id:int=Query(...), q:str=Query(...)):
    if not _is_admin(request):
        return JSONResponse({'error':'unauthorized'}, status_code=401)
    if not BOT_TOKEN:
        return JSONResponse({'error':'DISCORD_BOT_TOKEN missing'}, status_code=400)

    q = (q or '').strip()
    if len(q) < 2:
        return JSONResponse([], status_code=200)

    rl_key = f'members_search:{guild_id}'
    rem = await _rl_remaining(rl_key)

    cache_key = f'cache:members_search:{guild_id}:{q.lower()}'
    cached = await _kv_get(cache_key)
    cached_ts_raw = await _kv_get(cache_key + ':ts')
    cached_ts = float(cached_ts_raw) if cached_ts_raw else 0.0
    cache_fresh = (time.time() - cached_ts) < MEMBER_SEARCH_CACHE_TTL

    if rem > 0:
        if cached:
            return JSONResponse(json.loads(cached))
        return JSONResponse({'error':'rate_limited_members', 'remaining': rem}, status_code=429)

    if cache_fresh and cached:
        return JSONResponse(json.loads(cached))

    try:
        r = requests.get(
            f'{DISCORD_API_BASE}/guilds/{guild_id}/members/search',
            headers=_bot_headers(),
            params={'query': q, 'limit': 25},
            timeout=20
        )
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)

    if r.status_code == 429:
        ra = _retry_after_seconds(r)
        await _rl_set(rl_key, ra)
        rem = await _rl_remaining(rl_key)
        if cached:
            return JSONResponse(json.loads(cached))
        return JSONResponse({'error':'rate_limited_members', 'remaining': rem}, status_code=429)

    if not r.ok:
        return JSONResponse({'error': f'http_{r.status_code}', 'body': r.text[:200]}, status_code=400)

    members = r.json()
    payload = [{'id': int(m['user']['id']), 'label': _member_label(m)} for m in members if m.get('user') and m['user'].get('id')]
    await _kv_set(cache_key, json.dumps(payload, ensure_ascii=False))
    await _kv_set(cache_key + ':ts', str(time.time()))
    return JSONResponse(payload)

@app.post('/admin/api/resolve_top10')
async def resolve_top10(request: Request, guild_id:int=Form(...)):
    if not _is_admin(request):
        return JSONResponse({'error':'unauthorized'}, status_code=401)
    if not BOT_TOKEN:
        return JSONResponse({'error':'DISCORD_BOT_TOKEN missing'}, status_code=400)

    gid = int(guild_id)
    rem = await _rl_remaining(f'member_get:{gid}')
    if rem > 0:
        return RedirectResponse(url=f'/admin?guild_id={gid}&msg=레이트리밋:+{rem}초', status_code=302)

    top = await _db_top10(gid)
    resolved = 0
    for row in top:
        uid = row['user_id']
        key = f'name:{gid}:{uid}'
        if await _kv_get(key):
            continue
        try:
            r = requests.get(f'{DISCORD_API_BASE}/guilds/{gid}/members/{uid}', headers=_bot_headers(), timeout=15)
            if r.status_code == 429:
                ra = _retry_after_seconds(r)
                await _rl_set(f'member_get:{gid}', ra)
                break
            if r.ok:
                await _kv_set(key, _member_label(r.json()))
                resolved += 1
        except Exception:
            pass
        if resolved >= 3:
            break
    return RedirectResponse(url=f'/admin?guild_id={gid}&msg=닉네임+갱신:{resolved}', status_code=302)
