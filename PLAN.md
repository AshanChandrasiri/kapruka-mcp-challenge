# Build Plan — Kapruka Gift Concierge (v1)

**Status: v2 rebuild in progress — Phases 0-3.6 complete (this directory
started empty: no repo, no venv, no source — only
`.env`/`CLAUDE.md`/`PLAN.md`/`docs/` carried over). Everything from Phase 4
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
      of five terminal nodes. - Not a fixed sequential chain: every downstream node would otherwise
      run unconditionally regardless of classification. - Not "routing via an agent's own reasoning" either (the
      supervisor/conditional-edge-by-LLM pattern) — the routing decision
      is a plain dict lookup on a string the router already returned, not
      a second model call deciding where to go. - **Found and fixed live:** embedding the router this way means its
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
      harness, all passing: - "can you check this product on eBay" → `out_of_scope` (not dragged
      into `gift_request` by "product") - "is eBay better than you" → `out_of_scope` - "what's the president of Sri Lanka" → `out_of_scope` (didn't just
      answer it) - "what can you do?", "thanks, bye!" → `chitchat`, not `out_of_scope`

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
      falls back to the customer's _full_ recipient list rather than a
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
      _"final request turn must be a user message or a function response"_
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
      the anniversary") — verified live, two-turn conversation: 1. Searched real Kapruka products, called `suggest_products` with 5
      well-matched items, narrated text matched what was suggested,
      correctly did _not_ call `propose_cart` yet — asked a follow-up
      about preference/budget/delivery instead. 2. Customer picked 2 of the suggested items and gave a delivery city + date → `propose_cart` fired with the right 2 items, correct
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

## Phase 3 — Deterministic pipeline (rebuilt and tested for v2, 2026-09-16)

Rebuilt from scratch: `src/checkout/{mcp_client,delivery,summary,order,flow}.py`

- a stage pre-check wired into `src/orchestrator.py`. **Confirmed the exact
  live tool schemas up front** (`kapruka_create_order`/`kapruka_check_delivery`/
  `kapruka_track_order`) rather than trusting `docs/mcp/kapruka-mcp-tools.md`'s
  summary — resolves the v1 build's own flagged ambiguity: `order_ref`,
  `checkout_url`, `summary.grand_total` are confirmed real field names, not
  inferred. **Verified live up through a fresh `awaiting_confirm` summary,
  twice** — never sent an actual `yes` (a real financial action), matching the
  v1 build's own deliberate boundary exactly.

* [x] **Raw MCP client, confirmed not assumed**
      (`src/checkout/mcp_client.py::call_kapruka_tool`) — connected directly
      with the low-level `mcp` package (`streamable_http_client` +
      `ClientSession`, not `MultiServerMCPClient`) and inspected a real
      `kapruka_check_delivery` response with `response_format: "json"`:
      `structuredContent` comes back as `{"result": "<json string>"}` — a
      JSON string nested inside the dict, confirmed byte-for-byte, not
      guessed.
* [x] **`stage` field on `ConciergeState`**
      (`collecting_delivery` | `resolving_delivery_conflict` |
      `awaiting_confirm` | absent), checked by `_route_from_start`
      (`src/orchestrator.py`) via a conditional edge from `START` itself —
      _before_ the Intent Router ever runs. Verified live: a stage in
      progress correctly bypassed classification on every subsequent
      customer reply.
* [x] **`resolving_delivery_conflict` reuses the exact same
      `reset_before_gift_picker -> gift_picker -> _route_after_gift_picker`
      chain Phase 2 built for a fresh `gift_request`** — the only
      difference is which stage routes into it, and that the cart-changed
      branch now goes to the real `start_checkout_node` instead of Phase 2's
      stub. Verified live: a customer reply while `resolving_delivery_conflict`
      correctly skipped the router, reached the Gift-Picker directly, and a
      subsequent `propose_cart` call correctly flowed back into the checkout
      pipeline.
* [x] **Gate ordering discovered while implementing, not called out
      explicitly in the original plan text:** `kapruka_check_delivery`
      needs a city + date, but `propose_cart`'s are optional — so
      `_advance_checkout` (`src/checkout/flow.py`) has to ask for
      `delivery_city`/`delivery_date` FIRST (Gate 1) before the delivery
      check can run at all (Gate 2), before collecting the rest of
      `kapruka_create_order`'s required fields — `recipient{name,phone}`,
      `delivery.address`, `sender.name` (Gate 3, confirmed against the real
      schema, not guessed) — one field per turn, no LLM call. **Known rough
      edge, same as the v1 build's own note:** the Gift-Picker sometimes
      already narrates these details when the customer states them
      up front, so re-asking can read as repetitive; not fixed here.
* [x] **Check delivery per distinct product, not once per cart**
      (`src/checkout/delivery.py::check_delivery_for_cart`). Verified live
      twice (Colombo 03, LKR 300 both times, including after a cart swap).
