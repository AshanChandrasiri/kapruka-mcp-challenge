-- Kapruka Gift Concierge — hand-rolled state tables.
-- LangGraph's PostgresSaver owns its own checkpoint tables in this same
-- Neon database; this file only covers our own recipients/orders tables.
--
-- This Neon instance is reused from the pre-LangGraph (Google ADK) build of
-- this project — its own session tables (adk_internal_metadata, app_states,
-- events, sessions, user_states) still live here and are left untouched.
-- `recipients`/`orders` predate this file too; it was originally
-- `owner_contact`, renamed once (see migration below) to match this
-- project's phone_number == user_id == session_id convention (CLAUDE.md).

CREATE TABLE IF NOT EXISTS recipients (
    id SERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL,
    name TEXT NOT NULL,
    relationship TEXT,
    preferences TEXT,
    budget_min NUMERIC,
    budget_max NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One-time migration from the ADK-era column name (safe: table was empty).
-- Guarded so this file stays idempotent/re-runnable after the rename lands.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'recipients' AND column_name = 'owner_contact'
    ) THEN
        ALTER TABLE recipients RENAME COLUMN owner_contact TO phone_number;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_recipients_phone_number ON recipients (phone_number);

CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL,
    items JSONB,
    product_summary TEXT,
    total_amount NUMERIC,
    delivery_city TEXT,
    delivery_date DATE,
    kapruka_order_id TEXT,
    status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_phone_number ON orders (phone_number);
