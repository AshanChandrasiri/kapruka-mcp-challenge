# Build Plan — Kapruka Gift Concierge (v1)

**Status: v2 rebuild in progress — Phase 0 and Phase 1 complete (this
directory started empty: no repo, no venv, no source — only
`.env`/`CLAUDE.md`/`PLAN.md`/`docs/` carried over). Everything from Phase 2
onward in this file is still the narrative from the earlier build, kept as
design-decision history — that code does not exist yet in `v2` and needs to
be rebuilt phase by phase.**

This is a starting skeleton, not a spec. Flesh out each phase's detail when
you get to it — the point of this project is working through the decisions
yourself with Claude Code, not executing a plan written in advance.

## Phase 0 — Scaffolding (redone for v2, 2026-09-15)
- [x] Repo/venv setup — `git init` + `.venv`, installed `langgraph`, `langchain`,
      `langchain-mcp-adapters`, `langchain-google-genai`, `langgraph-checkpoint-postgres`,
      `fastapi`, `uvicorn`, `python-dotenv`, `psycopg[binary]`, `gradio`, `mcp`.
      Pinned via `requirements.txt`. `.gitignore` added (`.venv/`, `.env`, `.idea/`, etc).
- [x] Gemini API key confirmed working — `scripts/check_llm.py` builds a
      `langchain.agents.create_agent` LangGraph agent (the current, non-deprecated
      API; `langgraph.prebuilt.create_react_agent` is deprecated as of
      LangGraph 1.x) with a real tool, verified a live tool-calling round trip.
      **Model name correction:** `gemini-2.5-flash` (the model named in this
      plan/CLAUDE.md) 404s now — "no longer available to new users." Live API
      error names the replacement: `gemini-3.6-flash`. Set as the new default
      in `src/config.py::LLM_MODEL` (still overridable via env var). Revisit
      if it drifts again.
- [x] Connect `MultiServerMCPClient` from `langchain-mcp-adapters` (`transport: "streamable_http"`) to
      `https://mcp.kapruka.com/mcp` — 8 tools load (one more than documented,
      see note below). Verified via `scripts/check_mcp.py`.
- [x] Postgres: `recipients`/`orders` tables — `src/db/schema.sql`, applied to
      the live Neon instance, verified via `scripts/check_db.py`.
      **Same Neon instance as the earlier build** — its Google ADK-era
      session tables (`adk_internal_metadata`, `app_states`, `events`,
      `sessions`, `user_states`) are still there and were left untouched
      (confirms the "originally scaffolded on ADK, switched mid-Phase-0" note
      in `CLAUDE.md`). `recipients`/`orders` themselves already existed too,
      both with 0 rows — `recipients.owner_contact` was renamed to
      `phone_number` (guarded/idempotent in `schema.sql`) to match this
      project's `phone_number == user_id == session_id` convention; `orders`
      already had the Phase-3 columns from the earlier build.
      **Environment quirk, load-bearing for later phases:** this machine
      resolves the Neon pooler hostname to an IPv6 address it cannot
      actually route to (silent connection hang, not a fast failure) —
      IPv4 works. `scripts/check_db.py` resolves the A record itself and
      connects via `hostaddr=<ipv4>` (keeping `host=` for TLS/SCRAM channel
      binding). **`src/session.py`'s async `PostgresSaver` setup in Phase 1
      will need the same treatment** (psycopg3 async connection kwargs, not
      just the sync check script) or it will hang the same way.

**Note:** live server exposes 8 tools, not 7 — `kapruka_render_options_card`
(renders 1-4 products as a shareable JPEG "menu" card) isn't in
`docs/mcp/kapruka-mcp-tools.md` yet. Doc needs updating; not otherwise
blocking.

## Phase 1 — Intent Router (rebuilt and tested for v2, 2026-09-15)
Rebuilt from scratch — none of this existed yet in `v2` (see Phase 0 note
above): `src/prompts.py`, `src/session.py`, `src/router/intent_router.py`,
`src/orchestrator.py`, `src/pipeline.py`, `main.py`,
`scripts/test_intent_router.py`.