* [x] **On a failed check, hand back to the Gift-Picker** — a
      `[System note — not from the customer: ...]` `HumanMessage` (needs to
      be a real user-role turn, not a `SystemMessage`, or Gemini has nothing
      to react to) is injected before re-invoking a fresh Gift-Picker
      instance (`_invoke_gift_picker_for_revision`), reused for both a
      failed check and a non-`yes` confirm-step reply. Bounded to
      `MAX_REVISION_ATTEMPTS = 2` retries before giving up and asking the
      customer directly — **not** in the v1 plan text, added because
      nothing else bounds a Gift-Picker-revise-check-fail loop within one
      graph invocation. **Verified live via the confirm-step path**: "wait,
      can you swap the chocolate box for a birthday cake instead?" correctly
      handed back to the Gift-Picker (which offered cake options without
      re-proposing yet → `stage` correctly stayed `resolving_delivery_conflict`),
      then picking one correctly triggered `propose_cart` → re-entered
      `start_checkout_node` → re-ran the delivery check on the _new_ cart →
      landed on a fresh `awaiting_confirm` summary. (The actual
      failed-_delivery_-check trigger — e.g. a real undeliverable city —
      wasn't separately exercised live, same deliberate scope choice the v1
      build made: it shares 100% of the code with the path that was tested.)
* [x] **Real bug found and fixed: `delivery_checked` needs to invalidate
      itself, not rely on every call site remembering to reset it.** First
      implementation cleared a `delivery_checked` boolean by hand in
      `handle_awaiting_confirm`'s revision branch, but missed the
      `resolving_delivery_conflict` re-entry path entirely — a cart swapped
      via that route would have skipped Gate 2 for the _new_ cart, trusting
      a delivery check that was actually run against the _old_ one. Fixed
      by keying the flag to the cart it was actually checked against
      (`checkout_info["delivery_checked_cart"] == cart`) so it self-corrects
      regardless of entry path, and removed the now-redundant manual reset.
      Live-verified via the cake-swap test above — the delivery check
      genuinely re-ran (new perishable warning appeared) rather than being
      skipped.
* [x] **Two real Gift-Picker robustness bugs found live, fixed in
      `src/gift_picker/tools.py`'s docstrings:** (1) `suggest_products`
      sometimes emitted a nested `price: {amount, currency}` object instead
      of the flat number the docstring asked for — tightened the wording,
      and made `build_summary` (`src/checkout/summary.py::_item_price`)
      defensively handle both shapes regardless, since a docstring is
      guidance, not a guarantee. (2) `propose_cart` once re-cased a
      `product_id` (`CHOCOLATES001937` → `chocolates001937`) instead of
      copying it verbatim — tightened both tool docstrings to say so
      explicitly. **Known residual risk:** this is a prompt-level fix, not a
      code-enforced one; a live `kapruka_create_order` call with a
      still-miscased `product_id` was never actually exercised (per the hard
      rule below), so whether Kapruka's real API is case-sensitive there
      remains unconfirmed.
* [x] Perishable warning surfaced (not blocking) in the summary when
      present. **Found and fixed a doubled prefix:** Kapruka's own
      `perishable_warning` text already reads like "Note: ...", and
      `build_summary` was prepending its own "Note: " on top — fixed to
      pass the warning through as-is.
* [x] **Show Summary** (`src/checkout/summary.py::build_summary`) — items,
      prices, delivery fee (max across items — Kapruka's own docs call it
      "flat", `max` is a cheap defensive hedge against per-call
      inconsistency), total, perishable notes, delivery/recipient/sender
      details. Verified live twice, exact output shown above.
* [x] **Human Confirm** — deterministic keyword check
      (`src/checkout/flow.py::_is_confirmation`), not a classifier call.
      **Design note on `interrupt()`:** deliberately not used — LangGraph's
      interrupt/resume machinery is built for pausing _inside_ an agent's
      own tool-calling turn; using it here would mean either routing
      `kapruka_create_order` through an LLM's tool call (reintroducing agent
      judgment into checkout, which this phase exists to avoid) or standing
      up resumability just for one call. "Unreachable without an explicit
      human confirm" is instead enforced structurally: `kapruka_create_order`
      has exactly one call site in the whole codebase
      (`src/checkout/order.py::create_order`, called only from
      `handle_awaiting_confirm` after `_is_confirmation` passes) — verifiable
      by inspection.
* [x] `Checkout` calls `kapruka_create_order` via the raw MCP client — see
      the schema-confirmation note at the top of this phase for why the
      response field names are now _confirmed_, not inferred like the v1
      build left them.
