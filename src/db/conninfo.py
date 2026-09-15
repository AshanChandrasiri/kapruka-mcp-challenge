"""Shared IPv4-forcing helper for connecting to the Neon Postgres instance.

This machine's DNS returns AAAA records for the Neon pooler, but has no
working outbound IPv6 route — a plain connect on the hostname hangs (TCP SYN
silently dropped) instead of failing fast. Resolving the A record ourselves
and passing it as `hostaddr` skips libpq's own DNS resolution while `host` is
kept for the TLS/SCRAM channel binding Neon requires.
"""

import socket
from urllib.parse import urlparse

from psycopg.conninfo import make_conninfo


def ipv4_conninfo(database_url: str) -> str:
    host = urlparse(database_url).hostname
    ipv4 = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    return make_conninfo(database_url, hostaddr=ipv4)