- [x] Single structured-output LLM call, 5-way classification:
      `gift_request` | `track_order` | `return_item` | `chitchat` | `out_of_scope`
      — `src/router/intent_router.py::IntentClassification` (pydantic,
      `Literal` intent field), prompt in `src/prompts.py`. Built via
      `langchain.agents.create_agent(llm, tools=[], response_format=...)` —
      the current, non-deprecated structured-output API
      (`langgraph.prebuilt.create_react_agent` is deprecated as of
      LangGraph 1.x). No tools attached means no loop, so this is genuinely
      single-shot even though it's a real agent node under the hood.
- [x] **Session identity** — `src/session.py::session_identity(phone_number)`
      returns a `RunnableConfig` with `thread_id = user_id = session_id =
      phone_number`, used by every component that touches the shared session.
- [x] **`PostgresSaver` checkpointer**, pointed at `DATABASE_URL` (Neon), as a
      cached async singleton (`src/session.py::get_checkpointer`, using
      `AsyncPostgresSaver` from `langgraph-checkpoint-postgres`). Verified
      directly against the real Neon instance (`aget_tuple` round-trip) before
      wiring it into the orchestrator. Needed the same IPv4-forcing workaround
      as `scripts/check_db.py` (Phase 0) — `_ipv4_conninfo()` builds the
      conninfo via `psycopg.conninfo.make_conninfo(DATABASE_URL,
      hostaddr=<resolved-A-record>)` before handing it to
      `AsyncPostgresSaver.from_conn_string()`, which connects with a raw
      `psycopg.AsyncConnection` — no SQLAlchemy or URL-scheme rewrite
      involved at this layer. Windows-only: `WindowsSelectorEventLoopPolicy`
      set at import time in `src/session.py`, since psycopg3 async refuses
      the default `ProactorEventLoop`.
- [x] **Bound the router's history to the last ~4 messages — standalone path
      only.** LangGraph's own message-history handling is binary (full
      history via the checkpointer, or none) — there's no partial-N config
      lever. `classify_intent()` (`src/router/intent_router.py`) gets a
      bounded slice by reading the last checkpoint via
      `checkpointer.aget_tuple()`, slicing `channel_values["messages"][-4:]`
      itself, and invoking a fresh (uncheckpointed) router agent instance
      with that slice + the new message — a read of the shared session, not
      a turn against it, so it never persists back.
      **Production path (via the orchestrator) sees full history instead** —
      no bound at the top-level graph, since Phase 2's Gift-Picker will need
      full history as working memory and there's no way to know in advance
      (before the router classifies the message) whether a turn will even
      reach it. Verified live: `classify_intent()` correctly used two prior
      turns of session history to resolve "check on the thing I sent last
      week" as `track_order`, not a new `gift_request`.
