"""Environment/config loading, shared across the project."""

import os

from dotenv import load_dotenv

load_dotenv()

KAPRUKA_MCP_URL = os.environ["KAPRUKA_MCP_URL"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.6-flash")

# Phase 5.1: "development" (default) keeps console-only tracing exactly as
# every prior Phase 5.0.x sub-phase built it; "production" is the opt-in
# that routes the same spans to Langfuse Cloud over OTLP instead. Read
# with .get(), not a hard os.environ[...] lookup, since the concierge
# itself must never fail to start over a missing/misconfigured
# observability backend — a broken exporter should drop its own exports
# quietly, not take the app down.
APP_ENV = os.environ.get("APP_ENV", "development")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY")
LANGFUSE_BASE_URL = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
