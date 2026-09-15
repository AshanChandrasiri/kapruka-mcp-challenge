"""Phase 0 check: apply src/db/schema.sql to the Neon Postgres instance and
verify the recipients/orders tables are reachable.

Sync psycopg on purpose — this is a one-shot schema-apply/connectivity
check, not the async checkpointer path (that's src/session.py, built in
Phase 1 alongside PostgresSaver).

Run: .venv/Scripts/python.exe scripts/check_db.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from src.config import DATABASE_URL
from src.db.conninfo import ipv4_conninfo

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "src" / "db" / "schema.sql"


def main() -> None:
    schema_sql = SCHEMA_PATH.read_text()

    with psycopg.connect(ipv4_conninfo(DATABASE_URL), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        conn.commit()
        print("Schema applied (recipients, orders).")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN ('recipients', 'orders') "
                "ORDER BY table_name"
            )
            rows = [r[0] for r in cur.fetchall()]
            print("Tables present:", rows)

            cur.execute("SELECT count(*) FROM recipients")
            print("recipients row count:", cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM orders")
            print("orders row count:", cur.fetchone()[0])


if __name__ == "__main__":
    main()