- [x] **Orchestrator as a custom `StateGraph`**
      (`src/orchestrator.py::build_orchestrator` / `ConciergeState`) — the
      Intent Router runs as a real compiled subgraph added directly as a
      node (`graph.add_node("intent_router", build_router_agent())`),
      sharing the parent's own `messages` channel, so the inbound message is
      appended exactly once, not twice. `_extract_intent` reads the router's
      `structured_response.intent` into a plain `intent` field; a
      conditional edge (`_route_on_intent`) dispatches on that string to one
      of five terminal nodes.
      - Not a fixed sequential chain: every downstream node would otherwise
        run unconditionally regardless of classification.
      - Not "routing via an agent's own reasoning" either (the
        supervisor/conditional-edge-by-LLM pattern) — the routing decision
        is a plain dict lookup on a string the router already returned, not
        a second model call deciding where to go.
      - **Found and fixed live:** embedding the router this way means its
        `structured_response` (a custom pydantic type) gets checkpointed
        like any other state key — LangGraph warned about persisting an
        unregistered type via msgpack ("will be blocked in a future
        version"). Fixed by annotating `structured_response` as
        `EphemeralValue` (`langgraph.channels.ephemeral_value`) in
        `ConciergeState` — it's only needed transiently within the turn to
        feed `_extract_intent`, so it never needed persisting.
      `src/pipeline.py::run_turn(phone_number, message)` is the shared
      "run one turn" call — builds/caches the compiled orchestrator once,
      invokes it with `session_identity(phone_number)` as config, returns
      the reply text. `main.py` (console) is the first entry point to use it;
      the FastAPI webhook and Gradio dev UI will reuse it unchanged later.
- [x] `chitchat` → canned/templated capability response, fully wired
      (`CHITCHAT_RESPONSE` in `src/prompts.py`).
- [x] `out_of_scope` → canned decline + redirect, fully wired
      (`OUT_OF_SCOPE_RESPONSE` in `src/prompts.py`).
- [x] `gift_request` → stub only (log line + placeholder reply) until
      Phase 2's Gift-Picker exists. Didn't fake a destination.
- [x] `track_order` → stub only until Phase 4.
- [x] `return_item` → stub only until Phase 5.
- [x] Test against hand-written messages per intent, including the ambiguous
      "check on the thing I sent last week" case (verified separately above,
      not in the automated harness since it needs pre-seeded session
      history) — `scripts/test_intent_router.py`, **10/10 passing**.
- [x] Boundary/adversarial cases for `out_of_scope` vs `chitchat` — same
      harness, all passing:
      - "can you check this product on eBay" → `out_of_scope` (not dragged
        into `gift_request` by "product")
      - "is eBay better than you" → `out_of_scope`
      - "what's the president of Sri Lanka" → `out_of_scope` (didn't just
        answer it)
      - "what can you do?", "thanks, bye!" → `chitchat`, not `out_of_scope`

## Phase 2 — Gift-Picker Agent (built and tested in isolation)
- [x] LangGraph LangGraph agent node wired to an `MultiServerMCPClient` (tool selection scoped to
      `search_products` / `get_product` / `list_categories` only — not
      `check_delivery` / `create_order` / `track_order`) —
      `src/gift_picker/agent.py::build_gift_picker_agent`.
- [x] Custom `get_recipient_profile` tool against the `recipients` table
      (`src/gift_picker/tools.py`, DB access in `src/db/recipients.py`) —
      matches loosely on name OR relationship (e.g. "mom" finds
      relationship="mother"), returns `{"matches": [...]}` and lets the
      model decide/ask rather than forcing a single-best-match heuristic
      in Python.
- [x] **Full conversation history for this agent.** Resolved by removing
      the history bound from the *top-level* graph invocation in `main.py`
      entirely (it now defaults to full history for every turn) rather
      than trying to keep the Router bounded within a shared context —
      see the long comment at the top of `src/orchestrator.py` for why
      that turned out to be the only workable option once router and
      Gift-Picker share one shared graph state. `classify_intent()`'s own
      standalone bounded path is untouched, for isolated router testing.
- [x] `propose_cart` as a plain LangChain `@tool`, not structured output —
      `src/gift_picker/tools.py::propose_cart`. Writes
      `{items, estimated_total, notes}` into `tool_context.state['cart']`
      and clears `product_suggestions` in the same call.
- [x] `propose_cart` docstring written with explicit timing guidance
      ("only once confident and concrete... not to tentatively summarize
      progress"). **Verified working, not just written**: live test
      correctly withheld the cart on a first turn showing 4 candidate
      products and asking a follow-up, then proposed a 2-item cart with
      correct IDs/prices/total once the customer picked specific items —
      see verification note below.
- [x] `suggest_products` as a second plain LangChain `@tool` —
      `src/gift_picker/tools.py::suggest_products`. Maps the live API's
      `id` field to `product_id` per the field-name note in the docstring
      and in `GIFT_PICKER_INSTRUCTIONS`.
- [x] `product_suggestions` reset structurally, not by instruction — the
      orchestrator yields its own a graph state update
      {"product_suggestions": []}))` immediately before invoking the
      Gift-Picker sub-agent, confirmed (via reading the LangGraph state-update/checkpoint behavior)
      to persist through `append_event` and be visible on graph state
      by the time the sub-agent runs, same turn.
- [x] `GIFT_PICKER_INSTRUCTIONS` (`src/prompts.py`) encodes the full
      decision policy: check `get_recipient_profile` before asking about a
      named recipient, search eagerly on partial info, never describe a
      product without having looked it up, combine "found"/"still need" in
      one reply, `suggest_products` before describing by name,
      `propose_cart` only once concrete.
- [x] Orchestrator-side dispatch (`src/orchestrator.py::_run_gift_picker`):
      scans the Gift-Picker sub-agent's own yielded events for a `"cart"`
      key in `the node's returned state update` (precise "did propose_cart fire
      this turn" signal — robust to a stale `cart` already sitting in
      state from an earlier turn, unlike a plain before/after key-presence
      check). Present → stub note (Phase 3 doesn't exist yet, matching the
      Phase 1 stub precedent for `track_order`/`return_item`). Absent →
      relay the Gift-Picker's own final text, console-print
      `product_suggestions` as a stand-in for "cards" (no real UI yet).
