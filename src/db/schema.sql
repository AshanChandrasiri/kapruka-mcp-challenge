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
    total_amount NUMERIC,
    items_total NUMERIC,
    delivery_fee NUMERIC,
    addons_total NUMERIC,
    currency TEXT,
    delivery_city TEXT,
    delivery_date DATE,
    delivery_address TEXT,
    recipient_name TEXT,
    recipient_phone TEXT,
    sender_name TEXT,
    payment_url TEXT,
    payment_url_expires_at TIMESTAMPTZ,
    kapruka_order_ref TEXT,
    status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase 3.8 migration from the Phase 3 shape (kapruka_order_id, items JSONB,
-- product_summary) — guarded/idempotent, same pattern as owner_contact ->
-- phone_number above. items/product_summary are fully superseded by the
-- order_products table below (per-product rows, not a JSONB/text blob).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orders' AND column_name = 'kapruka_order_id'
    ) THEN
        ALTER TABLE orders RENAME COLUMN kapruka_order_id TO kapruka_order_ref;
    END IF;
END $$;

ALTER TABLE orders ADD COLUMN IF NOT EXISTS items_total NUMERIC;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_fee NUMERIC;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS addons_total NUMERIC;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS currency TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_address TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS recipient_name TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS recipient_phone TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS sender_name TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_url TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_url_expires_at TIMESTAMPTZ;
ALTER TABLE orders DROP COLUMN IF EXISTS items;
ALTER TABLE orders DROP COLUMN IF EXISTS product_summary;

CREATE INDEX IF NOT EXISTS idx_orders_phone_number ON orders (phone_number);

-- Phase 3.8: per-product detail, replacing the old items JSONB blob — one
-- row per distinct product in the cart, with an explicit quantity column
-- (see src/gift_picker/tools.py::propose_cart's docstring for the cart-item
-- shape this assumes: one row per distinct product_id, not one row per unit).
CREATE TABLE IF NOT EXISTS order_products (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders(id),
    kapruka_product_id TEXT,
    product_name TEXT,
    product_url TEXT,
    product_image_url TEXT,
    unit_price NUMERIC,
    quantity INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_order_products_order_id ON order_products (order_id);

-- Phase 4: thread_id is decoupled from phone_number (src/session.py) so a
-- client can own conversation boundaries (a "New Chat" button) instead of
-- one phone number meaning exactly one conversation forever. AsyncPostgresSaver
-- only knows about thread_ids, not which customer any of them belong to —
-- without this table there's no way to answer "show this customer their
-- past chats" at all (the surface itself isn't built in this phase, just
-- made possible).
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_threads_phone_number ON threads (phone_number);
