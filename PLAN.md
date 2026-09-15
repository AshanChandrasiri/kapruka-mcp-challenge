# Build Plan — Kapruka Gift Concierge (v1)

**Status: v2 rebuild in progress — Phases 0-2 complete (this directory
started empty: no repo, no venv, no source — only
`.env`/`CLAUDE.md`/`PLAN.md`/`docs/` carried over). Everything from Phase 3
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

## Phase 2 — Gift-Picker Agent (rebuilt and tested for v2, 2026-09-15)
Rebuilt from scratch: `src/gift_picker/{agent,tools,state}.py`,
`src/db/recipients.py`, `src/db/conninfo.py` (promoted out of
`src/session.py` — both the checkpointer and the new recipients connection
pool needed the same IPv4-forcing fix from Phase 0).

- [x] The only real ReAct loop in the system — `create_agent` wired to the
      Kapruka MCP tools via `MultiServerMCPClient`, filtered to
      `kapruka_search_products` / `kapruka_get_product` /
      `kapruka_list_categories` only (never `check_delivery`/`create_order`/
      `track_order`) — `src/gift_picker/agent.py::build_gift_picker_agent`.
      The scoped tool list is cached (network round-trip to list them is
      unnecessary every turn).
- [x] Custom `get_recipient_profile` tool (`src/gift_picker/tools.py`) — DB
      access in `src/db/recipients.py` (async psycopg pool). Matches loosely
      on name OR relationship via `ILIKE`; when that comes back empty,
      falls back to the customer's *full* recipient list rather than a
      hardcoded alias table (e.g. "mom" vs a stored `relationship="mother"`)
      — lets the model do the semantic matching instead of Python. Returns
      `{"matches": [...]}`, never a forced single best match. Scoped to the
      caller's own `phone_number`, read out of `RunnableConfig` (LangChain's
      built-in "a parameter typed exactly `RunnableConfig` gets
      auto-injected" mechanism) rather than trusting the model to pass its
      own phone number.
- [x] **Full conversation history for this agent** — true by construction:
      the top-level orchestrator invocation was never bounded to begin with
      (see Phase 1), so there was nothing to undo here.
- [x] `propose_cart` as a plain `@tool` returning `Command(update={"cart":
      ..., "product_suggestions": [], "messages": [...]})` —
      `src/gift_picker/tools.py::propose_cart`. Schema includes
      `delivery_city`/`delivery_date` from day one (the v1 build added these
      later as a "retroactive amendment"; built in here since the need was
      already known).