- [x] Test: **explicit products** shape ("flower bouquet and chocolates for
      the anniversary") — verified live, two-turn conversation:
      1. Searched real Kapruka products, called `suggest_products` with 4
         well-matched items (2 flower bouquets, 2 chocolate boxes),
         narrated text matched exactly what was suggested, correctly did
         *not* call `propose_cart` yet — asked about budget/delivery
         instead.
      2. Customer picked 2 of the 4 and gave a delivery city →
         `propose_cart` fired with the right 2 items, correct prices
         (LKR 4,000 + 3,750), correct total (7,750), sensible `notes`.
         Orchestrator correctly detected the state update and printed the
         Phase 3 stub transition.
- [ ] Test: **vague + implied bundle** shape ("surprise my mom for her
      birthday, plan a gift pack under 15000 LKR") with a seeded
      `recipients` row — harness ready at `scripts/test_gift_picker.py`
      (seeds/cleans up a "Mum"/mother row), **not yet verified — blocked**.
      Discovered mid-testing: `LLM_MODEL` (`gemini-3.8-flash`, set in
      `src/config.py`) is capped at **20 free-tier requests/day**
      (`generativelanguage.googleapis.com/generate_content_free_tier_requests`,
      confirmed via the API's own 429 response). Earlier "503 UNAVAILABLE"
      failures during this same testing session were likely this same
      quota, not real outages. Burned through it validating the explicit-
      products shape above; re-run once the quota resets or a
      higher-quota/paid model is configured.
- [ ] Test: **zero-result search** recovery — harness ready
      (`scripts/test_gift_picker.py::zero_results_case`), **not yet run**,
      same quota block.
- [ ] Test that rendered suggestions and narrated text agree — confirmed
      by inspection on the one live run above (4 suggested, 4 narrated);
      not yet deliberately stress-tested for a mismatch.
- [x] **Retroactive Phase 2 amendment** — added optional
      `delivery_city: str | None` / `delivery_date: str | None` to
      `propose_cart`'s schema (`src/gift_picker/tools.py`), captured into
      graph state's `cart`. `GIFT_PICKER_INSTRUCTIONS` tells the model to
      pass these along when the customer already said them, never to guess
      or ask just to fill them in.

## Phase 3 — Deterministic pipeline
Built as `src/checkout/` (`mcp_client.py`, `delivery.py`, `summary.py`,
`order.py`, `flow.py`) + a stage pre-check in `src/orchestrator.py`.
**Verified live**, up to and stopping just short of an actual `yes`
(placing a real order is a genuine financial action — didn't trigger one
during dev testing):
- [x] **New graph state's `stage` field**
      (`src/checkout/flow.py::STAGE_KEY`), checked by the orchestrator
      *before* the Intent Router runs: `collecting_delivery` |
      `resolving_delivery_conflict` | `awaiting_confirm` | absent (normal
      flow). Confirmed live both ways: a bare "yes" sent with no active
      checkout correctly fell through to normal classification (not
      checkout); once a checkout was in progress, replies were correctly
      *not* reclassified.
- [x] `collecting_delivery` — deterministic, one field per turn, no LLM
      call at all (verified: these turns produced no LLM request).
      **Scope discovered beyond the original plan text:** the live
      `kapruka_create_order` schema (checked against the real tool schema,
      not guessed) requires `recipient{name,phone}`, `delivery{address,
      city,date}`, `sender{name}` — not just city/date. `propose_cart` only
      captures city/date (see the Phase 2 amendment above), so
      `collecting_delivery` was extended to also collect recipient name,
      recipient phone, delivery address, and sender name, one at a time,
      via the same plain-text-ask pattern — checkout cannot succeed against
      the real API without them. **Known rough edge, confirmed live:** the
      Gift-Picker often already narrates these details itself when the
      customer states them up front (it's a capable model), so
      `collecting_delivery` asking again for values already stated reads as
      slightly repetitive. Not fixed here — `propose_cart`'s schema would
      need to grow further, deferred rather than expanded mid-flow.
- [x] **Check delivery per distinct product, not once per cart**
      (`src/checkout/delivery.py::check_delivery_for_cart`) — one
      `kapruka_check_delivery` call per distinct `product_id`. Verified
      live against the real MCP server (real delivery fee returned:
      LKR 300 for Colombo 03).
- [x] **On a failed check, hand back to the Gift-Picker agent.** A
      `[System note — not from the customer: ...]` event carrying the
      failure reason(s) is injected into the shared session before
      re-invoking the *same* Gift-Picker sub-agent
      (`src/checkout/flow.py::run_delivery_check` /
      `_run_gift_picker_for_revision`) — reused verbatim for the
      confirm-step "reply wasn't a yes" case too, per the plan's own
      framing that these are the same move. **Verified live** via the
      confirm-step path: a non-yes reply at `awaiting_confirm` correctly
      handed back to the Gift-Picker, which called `propose_cart` again
      (detected via `the node's returned state update`), which correctly re-ran
      the delivery check and re-landed on `awaiting_confirm` with a fresh
      summary. (The *actual failed-check* trigger for this same code path
      — e.g. a real undeliverable city — wasn't separately exercised live;
      it shares 100% of the code with the path that was.)
- [x] Perishable warning surfaced (not blocking) in the summary when
      present — `src/checkout/summary.py`.
- [x] **Show Summary** (`src/checkout/summary.py::build_summary`) — items,
      prices, delivery fee (max across items, since Kapruka's own docs call
      it a "flat" rate — see the comment on why `max` rather than assuming
      they're always identical), total, perishable notes, delivery/
      recipient/sender details. Verified live, exact output in the commit.
- [x] **Human Confirm** — deterministic keyword check
      (`src/checkout/flow.py::_is_confirmation`), not a classifier call.
      **Design note on `interrupt()`:** LangGraph's interrupt
      pause/resume was deliberately not used — it's implemented inside
      LangGraph's graph execution and interrupt mechanism and LangGraph checkpointing and interrupt/resume mechanism
      (confirmed by reading `flows/llm_flows/request_confirmation.py`),
      neither of which fits a deterministic, no-agent checkout call without
      either reintroducing agent judgment or standing up resumability just
      for this. "Unreachable without an explicit human confirm" is instead
      enforced structurally: `kapruka_create_order` has exactly one call
      site in the whole codebase (`src/checkout/order.py::create_order`,
      called only from `handle_awaiting_confirm` after the keyword check
      passes) — verifiable by inspection. Live-verified the gate itself
      (bare "yes" with no checkout in progress does nothing; a non-yes
      reply during confirm does not check out); the actual create_order
      call was deliberately never triggered during testing.
- [x] `Checkout` calls `kapruka_create_order` via a raw deterministic MCP
      client call (`src/checkout/mcp_client.py`), not `MultiServerMCPClient` — see
      the `interrupt()` note above for why. **Untested against a
      live success response** (real financial action) — response field
      names (`order_ref`, a pay-link key) are inferred from the tool's own
      schema/docs, not confirmed; flagged clearly in `order.py`'s docstring
      to verify the first time this actually runs.
- [x] On success: writes `phone_number`, `items` (JSONB), `product_summary`,
      `total_amount`, `delivery_city`, `delivery_date`, `kapruka_order_id`,
      `status` to `orders` (`src/checkout/order.py::save_order`) —
      required adding those four columns via `ALTER TABLE ... ADD COLUMN
      IF NOT EXISTS` in `src/db/schema.sql` (applied to the live Neon
      instance). Clears `cart`/`checkout_info`/`stage`/`collecting_field`
      structurally, same pattern as `product_suggestions`.
- [x] `Track Order` here (`src/checkout/order.py::track_order_once`) is a
      best-effort immediate status check right after checkout, distinct
      from Phase 4's `track_order` *intent*. An "order not found yet" result
      here is treated as expected (payment likely isn't complete yet), not
      an error.

## Phase 4 — Track-order branch
- [ ] Order-number extraction (ask if missing) → `kapruka_track_order` →
      format reply

## Phase 5 — Return-item branch
- [ ] Fallback response only — no MCP tool exists for this, don't build one

## Phase 6 — Wire the full graph + end-to-end test
- [ ] FastAPI webhook → Entry → Intent Router → the five branches
- [ ] Manually test all five intents through the real webhook, not just
      the graph in isolation

## Deferred — later, separate milestone
- Cron-triggered proactive reminders (draft, send, wait for reply)
- Re-classifying a reply through the Intent Router (confirm vs. new request)
