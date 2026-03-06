import asyncio
import os
import re
import sys
import asyncpg

CANDIDATES = [
    # level role rules
    (("guild_id", "level"), "uq_{table}_guild_level"),
    # reaction block
    (("message_id", "blocked_role_id"), "uq_{table}_message_blocked_role"),
    (("guild_id", "message_id", "blocked_role_id"), "uq_{table}_guild_message_blocked_role"),
    (("guild_id", "channel_id", "message_id", "blocked_role_id"), "uq_{table}_guild_channel_message_blocked_role"),
    (("channel_id", "message_id", "blocked_role_id"), "uq_{table}_channel_message_blocked_role"),
    # reaction role
    (("message_id", "emoji"), "uq_{table}_message_emoji"),
    (("guild_id", "message_id", "emoji"), "uq_{table}_guild_message_emoji"),
    (("guild_id", "channel_id", "message_id", "emoji"), "uq_{table}_guild_channel_message_emoji"),
    (("channel_id", "message_id", "emoji"), "uq_{table}_channel_message_emoji"),
]

INDEXES = [
    (("guild_id",), "ix_{table}_guild_id"),
    (("message_id",), "ix_{table}_message_id"),
    (("blocked_role_id",), "ix_{table}_blocked_role_id"),
    (("emoji",), "ix_{table}_emoji"),
    (("level",), "ix_{table}_level"),
]

def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'

def slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", name)
    return s.strip("_").lower() or "idx"

async def main():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL 환경변수가 없습니다.")
        sys.exit(1)

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        tables = [r["table_name"] for r in rows]
        print(f"public 스키마 테이블 수: {len(tables)}")
        touched = 0

        for table in tables:
            col_rows = await conn.fetch("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = $1
            """, table)
            cols = {r["column_name"] for r in col_rows}

            local_changes = []
            for cand_cols, name_tpl in CANDIDATES:
                if all(c in cols for c in cand_cols):
                    idx_name = slug(name_tpl.format(table=table))
                    cols_sql = ", ".join(qident(c) for c in cand_cols)
                    sql = f'CREATE UNIQUE INDEX IF NOT EXISTS {qident(idx_name)} ON {qident(table)} ({cols_sql})'
                    await conn.execute(sql)
                    local_changes.append(("UNIQUE", idx_name, cand_cols))

            for cand_cols, name_tpl in INDEXES:
                if all(c in cols for c in cand_cols):
                    idx_name = slug(name_tpl.format(table=table))
                    cols_sql = ", ".join(qident(c) for c in cand_cols)
                    sql = f'CREATE INDEX IF NOT EXISTS {qident(idx_name)} ON {qident(table)} ({cols_sql})'
                    await conn.execute(sql)
                    local_changes.append(("INDEX", idx_name, cand_cols))

            if local_changes:
                touched += 1
                print(f"\n[{table}]")
                for kind, idx_name, cand_cols in local_changes:
                    print(f"  - {kind}: {idx_name} -> {', '.join(cand_cols)}")

        print(f"\n완료: 인덱스/제약 후보를 적용한 테이블 {touched}개")
        print("이제 Render 대시보드에서 반응 차단 저장을 다시 테스트하세요.")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