- [x] `propose_cart` docstring carries explicit timing guidance ("ONLY once
      you and the customer have converged on specific items... not to
      tentatively summarize progress"). **Verified live, not just written**
      — see the explicit-products test below.
- [x] `suggest_products` as a second plain `@tool` — maps the live API's
      `id` field to `product_id` per the field-name note in both the
      docstring and `GIFT_PICKER_INSTRUCTIONS`; verified live (all returned
      items correctly carried `product_id`, never raw `id`).
- [x] `product_suggestions` reset structurally, not left to the model —
      `_reset_before_gift_picker` (`src/orchestrator.py`) returns
      `{"product_suggestions": [], "cart_snapshot": state.get("cart")}` as
      its own graph step immediately before the Gift-Picker node runs, so
      the reset is committed and visible by the time it does.
- [x] `GIFT_PICKER_INSTRUCTIONS` (`src/prompts.py`) encodes the full
      decision policy: check `get_recipient_profile` before asking about a
      named recipient, search eagerly on partial info, never describe a
      product without having looked it up, combine "found"/"still need" in
      one reply, `suggest_products` before describing by name,
      `propose_cart` only once concrete.
- [x] **Orchestrator-side dispatch, redesigned from the v1 plan's own
      approach.** The Gift-Picker is embedded as a real subgraph node
      (`graph.add_node("gift_picker", await build_gift_picker_agent())`),
      sharing `messages`/`product_suggestions`/`cart` with `ConciergeState`
      via matching field names (`src/gift_picker/state.py`) — same
      mechanism Phase 1 uses for the router. Detecting "did `propose_cart`
      fire THIS turn" (not a stale cart from an earlier turn) is done by
      **snapshotting `cart` immediately before the Gift-Picker runs and
      diffing it against `cart` immediately after** (`_route_after_gift_picker`
      in `src/orchestrator.py`) — not by watching for a key in a yielded
      event. An `EphemeralValue` signal (the mechanism Phase 1 uses for
      `structured_response`) was tried first and doesn't work here: it only
      survives exactly one step past the write, but the Gift-Picker's own
      ReAct loop always needs one more internal step after any tool call
      (the model's reply acknowledging the tool result) before the subgraph
      itself returns to the parent — so an ephemeral flag set inside that
      subgraph is already cleared by the time control returns here. The
      plain before/after value comparison has no such timing gap and is
      still robust to a stale cart (a value diff, not a presence check).
      Cart changed → `_cart_proposed_stub` (Phase 3 doesn't exist yet,
      matching the Phase 1 stub precedent) logs a note; the Gift-Picker's
      own narration (already in shared `messages`) is left as the reply,
      unlike Phase 1's stubs, which had no real agent output to relay.
      No cart change → `_no_cart_relay` console-prints `product_suggestions`
      as a stand-in for "cards" (no real UI yet).
- [x] **Real bug found and fixed while wiring this up:** embedding the
      Intent Router as a literal subgraph node (Phase 1's original design)
      shares `messages` in both directions — its own classification-turn
      `AIMessage` was getting appended to shared history. Harmless on its
      own, but once the Gift-Picker (a second real model call in the same
      turn) reads that history, Gemini rejects the request outright:
      *"final request turn must be a user message or a function response"*
      — no prefill support, and history now ended in an assistant turn with
      nothing after it. Fixed by wrapping the router in `_run_intent_router`
      (`src/orchestrator.py`), which calls it with `state["messages"]` as
      input but returns only `{"structured_response": ...}` — the router's
      internal turn should never have been part of the customer-facing
      transcript regardless of this bug. Phase 1's own tests still pass
      unchanged after this fix.
- [x] `src/pipeline.py::run_turn` fixed to read `.text` off the final
      message instead of `.content` — Gemini's `AIMessage.content` is a list
      of content blocks (with a `signature` field etc.), not a plain string;
      `.content` was leaking that raw structure into the reply text.
- [x] Test: **explicit products** shape ("flower bouquet and chocolates for
      the anniversary") — verified live, two-turn conversation:
      1. Searched real Kapruka products, called `suggest_products` with 5
         well-matched items, narrated text matched what was suggested,
         correctly did *not* call `propose_cart` yet — asked a follow-up
         about preference/budget/delivery instead.
      2. Customer picked 2 of the suggested items and gave a delivery city
         + date → `propose_cart` fired with the right 2 items, correct
         `product_id`s, correct prices (LKR 5,210 + 4,150), correct total
         (9,360), captured `delivery_city`/`delivery_date` exactly as
         stated (not guessed). `_route_after_gift_picker` correctly detected
         the change and logged the Phase 3 stub transition;
         `product_suggestions` correctly cleared to `[]`.
- [x] Test: **vague + implied bundle** shape ("surprise my mom for her
      birthday, plan a gift pack under 15000 LKR") with a seeded
      `recipients` row (relationship="mother", preferences="loves tea,
      floral scents, and Ferrero chocolates", cleaned up after the test) —
      verified live: `get_recipient_profile("mom")` correctly matched the
      "mother" row, products searched were genuinely aligned with the
      stored preferences (tea box, Ferrero Rocher, flowers, cake), stayed
      within budget (13,940 of 15,000), and correctly held off on
      `propose_cart` pending delivery city/date. No quota block this
      time — Phase 0 already corrected the model name
      (`gemini-2.5-flash` → `gemini-3.6-flash`); the 20-req/day free-tier
      cap the v1 build hit doesn't apply to `gemini-3.6-flash`. Did hit
      Kapruka MCP's own 60-req/min rate limit once from rapid manual
      testing in this same session (429, not a bug) — waited it out.
- [ ] Test: **zero-result search** recovery — not yet run. Deferred rather
      than burning more of the shared, public Kapruka MCP endpoint's rate
      limit on speculative edge cases; revisit if a real conversation hits
      it.
- [x] Test that rendered suggestions and narrated text agree — confirmed on
      both live runs above (suggested counts and narrated items matched
      exactly each time).

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
