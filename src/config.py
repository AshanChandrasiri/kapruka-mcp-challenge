"""Environment/config loading, shared across the project."""

import os

from dotenv import load_dotenv

load_dotenv()

KAPRUKA_MCP_URL = os.environ["KAPRUKA_MCP_URL"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.6-flash")
