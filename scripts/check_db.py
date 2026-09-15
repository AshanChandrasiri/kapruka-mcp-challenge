"""Phase 0 check: apply src/db/schema.sql to the Neon Postgres instance and
verify the recipients/orders tables are reachable.

Sync psycopg on purpose — this is a one-shot schema-apply/connectivity
check, not the async checkpointer path (that's src/session.py, built in
Phase 1 alongside PostgresSaver).

Run: .venv/Scripts/python.exe scripts/check_db.py
"""

import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from src.config import DATABASE_URL

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "src" / "db" / "schema.sql"


def _connect_kwargs() -> dict:
    """Force IPv4 for the Neon host.

    This machine's DNS returns AAAA records for the Neon pooler, but has no
    working outbound IPv6 route — a plain psycopg.connect(DATABASE_URL) hangs
    (TCP SYN silently dropped) instead of failing fast. Resolving the A
    record ourselves and passing it as `hostaddr` skips libpq's own DNS
    resolution while `host` is kept for the TLS/SCRAM channel binding Neon
    requires.
    """
    host = urlparse(DATABASE_URL).hostname
    ipv4 = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    return {"hostaddr": ipv4, "connect_timeout": 10}


def main() -> None:
    schema_sql = SCHEMA_PATH.read_text()

    with psycopg.connect(DATABASE_URL, **_connect_kwargs()) as conn:
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