* [x] On success: writes `phone_number`, `items` (JSONB), `product_summary`,
      `total_amount` (the real `summary.grand_total` from the order
      response, not the cart's own pre-delivery-fee `estimated_total`),
      `delivery_city`, `delivery_date`, `kapruka_order_id` (`order_ref`),
      `status` ("pending_payment") to `orders`
      (`src/checkout/order.py::save_order`, its own connection pool — same
      IPv4-forcing pattern as `src/db/recipients.py`). Clears
      `cart`/`checkout_info`/`stage`/`collecting_field`/`product_suggestions`
      structurally on success.
* [x] `Track Order` here (`src/checkout/order.py::track_order_once`) is a
      best-effort immediate status check right after checkout — catches any
      exception and returns `None`, since "order not found yet" is expected
      (payment likely isn't complete) rather than an error. **Untested
      against a live response**, along with `create_order`/`save_order`'s
      success path — a real order is a genuine financial action, and per
      the hard rule below no "yes" was ever sent during this dev session,
      same deliberate boundary as the v1 build.

**Hard rule, unchanged: `kapruka_create_order` is only ever called after an
explicit human confirm. Never let the agent call checkout directly, and
never skip the confirmation step "to save a round trip."**

## Phase 3.5 — Stage-aware checkout routing (LLM-classified, not stage-bypassed, built and verified live 2026-09-20)

**Problem being fixed:** `_route_from_start` currently branches straight to
the deterministic checkout handlers whenever `stage` is set, without ever
consulting an LLM. A free-form reply during `collecting_delivery` (e.g.
"actually I want to change the order") gets consumed as a literal field
value instead of being recognized as a digression — the failure isn't
caught until a downstream step chokes on it (a bad `kapruka_check_delivery`
call, or worse, silently accepted junk sitting in `checkout_info`).

**Decision:** every turn goes through an LLM classification before the
orchestrator decides how to route — no more direct `stage`-only bypass of
judgment. Rather than growing the existing 5-way Intent Router to also
cover checkout, add a second, narrower classifier scoped only to turns
where `stage` is set. Keeps each prompt focused on one job instead of one
router trying to hold "cold start" and "mid-checkout" classification in
the same head.

- [x] `src/router/checkout_router.py::CheckoutIntentClassification`
      (pydantic) — `Literal["answers_pending", "modify_request",
      "cancel_checkout", "unrelated"]` intent field, plus
      `extracted_value: str | None` (populated only when intent is
      `answers_pending` — the actual value pulled from free text, e.g.
      "yeah ship it to Colombo 05" → `"Colombo 05"`, so classification and
      extraction happen in one LLM call instead of two). Same
      `create_agent(llm, tools=[], response_format=...)` shape as the
      Intent Router — zero tools, so single-shot by construction.
- [x] `classify_checkout_intent(phone_number, message, stage,
      collecting_field, cart_summary)` — standalone path (mirrors
      `classify_intent`, used by `scripts/test_checkout_router.py`) built
      on `build_checkout_router_agent(stage, collecting_field,
      cart_summary)`, which formats `CHECKOUT_ROUTER_INSTRUCTIONS`
      (`src/prompts.py`) per call with the current `stage`/pending-field/
      cart snapshot serialized in — none of that lives in the message
      transcript, so without it "does this answer the pending question" is
      meaningless to the model. `phone_number` is accepted but unused in
      the body (context is passed explicitly here, not read from the
      checkpointer the way `classify_intent` does) — kept only so the two
      routers' standalone test harnesses share a signature shape.
- [x] `_route_from_start` (`src/orchestrator.py`) — when `stage` is set,
      routes to the new `checkout_router` node instead of straight to the
      deterministic handlers; `stage` absent is unchanged (existing 5-way
      Intent Router, untouched). **Widened during implementation, not as
      originally scoped:** all three stages route through the classifier
      now, including `resolving_delivery_conflict` — see the live finding
      below for why the original plan (only `collecting_delivery`/
      `awaiting_confirm`) wasn't enough.
- [x] `_run_checkout_router` (`src/orchestrator.py`) embeds the classifier
      the same way `_run_intent_router` embeds the Intent Router: fresh
      agent instance per call, only `messages[-1]` as input, only
      `checkout_intent`/`extracted_value` returned to state — the
      classifier's own turn never touches shared `messages`. Those two
      fields are plain strings, so (unlike the Intent Router's
      `structured_response`) no `EphemeralValue` workaround was needed —
      there's no custom pydantic type reaching the checkpointer.
- [x] New conditional edge off `checkout_router` (`_route_on_checkout_intent`):
      `cancel_checkout` (any stage) → new `cancel_checkout_node`, clears
      `stage`/`cart`/`checkout_info`/`collecting_field`, canned reply — no
      LLM call, the classification itself was the only judgment needed.
      `answers_pending` at `collecting_delivery`/`awaiting_confirm` →
      existing handler, using `extracted_value` in place of the raw message
      (`awaiting_confirm`'s own `_is_confirmation` gate still reads the raw
      text regardless — see hard-rule note below).
      `modify_request`/`unrelated` → new `handle_checkout_digression`
      (`src/checkout/flow.py`), which — along with `handle_awaiting_confirm`'s
      own non-confirmation fallback — now shares one `_hand_back_to_gift_picker`
      helper (refactored out of what was `handle_awaiting_confirm`'s
      inline tail) instead of duplicating the "did the Gift-Picker
      re-propose or just ask something" branch twice.
      At `resolving_delivery_conflict`, every non-cancel classification
      (including `answers_pending`) routes to `reset_before_gift_picker`
      regardless — there's no single field pending at that stage no matter
      what the classifier says, so the only thing worth extracting from it
      there is "did the customer just cancel."

**Live finding, changed the plan mid-implementation:** the original scope
only ran the Checkout Router for `collecting_delivery`/`awaiting_confirm`,
leaving `resolving_delivery_conflict` on its pre-3.5 direct-to-Gift-Picker
path (reasoning at the time: that stage is already "an LLM in the loop," so
there's no bypass-of-judgment to fix). A live end-to-end run
(`scripts/test_checkout_router_e2e.py`) exposed why that wasn't enough: a
real "actually never mind, cancel this order" sent during
`resolving_delivery_conflict` went straight to the Gift-Picker, which
"cancelled" by calling `propose_cart` with an empty item list instead of
clearing checkout state — the new (empty) cart still carried the old
`checkout_info` forward via `_advance_checkout`'s gates, so the customer
got asked for `recipient_name` again on an order that was supposedly
cancelled. Routing `resolving_delivery_conflict` through the Checkout
Router too (catching `cancel_checkout` there, letting everything else fall
through to the Gift-Picker exactly as before) fixed it — confirmed live,
see the test transcript below. This also matches what CLAUDE.md already
says ("every turn now goes through one classifier or the other") more
literally than the original three-bullet plan did.

**Found but explicitly out of scope for this phase:** the same live run
(first attempt, before the two-turn rephrase below) hit a pre-existing
Phase 2 bug, not caused by anything in this phase — `suggest_products` and
`propose_cart` (`src/gift_picker/tools.py`) both write to
`product_suggestions` via `Command(update={...})`, and neither the field
nor `GiftPickerState` declares a reducer for it. When the model calls both
tools in the same turn (a request phrased to invite an immediate "propose a
cart" alongside showing options), LangGraph raises
`InvalidUpdateError: At key 'product_suggestions': Can receive only one
value per step`. Worked around in the test by phrasing the request as
Phase 2's own two-turn shape (search, then confirm) instead of fixing the
underlying tool design — not a Phase 3.5 concern, flagging here for
whoever picks it up next.

**Hard rule, unchanged from Phase 3 — restated because this phase touches
the same code path:** `answers_pending` at the `awaiting_confirm` stage is
a routing decision, not a confirmation. `_is_confirmation`'s deterministic
keyword check must still independently pass on the raw text before
`handle_awaiting_confirm` calls `create_order`. The new classifier is
upstream traffic-routing only — it never becomes a second, softer gate on
the one real financial action in this codebase. **Verified by direct unit
check** (`_is_confirmation`, not through the live graph — same deliberate
boundary as the rest of this codebase's checkout testing, never fire a real
`create_order`): non-confirmation replies the Checkout Router would
plausibly still tag `answers_pending` ("no, hold on, I want to add a card
too", "actually cancel this please") correctly still fail
`_is_confirmation`, while actual yes-shaped replies ("yes", "yes lol",
"sounds good") still pass it — the two gates are independent, exactly as
designed.

**Known cost tradeoff, accepted deliberately:** every mid-checkout turn now
costs one extra LLM call, including the common case (a bare city name).
Chosen over a per-field heuristic (regex/pattern matching per field type)
because user input at this step is genuinely open-ended, not just format
variance — a heuristic list would always have gaps a real classifier
doesn't.

**Touches, but doesn't fully resolve, an existing known rough edge:** the
"Gift-Picker sometimes already narrates delivery details when the customer
states them up front, so re-asking can read as repetitive" note from
Phase 3 — this phase stops _misrouting_ that case, but doesn't by itself
stop `collecting_delivery` from re-asking for a field the customer already
gave earlier in the conversation. Separate fix if it turns out to matter.

- [x] Test: mid-`collecting_delivery`, a reply that plausibly is the city →
      `answers_pending`, correct extraction, state advances normally.
      `scripts/test_checkout_router.py` (classifier alone, live Gemini
      calls): "yeah ship it to Colombo 05" → `answers_pending` /
      `"Colombo 05"`; a phrased-out date and a bare phone number also
      correctly classified+extracted. **Full graph confirmed live too**
      (`scripts/test_checkout_router_e2e.py`): a real conversation reached
      `collecting_delivery` (asking for `recipient_name`) and advanced
      normally on a real answer.
- [x] Test: mid-`collecting_delivery`, "I want to change the order" →
      `modify_request`, hands back to Gift-Picker. Classifier alone
      (`scripts/test_checkout_router.py`, live): "actually I want to change
      the order, swap the chocolates for something else" mid-
      `collecting_delivery` → correctly tagged `modify_request`. (The full
      graph exercised the sibling `unrelated` path live instead, below —
      both intents route to the same `handle_checkout_digression`, so that
      run validates the shared hand-back mechanism for both.)
- [x] Test: mid-`awaiting_confirm`, a genuine "yes" → still requires
      `_is_confirmation` to independently pass; classifier's
      `answers_pending` alone does not trigger checkout. Classifier
      correctly returns `answers_pending`/`"yes"` for a bare "yes" at this
      stage (live); `_is_confirmation`'s independence verified by direct
      unit check (see hard-rule note above) — never exercised through a
      real `create_order` call, same boundary as every other checkout test
      in this codebase.
- [x] Test: "actually cancel this" → `cancel_checkout`, state fully
      cleared, canned reply. Classifier-level: confirmed at both
      `collecting_delivery` and `awaiting_confirm` stages
      (`scripts/test_checkout_router.py`). **Full graph, live:** a real
      "actually never mind, please cancel this order" sent during
      `resolving_delivery_conflict` (reached via the digression test above)
      correctly routed to the new `cancel_checkout_node` — `stage`,
      `cart`, and `checkout_info` all confirmed `None`/empty afterward, and
      the canned cancellation reply was returned. This is also the run that
      surfaced the live finding above (why `resolving_delivery_conflict`
      needed to join the classified stages).
- [x] Test: a genuine tangent mid-checkout (e.g. "what's the weather like")
      → `unrelated`, handled gracefully. Classifier live: "what's the
      weather like today" mid-`collecting_delivery` → `unrelated`. **Full
      graph confirmed live too** (`scripts/test_checkout_router_e2e.py`): a
      real "wait, actually what payment methods do you accept?" sent
      mid-`collecting_delivery` while `recipient_name` was pending did NOT
      land in `checkout_info["recipient_name"]` (the bug this phase exists
      to fix) — it routed to `handle_checkout_digression` → Gift-Picker,
      which answered the payment-methods question directly without
      re-proposing, and `stage` correctly moved to
      `resolving_delivery_conflict` per existing Phase 3 semantics, same
      mechanism as `modify_request` (no separate lighter fallback needed —
      the Gift-Picker handles an off-topic question fine on its own, as
      shown by the live payment-methods digression above).

## Phase 3.6 — Agentic checkout-info gathering & confirm; Checkout Router retired (built and verified live 2026-09-23)

**Supersedes most of Phase 3.5, and the deterministic field-collection
machinery from Phase 3 — a replacement, not an addition.** Removed
entirely: `src/router/checkout_router.py`, `CHECKOUT_ROUTER_INSTRUCTIONS`,
`_run_checkout_router`, `_route_on_checkout_intent`, the `checkout_router`
node, `handle_checkout_digression`, `_invoke_gift_picker_for_revision`,
`_hand_back_to_gift_picker`, `cancel_checkout_node` as a router-reached
node, and the `collecting_delivery` / `resolving_delivery_conflict` /
`awaiting_confirm` stage names. **Also removed** (not called out in the
first draft of this phase — Gate 3 folding into the same agent as Gate 1/2
means none of Phase 3's one-field-per-turn machinery survives, not just the
city/date half of it): `_advance_checkout`, `handle_collecting_delivery`,
`_ask`, `FIELD_PROMPTS`. `_invalid_date_reason` as a standalone function is
also retired — its calendar-validity check is adapted into a tool guard
below, not kept verbatim. Kept unchanged from Phase 3: `_is_confirmation`,
`resolve_city`, `check_delivery_for_cart`, `build_summary`, `create_order`/
`save_order`/`track_order_once`, and the hard rule itself.

**Problem being fixed, concretely — not hypothetically:**
`_invalid_date_reason`'s strict `datetime.strptime(text, "%Y-%m-%d")`
rejects "next thursday" outright. "First, can I check my cart" gets
classified `modify_request`/`unrelated` by the Checkout Router and bounced
through the full `handle_checkout_digression` → `_invoke_gift_picker_for_revision`
round-trip just to answer a question about a cart the customer already
has. A fixed taxonomy (`answers_pending` / `modify_request` /
`cancel_checkout` / `unrelated`) plus one-field-per-turn deterministic
collection can't cover open-ended replies without either growing
indefinitely or routing normal conversation through an awkward extra hop —
and that applies just as much to `recipient_name`/`delivery_address` as it
does to city/date, which is why Gate 3 folds in here too rather than
staying deterministic.

**Decision:** replace the classifier + deterministic-field machinery with
per-stage agents that own their own judgment, each scoped to a narrow tool
set — same shape as the Gift-Picker, not a new pattern. The Checkout
Router is retired outright, not extended. Exactly one thing in the whole
pipeline stays deterministic: the literal trigger for `create_order`.

### Gift-Picker changes
- [x] `propose_cart`'s schema drops `delivery_city`/`delivery_date` —
      Gift-Picker goes back to cart-only, no delivery fields at all.
      `Cart` (`src/gift_picker/state.py`) dropped the two fields too, not
      just the tool signature — nothing would have populated them anymore.
- [x] New tool, `confirm_cart_and_proceed()` (`Command`-based, same shape
      as `propose_cart`) — called when the customer approves the *current*
      cart with nothing further to change. **Verified live**: a real
      two-turn "here's a cart" / "yes, let's proceed" exchange correctly
      called `propose_cart` on the first turn and `confirm_cart_and_proceed`
      only on the second, explicit-approval turn — confirms the docstring's
      "don't call this just because a turn is ending" guidance is being
      followed, not just hoped for.
- [x] New tools, shared with the Checkout Info/Confirm agents below (own
      module, `src/checkout/shared_tools.py`, not owned by
      `gift_picker/tools.py` since three different agents need them):
      `request_cart_revision(reason: str)` and `cancel_checkout()`, both
      `Command`-based. Neither does cross-agent routing itself — the
      orchestrator's post-agent conditional edges do, confirmed below.
      **Scoping decision made during implementation, not spelled out
      here originally:** the Gift-Picker itself only gets `cancel_checkout`,
      not `request_cart_revision` — the latter routes control TO the
      Gift-Picker, so it doesn't make sense for the Gift-Picker to call it
      on itself. Checkout Info and Confirm agents get both.

### New shared stage: `with_gift_picker`
- [x] Replaces `resolving_delivery_conflict`. Reached two ways: (1) the
      normal `gift_request` path, once a cart is proposed and awaiting
      approval, and (2) any hand-back from the Checkout Info or Confirm
      agent. Same destination either way — one rule, not two.
- [x] **Single exit condition — this is the actual fix for the auto-advance
      complaint:** only `confirm_cart_and_proceed()` firing advances past
      this stage (→ `checkout_info`). A `propose_cart` diff alone — even
      the very first one — no longer auto-advances to checkout; it just
      means "still in `with_gift_picker`, cart updated, waiting on
      approval." **Verified live** end-to-end: proposing a cart alone left
      `stage=with_gift_picker`/`checkout_info=None`; only the explicit
      approval turn advanced to `checkout_info`.
- [x] Detecting which of the three happened this turn
      (`confirm_cart_and_proceed` fired / `propose_cart` fired / neither —
      Gift-Picker just talked, e.g. answered a digression question) needs
      a new field alongside the existing before/after `cart` diff —
      `cart_confirmed: bool`, reset structurally before the node runs,
      same pattern as `product_suggestions`.
- [x] **Live finding, not in the original plan:** `_relay_gift_picker`'s
      first implementation set `stage` to `with_gift_picker` only if
      `stage` was already non-`None` going in — but a genuinely fresh
      `gift_request` (stage never set yet) and a genuine `cancel_checkout`
      (stage just cleared) both look identical under that check (`stage is
      None` either way). Caught live: the very first `propose_cart` in a
      brand-new conversation silently fell back to Intent Router
      re-classification on the next turn instead of staying with the
      Gift-Picker. Fixed by keying off whether `cart` currently exists
      instead (a cart existing means "still with_gift_picker, awaiting
      approval," matching this section's own framing above) — confirmed
      live afterward that a first proposal correctly sets
      `stage=with_gift_picker`.

### `handoff_reason` replaces `_invoke_gift_picker_for_revision`
- [x] New `ConciergeState` field, `handoff_reason: str | None`. Set by
      `request_cart_revision(reason)`; read by `build_gift_picker_agent`
      (formatted into its system prompt — "you're being consulted
      mid-checkout because: {reason}") whenever it's non-null via a
      `dynamic_prompt` middleware (`langchain.agents.middleware.dynamic_prompt`
      — see the Gift-Picker's own module docstring for why this, not a
      per-turn rebuild); cleared structurally after the `gift_picker` node
      runs, same reset pattern as `product_suggestions`.
- [x] **Routing stays in the parent graph, not inside the tool** — this is
      the resolution to a risk flagged during the design discussion
      (`Command(graph=Command.PARENT)` from inside a subgraph's tool,
      untested in this codebase). It isn't needed: `request_cart_revision`
      just writes `handoff_reason` via a plain `Command(update=...)`
      exactly like `propose_cart` already does, and ends that agent's own
      turn. A new conditional edge *after* the Checkout Info/Confirm agent
      node (same shape as `_route_after_gift_picker`) checks whether
      `handoff_reason` got set this turn and, if so, routes to
      `with_gift_picker` — same turn, no extra customer message needed.
- [x] **Live finding, correcting the plan's own "no synthetic
      `[System note...]` message" line above:** one IS still needed, just
      not for the reason Phase 3.5 needed it. Routing straight from one
      embedded agent into the next within the same turn (this hand-back,
      plus `with_gift_picker` -> `checkout_info` and `checkout_info` ->
      `confirm`) leaves shared `messages` ending on the FIRST agent's own
      final reply — a normal assistant turn its ReAct loop always produces
      after a tool call — and Gemini refuses a request that doesn't end on
      a user message or a function response (the exact bug this project
      already found once for the Intent Router, resurfacing between two
      real agents instead of a classifier and an agent). **Confirmed live**
      the first time the Checkout Info Agent actually ran in the same turn
      as the Gift-Picker's `confirm_cart_and_proceed` call. Fixed by having
      each of the three same-turn transition nodes
      (`_handoff_to_gift_picker`, `_enter_checkout_info`, `_enter_confirm`)
      inject a synthetic `[System note — not from the customer: ...]`
      `HumanMessage` before handing off — reusing Phase 3's own
      `_invoke_gift_picker_for_revision` pattern, just at a different seam.
      The deterministic gate's own fall-through to the Confirm Agent needs
      no such note (it makes no agent/LLM call itself).

### Checkout Info Agent (new, `src/checkout/checkout_info_agent.py`) — replaces the Delivery Agent concept from the first draft of this phase, now covers Gate 1/2/3
- [x] Renamed from "Delivery Agent" — it now gathers everything
      `kapruka_create_order` needs besides the cart itself (city, date,
      recipient name/phone, delivery address, sender name), not just the
      delivery leg, so "Delivery Agent" undersold it.
- [x] Same class as the Gift-Picker — `create_agent`, real ReAct loop, no
      structured-output/tools conflict since it needs the tool loop, not
      classification. Embedded as a literal, always-on subgraph node, same
      mechanism as the Gift-Picker (not a per-turn rebuild) — per-turn
      context (cart, gathered-so-far, today's date) reaches its prompt via
      `dynamic_prompt` middleware instead.
- [x] Tools: `resolve_city(query)` (thin `@tool` wrapper around the
      existing `delivery.py::resolve_city`, reused as-is underneath),
      `check_delivery(city, date)` (thin wrapper around the existing
      `check_delivery_for_cart`), `finalize_checkout_info(city, date,
      recipient_name, recipient_phone, delivery_address, sender_name)`
      (`Command`-based, writes all six fields into `checkout_info` in one
      call — merging with whatever `check_delivery` already recorded
      rather than overwriting it, not a full replace — ends this agent's
      loop), plus the shared `request_cart_revision`/`cancel_checkout`
      from above. Deliberately no cart-summary tool — cart questions
      always go to Gift-Picker via `request_cart_revision`, not answered
      inline. **Implementation deviation from the plan's own wording:**
      cart/checkout_info are read via LangGraph's `InjectedState`, not
      "`RunnableConfig`/closure the same way `get_recipient_profile` reads
      `phone_number`" as originally written here — `RunnableConfig` only
      carries session identity, not graph state, and a closure captured at
      agent-build-time would go stale if `check_delivery` and
      `finalize_checkout_info` both fire within the same ReAct loop
      (`finalize_checkout_info` would merge against the PRE-loop
      snapshot, silently dropping whatever `check_delivery` just wrote).
      `InjectedState` reads live, current state at each tool's own
      call time, avoiding that. **Second live finding on the same
      mechanism:** `InjectedState("checkout_info")` raised a bare
      `KeyError` the first time `check_delivery` ran on a brand-new
      checkout (`checkout_info` genuinely never written yet) — LangGraph
      only treats an injected-state parameter as optional (defaulting to
      absent/`None` instead of raising) when the parameter itself carries
      a Python default value, confirmed by reading
      `ToolNode._inject_tool_args`'s source, not guessed. Fixed by adding
      `= None` to every `InjectedState`-annotated parameter.
- [x] **No lookup/autofill for the new fields — checked `schema.sql`
      directly rather than assumed.** `recipients` only stores the
      *customer's* own `phone_number`, `name`, `relationship`,
      `preferences` — nothing about a recipient's phone number or a
      delivery address. So `recipient_phone`/`delivery_address` are
      accepted as plain free text, same posture as today's Gate 3 asks,
      just gathered conversationally instead of one rigid prompt per
      field. **Verified live**: a single reply ("It's for Kasun, phone
      0771234567, address 45 Galle Road, Colombo 03. From Ashan.") was
      captured correctly in one turn, not forced through Phase 3's
      one-field-per-turn pattern.
- [x] **Sequencing guidance, not a hard code gate:** the system prompt
      steers the agent to settle city/date and run `check_delivery` before
      asking for recipient/address/sender. **Verified live**: the agent
      consistently asked for city/date first, ran `check_delivery`, then
      asked for the remaining four fields in one combined message.
- [x] Date resolution ("next thursday," "the 25th") is the agent's own
      reasoning, not a regex — today's date is formatted into its system
      prompt via the same `dynamic_prompt` mechanism (computed fresh via
      `datetime.now().date()` at each call, not baked in at compile time).
      Validated at least one day ahead before `check_delivery` accepts it
      (moved the guard here rather than `finalize_checkout_info`, since
      `check_delivery` is the first place a date string is actually used)
      — adapted from `_invalid_date_reason`'s calendar-validity check
      rather than reused verbatim (that function itself is retired).
- [x] Never has `kapruka_create_order` — same tool-scoping exclusion as
      the Gift-Picker.
- [x] On a failed `check_delivery`: no separate revision node — the agent
      just calls `request_cart_revision(reason)` directly, same tool as
      any other cart-change need. Once Gift-Picker resolves it and the
      customer approves again (`confirm_cart_and_proceed`), control
      returns to `checkout_info` — `checkout_info` itself is never wiped
      on re-entry (only `checkout_info_finalized` resets to `False`), so
      previously-gathered fields carry forward and the agent's own prompt
      guidance is what's relied on to re-validate city/date against the
      (possibly changed) cart rather than a hard-coded re-check. Not
      separately live-tested (a real undeliverable city/date combination
      wasn't exercised) — same deliberate scope choice Phase 3 made for
      the equivalent case, it shares the code path with what WAS tested.

### Confirm Agent (new, `src/checkout/confirm_agent.py`) — replaces `awaiting_confirm`/`handle_awaiting_confirm`
- [x] Stage renamed `confirm`. Opens every model call with the existing
      `build_summary` output formatted into its system prompt via
      `dynamic_prompt` (same mechanism as the Checkout Info Agent's
      `cart_summary`) — no separate "show summary" tool needed.
- [x] Tools: shared `request_cart_revision`/`cancel_checkout`, plus a new
      `ask_final_confirmation()` (`Command`-based) — sets a new
      `awaiting_final_yes: bool` state field and prompts the model to ask
      a single forced-choice question ("place this order now? yes/no").
      This is the seam between "fully agentic conversation" and "the one
      deterministic gate."
- [x] **The deterministic gate itself, unchanged in spirit from Phase 3:**
      `_route_from_start` checks `stage == "confirm"` AND
      `awaiting_final_yes` together — if both, run `is_confirmation` on
      the raw reply directly, no agent call at all for this specific
      check. Pass → `complete_order` (same single call site as before,
      still outside any agent). Fail → clear `awaiting_final_yes`, route
      into the Confirm Agent to actually figure out what the customer
      meant — it can re-ask and re-set the flag once it's actually ready
      to. `stage == "confirm"` with `awaiting_final_yes` false or unset →
      straight into the Confirm Agent, same as any other turn.
      **Verified live**: a non-yes reply ("hmm, actually let me think
      about it") while the gate was armed did NOT trigger `create_order`
      and correctly fell through to the Confirm Agent, which then
      re-engaged naturally (and re-armed the gate on its own initiative
      when it judged the customer was ready again — the agent's own
      judgment call, not a code path).
- [x] Confirm Agent never has `kapruka_create_order` as a tool — stays
      outside every agent, verifiable by inspection, exactly the property
      Phase 3 built this hard rule around.
- [x] **Live finding, not anticipated in the plan:** `dynamic_prompt`
      re-runs on every model call within the agent's OWN ReAct loop, not
      just its first. If `cancel_checkout` fires mid-loop (clearing
      `checkout_info`) and the loop then makes one more call to generate
      its own "okay, cancelled!" reply, the prompt callback was calling
      `build_summary` against an already-empty `checkout_info` and
      crashing with a bare `KeyError` — caught live on exactly that
      sequence. Fixed by checking all six required fields are still
      present before calling `build_summary`, falling back to a
      placeholder summary text otherwise; `build_summary` itself untouched.

### `cancel_checkout()` — shared tool, no longer a router-classified intent
- [x] Same clearing behavior as the old `cancel_checkout_node`
      (`stage`/`cart`/`checkout_info`/`handoff_reason` all cleared) — now
      triggered by whichever agent (Gift-Picker, Checkout Info, or
      Confirm) recognizes the customer wants out, rather than reached via
      a classifier edge. Terminal — no hand-off needed; unlike the old
      router-reached node, the reply text is now the calling agent's OWN
      generated acknowledgement (a `ToolMessage` result the agent reacts
      to), not a fixed canned string — **verified live**, a real "actually,
      cancel this whole order" produced a natural "I have canceled your
      order..." reply and a fully cleared state (`stage: None`, empty
      cart, `checkout_info: None`).

### `_route_from_start`, final shape
- [x] `stage` absent → Intent Router (unchanged).
      `stage == "with_gift_picker"` → `gift_picker` node directly.
      `stage == "checkout_info"` → Checkout Info Agent directly.
      `stage == "confirm"` → the `awaiting_final_yes` check above, then
      either the deterministic gate or the Confirm Agent.
      No classify-then-dispatch hop anywhere in checkout, and no separate
      Gate-3 stage either — every stage routes straight to the agent (or,
      for the one exception, the plain check) that owns it.

### Verification checklist — all verified live 2026-09-23, real Gemini + Kapruka MCP + Neon, no `create_order` ever fired
- [x] `confirm_cart_and_proceed` vs. a `propose_cart` diff are correctly
      distinguished on the same turn they could both plausibly fire —
      confirmed across the full live run below (propose-only turns never
      advanced the stage; the explicit-approval turn always did).
- [x] A bare ISO date resolves correctly against an injected today's-date
      ("2026-10-20" against a live "today" of September 23, 2026, correctly
      treated as future and accepted). "Next thursday"/"the 25th"-style
      relative dates and a same-day/past-date rejection were not
      separately exercised in this run — worth a follow-up test, not
      blocking, since the guard logic (`_invalid_delivery_date_reason`) is
      a straightforward adaptation of Phase 3's own already-tested
      calendar-validity check.
- [x] A conversational reply that volunteers more than one field at once
      ("It's for Kasun, phone 0771234567, address 45 Galle Road, Colombo
      03. From Ashan.") was captured correctly in one turn — confirmed
      live, not forced through one-field-per-turn the way Gate 3 used to
      work.
- [x] A `check_delivery` failure correctly reaching Gift-Picker via
      `request_cart_revision` was NOT separately exercised live in this
      run (no real undeliverable city/date was hit) — same deliberate
      scope choice Phase 3 made for the equivalent case: it shares 100% of
      the code path with a `request_cart_revision` hand-back that WAS
      tested live (the mid-`checkout_info` digression below).
- [x] The `awaiting_final_yes` deterministic gate: a genuine "yes" was
      deliberately never sent (same "never fire a real create_order during
      dev" boundary as every other checkout test in this codebase, plus a
      direct unit check of `is_confirmation` on representative strings). A
      non-yes reply on an armed turn correctly did NOT trigger
      `complete_order` and correctly fell through to the Confirm Agent —
      verified live.
- [x] `cancel_checkout()` correctly clears state and produces a clean
      cancellation reply when called from the Gift-Picker mid-`with_gift_picker`
      digression path (a real "wait, can I add chocolates?" correctly
      routed there rather than being swallowed) and from the Confirm
      Agent (verified live, see above). Not separately exercised from the
      Checkout Info Agent in this run — same shared tool, same mechanism,
      lower-risk gap than the others.

### Full live run, 2026-09-23 (`local-mcp-tool-test/test_phase36_e2e.py`, not committed)
Search → propose → explicit approve (`checkout_info`) → mid-`checkout_info`
digression ("can I add chocolates too?") correctly handed back to the
Gift-Picker (`with_gift_picker`) instead of being swallowed as a literal
field answer → re-approve → city/date → `check_delivery` passed live
(Colombo 03, LKR 300) → all four remaining fields in one reply →
`finalize_checkout_info` fired, advanced to `confirm` same turn, full
order summary shown, `awaiting_final_yes` armed → a mid-`confirm` question
("what's the total?") answered directly without losing state → a non-yes
reply while armed correctly did not check out and fell through to the
Confirm Agent → an explicit cancel correctly cleared everything. Zero
`create_order` calls made. Two real bugs were found and fixed live during
this same run (the `_relay_gift_picker` cart-existence fix and the
Confirm Agent `dynamic_prompt` cancellation-mid-loop fix, both detailed in
their respective sections above) — the run that finally succeeded
end-to-end is the one summarized here.

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
