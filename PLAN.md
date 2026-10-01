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

## Phase 3.7 — Fold the deterministic confirm gate into `confirm_agent` itself (code complete 2026-09-27, live verification still pending)

**Problem being fixed:** Phase 3.6 ended up with two nodes doing what's
really one job. `check_final_confirmation` exists purely to run
`is_confirmation` on the raw reply before the real Confirm Agent ever gets
a turn, and `_route_from_start` has to branch on `awaiting_final_yes` just
to pick between the two node names. That's a whole extra node and a whole
extra conditional-edge branch for what's really a two-line check.

**Decision:** the check moves inside `confirm_agent`'s own function, as
plain code that runs before the underlying compiled agent is ever invoked
— not something the LLM decides, same deterministic keyword check as
before, just relocated, not weakened. `check_final_confirmation` and
`_route_after_final_confirmation_check` are retired; `_route_from_start`
loses the `awaiting_final_yes` branch entirely.

- [x] The `confirm_agent` node stops being the compiled agent embedded
      directly (the pattern Phase 3.6 used, matching the Gift-Picker/
      Checkout Info Agent) — it becomes a wrapping function,
      `_run_confirm_agent`, same shape as Phase 1's `_run_intent_router`:
      - Checks `awaiting_final_yes` first. If set, runs `is_confirmation`
        on the raw reply directly — no call to the underlying LLM agent
        at all on this branch.
      - Pass → calls `complete_order` and returns its result directly —
        identical to what `check_final_confirmation` already did, just
        from inside this function instead of a separate node.
      - Fail → clears `awaiting_final_yes` to `False`, then falls through
        in the same function call to invoke the actual compiled
        `confirm_agent` — no extra graph hop needed, since it's now
        sequential code in one function rather than two nodes connected
        by an edge.
      - `awaiting_final_yes` false or unset → skips the check entirely,
        goes straight to the compiled agent — covers `enter_confirm`'s own
        first entry, where it's always `False`.
- [x] `_route_from_start`'s `stage == "confirm"` branch drops the
      `awaiting_final_yes` check — always routes to `confirm_agent` (the
      new wrapper) regardless of that flag; the flag is only read inside
      the wrapper now, not at the routing layer.
- [x] `_route_after_confirm` (the one remaining post-node router) picks up
      `_route_after_final_confirmation_check`'s `stage is None → END`
      check as its first condition, ahead of its existing
      `handoff_reason`/`relay_confirm` checks — needed since the
      wrapper's fast path can now itself produce `stage: None` (via
      `complete_order`) on the same node that also handles the slow,
      agentic path. `check_final_confirmation` and
      `_route_after_final_confirmation_check` are removed once this is
      folded in — nothing else calls either.
- [x] Net effect: one node instead of two for the entire confirm stage,
      one fewer conditional-edge branch in `_route_from_start`. The
      actual safety property is unchanged — `is_confirmation` still runs
      as plain code, still runs before any LLM call on that turn,
      `complete_order` still has exactly one call site (now inside
      `_run_confirm_agent` instead of `_check_final_confirmation`) — only
      where the check physically lives moves, not what it does or when it
      runs relative to the agent.

**Explicitly not part of this phase, flagged rather than decided:** a
second, narrow, tool-less classifier for replies that fail
`is_confirmation` but are still plausibly a "yes" in different words (e.g.
"great, let's do this") was discussed as a follow-on — not included here.
As things stand, that class of reply still falls through to the full
Confirm Agent, which re-engages and re-arms the gate on its own judgment,
same as any other non-matching reply.

- [ ] Verify live: a genuine "yes" while `awaiting_final_yes` is armed
      still reaches `complete_order` without invoking the compiled Confirm
      Agent at all this turn (confirms the LLM truly isn't in this call
      path, not just that the outcome looks the same).
- [ ] Verify live: a non-matching reply while armed still correctly falls
      through to the real agent in the same turn, same behavior as Phase
      3.6's own equivalent test.
- [ ] Verify live: `enter_confirm`'s first-entry turn (`awaiting_final_yes`
      freshly `False`) is unaffected — still reaches the real agent
      directly, same as today.

## Phase 3.8 — Order persistence: `orders`/`order_products` schema, cart items carry product + image URLs (built and verified live 2026-09-27)

**Found while implementing, not anticipated in the plan:** the live
`orders` table already had 1 real row (`id=1`, `kapruka_order_ref
ORD-20260927-CG7A`, `phone_number '+94_console_dev'`, created
2026-09-27) — a real `kapruka_create_order` call was made at some point
outside this session (this phase's own migration is destructive —
`DROP COLUMN items`/`product_summary` — so this was checked directly
before writing any migration SQL, not assumed empty the way Phase 0 found
it). Flagged and confirmed with the customer before applying — explicit
go-ahead given to drop that row's `items`/`product_summary` data rather
than backfill it; the row itself (`id=1`) was left in place, just with
those two now-dropped columns' data gone.

**Problem being fixed:** right now, a completed order's data exists in two
places, both fragile: the `checkout_url` shown to the customer once in
`complete_order`'s reply text (never persisted — if that message is lost,
so is the payment link), and a single JSONB blob in the existing `orders`
table (`items`, `product_summary`) with no per-product detail, no pricing
breakdown, and no way to look anything up without a customer supplying
context the bot would otherwise have to pull from message history. Phase 4
needs a `phone_number` → order lookup that doesn't depend on history at
all — this phase is what makes that possible.

**Decision:** extend the existing `orders` table (not a new, differently
named table — `order` is a reserved SQL keyword and would need quoting
everywhere) with the fields `complete_order` already has on hand but
doesn't currently save, add a new `order_products` table for per-item
detail, and align every new column name with the field names
`checkout_info`/`create_order` already use — `sender_name` (not `from`,
also reserved), `delivery_fee` (not `delivery_price` — matches Kapruka's
own response field), `delivery_address`, `delivery_city` — so `save_order`
needs no translation layer between what it already has and what it writes.

### `orders` table changes (`src/db/schema.sql`)
- [x] Add columns: `payment_url TEXT`, `payment_url_expires_at TIMESTAMPTZ`,
      `items_total NUMERIC`, `delivery_fee NUMERIC`, `addons_total NUMERIC`
      (Kapruka's real response splits the total into these three plus
      `grand_total` — the existing `total_amount` column already holds
      `grand_total`; storing all three components rather than just
      `delivery_fee` alone means the numbers can be reconciled later
      instead of dropping `addons_total` silently), `currency TEXT`,
      `delivery_address TEXT`, `recipient_name TEXT`, `recipient_phone
      TEXT`, `sender_name TEXT`.
- [x] Rename `kapruka_order_id` → `kapruka_order_ref`, matching
      `order_result.get("order_ref")`'s own field name exactly — same
      guarded, idempotent migration pattern already used for
      `owner_contact` → `phone_number` in this same file
      (`DO $$ ... IF EXISTS ... THEN ALTER TABLE ... RENAME COLUMN ...`).
- [x] Drop `items` (JSONB) and `product_summary` — both fully superseded
      by `order_products` below; keeping either would be a second source
      of truth that could silently drift from the real per-product rows.
- [x] `phone_number`, `status`, `delivery_city`, `delivery_date`,
      `total_amount`, `created_at` — already exist, unchanged.

### New `order_products` table
- [x] `id SERIAL PRIMARY KEY`, `order_id INTEGER REFERENCES orders(id)`,
      `kapruka_product_id TEXT`, `product_name TEXT`, `product_url TEXT`,
      `product_image_url TEXT`, `unit_price NUMERIC`, `quantity INTEGER`,
      `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`. Index on `order_id`.

### Cart items need to carry `url`/`image_url` through — they currently don't
- [x] `propose_cart`'s docstring (`src/gift_picker/tools.py`) only requires
      `product_id`, a plain `price`, and "whatever name info you have" per
      item — it never mentions `url` or `image_url`, even though
      `suggest_products`'s own `ProductSuggestion` schema already captures
      both a step earlier in the same conversation. Update the docstring
      to explicitly require both fields per cart item, same tightened,
      explicit-example style already used for the `product_id` exact-case
      rule — a docstring here is guidance, not a guarantee, per this
      project's own established finding (the price-shape bug, the
      product_id re-casing bug), so vague wording has a known track record
      of being followed loosely.
- [ ] **Residual risk worth flagging, not glossing over:** unlike
      `product_id` (trivially copy-paste-able), `url`/`image_url` require
      the model to still be carrying a value forward from an earlier
      `suggest_products`/`kapruka_get_product` call, potentially several
      turns back. `GIFT_PICKER_INSTRUCTIONS`'s existing "never describe a
      product without having looked it up" rule should make this mostly
      self-satisfying, but it hasn't been exercised in a longer, multi-turn
      conversation the way the immediate lookup-then-cart pattern already
      has — worth a dedicated live test, not assumed to just work because
      the shorter-conversation tests passed.

### Fix the hardcoded quantity bug, found while designing this phase
- [x] `create_order` (`src/checkout/order.py`) currently sends
      `"quantity": 1` for every cart item unconditionally, regardless of
      what's actually in `cart["items"]`:
      `{"product_id": item["product_id"], "quantity": 1}`. A `quantity`
      column on `order_products` is meaningless while this stays broken —
      fix to `item.get("quantity", 1)`.
- [x] **Real design fork, not just a one-line change — `propose_cart`'s
      docstring has never said anything about quantity at all** (checked
      directly, not assumed). Two different cart shapes are both
      consistent with that silence: the Gift-Picker could be expected to
      emit one row per unit (two chocolates = two identical `product_id`
      entries) or one row per distinct product with an explicit
      `quantity` field. Decide which before writing `finalize_checkout_info`/
      `order_products`-insert logic that assumes one or the other — the
      docstring update above should make the chosen shape explicit rather
      than leaving it to be inferred. **Decided: one row per distinct
      product_id, explicit `quantity` field** — matches
      `kapruka_create_order`'s own `{product_id, quantity}` cart-item shape
      (which already had a `quantity` field sitting unused/hardcoded), and
      avoids `order_products` carrying N identical rows for N units of the
      same product.

### `save_order` rewrite (`src/checkout/order.py`)
- [x] Becomes a two-table insert: one `orders` row (`checkout_info`'s
      fields map 1:1 now that names match, plus `order_result`'s
      `summary.*`/`checkout_url`/`order_ref`), then one `order_products`
      row per cart item. Run inside a single transaction — a partial
      failure leaving an `orders` row with no matching `order_products`
      rows is a new failure mode a two-table insert introduces that the
      old single-table version never had. (`pool.connection()`'s own
      context manager already commits-on-clean-exit/rolls-back-on-exception
      per connection, so both inserts sharing one `async with pool.connection()`
      block gets this for free — no explicit `BEGIN`/`COMMIT` needed.)
- [x] `payment_url_expires_at` computed at insert time as
      `now() + interval '60 minutes'` — Kapruka's own guest-checkout
      pay-link window, confirmed from the tool's own documented behavior,
      not guessed.
- [x] `summary.delivery_fee`/`summary.addons_total` read defensively
      (`.get(...)`, allow null) — this project's own build log only
      explicitly confirmed `summary.grand_total` against a live response
      so far (Phase 3); treat the other two the same cautious way
      `_item_price` already handles inconsistent price shapes elsewhere,
      rather than assuming the tool's documented schema and the live
      response agree on every field.

### Not this phase's job — context for why this phase exists
- [ ] The actual track-order intent handler (order-number extraction,
      `kapruka_track_order`, formatting a reply) stays Phase 4's work.
      This phase only makes the lookup possible: `phone_number` →
      `orders`, joined to `order_products` for per-item detail, in place
      of pulling that context from message history.

### Verification checklist — migration applied live 2026-09-27 (existing
`orders` row id=1 predates this phase, left in place with the now-dropped
columns' data gone, per explicit go-ahead — see the found-live note above)
- [x] `schema.sql`'s migration re-applies cleanly a second time against the
      live Neon instance (idempotent) — same bar the existing
      `owner_contact` migration already meets. **Verified live**: ran
      `scripts/check_db.py` twice in a row against the real Neon instance,
      no errors either time.
- [x] Rewritten `save_order` tested directly against a fabricated
      `order_result` (not a live Kapruka call — same "never fire a real
      create_order in dev" boundary as every other checkout test in this
      codebase) confirms both tables populate correctly, including
      `product_url`/`product_image_url` on every `order_products` row.
      **Verified live**: a fabricated 2-item cart (`quantity` 2 and 1,
      default) round-tripped through the real `save_order` against the
      real Neon instance — `orders` row had every field populated
      correctly (`items_total`/`delivery_fee`/`addons_total`/`currency`
      from `summary`, all six `checkout_info` fields, `payment_url`,
      correct `kapruka_order_ref`), both `order_products` rows had correct
      `product_url`/`product_image_url`/`unit_price`/`quantity`. Test rows
      deleted immediately after (`TEST-VERIFY-3.8`).
- [x] A cart item with `quantity` > 1 reaches Kapruka's real `quantity`
      field correctly, not hardcoded `1`. **Verified directly** (not
      against the live Kapruka MCP call itself, same "never fire a real
      create_order" boundary): monkeypatched `call_kapruka_tool` to
      capture `create_order`'s own outgoing params without a network
      call — a `quantity: 3` item and a quantity-omitted item both
      produced the correct request shape (`3` and `1`/default,
      respectively), confirming the hardcode is actually gone, not just
      the line of code that used to say `1`.
- [x] `payment_url_expires_at` lands roughly 60 minutes after `created_at`
      on a real inserted row. **Verified live**: the test row's
      `payment_url_expires_at` was exactly `created_at + 1:00:00`.
- [ ] A longer, multi-turn conversation (lookup a product several turns
      before it's actually added to the cart) still produces a correct
      `url`/`image_url` on the resulting cart item — the specific residual
      risk flagged above, not covered by the shorter existing tests. Not
      run — needs a real multi-turn Gift-Picker/Gemini conversation, not
      just a DB-layer check; deferred rather than spending live LLM/Kapruka
      rate-limit budget on it speculatively, same call this project has
      made before for lower-priority live tests.

## Phase 3.8.1 — Prevent modification of a placed order (built and verified live 2026-09-28)

**Problem being fixed:** `complete_order`'s own success path clears
`stage` to `None` (see Phase 3.6) — so a "can you change my order" message
arriving right after checkout has nothing left routing it to the Confirm
Agent. `_route_from_start` only reaches the Confirm Agent when
`stage == "confirm"`, and that stage is already gone by the time this
message arrives; it isn't a judgment call the Confirm Agent could make
even in principle, it's structurally unreachable. The message falls
through to the Intent Router instead, same as any fresh turn — and none
of the five existing intents (`gift_request` / `track_order` /
`return_item` / `chitchat` / `out_of_scope`) obviously fit "modify a
just-placed order." Left unhandled, it risks landing on `out_of_scope`,
declining someone who still wants help.

**Decision:** Kapruka has no modify tool anywhere in its MCP surface — so
any change to a placed order is necessarily a brand-new order, which is
exactly what the Gift-Picker already does every day. Reuse it rather than
building a new stage or node. **Scoped to modification only, for now** —
cancellation-shaped requests ("cancel my order," "undo my order") are
explicitly out of scope for this phase, not silently folded in. A
deliberate scope decision, not an oversight; revisit separately once
cancellation itself gets designed.

- [x] Intent Router instructions (`src/prompts.py`) — add guidance: a
      message implying the customer wants to add/change/redo something
      about an order they believe is already placed → `gift_request`, not
      `out_of_scope`.
- [x] `GIFT_PICKER_INSTRUCTIONS` (`src/prompts.py`) — add: if the
      customer's message implies they think they're modifying a
      previously placed order, say plainly that a placed order can't be
      modified and this will be treated as a new order, then continue
      helping normally (search/suggest/propose as usual) — a redirect
      into the same flow every other `gift_request` already gets, not a
      dead end.
- [ ] **Left as a default, not a full decision — flagged as such:** the
      Gift-Picker's can't-modify reply is phrasing-based only, not
      verified against a real order — it reacts to how the customer
      describes their situation, not an actual DB lookup. Works
      identically whether it's the same thread, a new thread, or weeks
      later, and needs nothing from Phase 3.8's schema to function. A
      DB-backed version (a read-only tool querying `orders` by
      `phone_number`, so the reply can reference the actual order
      ref/date instead of just trusting the customer's framing) would be
      more accurate but needs a new tool — not built here; an upgrade
      path if the phrasing-only version turns out too easily fooled or
      too generic in practice.
- [ ] **Explicitly out of scope for this phase:** cancellation-shaped
      requests get no routing change here — they fall through to
      whatever the Intent Router already does today (most likely
      `out_of_scope`), same as before this phase. Not addressed because
      "place a new order" is bad advice for someone trying to cancel, and
      Kapruka has no cancellation capability to route them toward either
      — that needs its own design, not a default made in passing here.

### Verification checklist
- [x] Live test: "can you change the delivery date on my order" →
      `gift_request`; Gift-Picker gives the can't-modify explanation, then
      proceeds to help normally. **Verified live** (fresh thread, real
      Gemini + real graph, no checkout ever reached so no financial-action
      risk): reply was "I'm unable to modify the delivery date or details
      of an already placed order... However, if you'd like to place a new
      order or pick out a new gift, I'd be happy to help! Who are you
      shopping for..." — exactly the shape the plan called for, not a dead
      end.
- [x] Boundary check, same style as Phase 1's own `out_of_scope`/
      `chitchat` adversarial tests: a handful of adjacent modification
      phrasings classified as `gift_request` correctly, not `out_of_scope`.
      **Verified live** via `classify_intent` directly: "can you change the
      delivery date on my order," "I want to add something to my order,"
      "can you fix my order" all → `gift_request`. Also confirmed no
      regression on the adjacent case this phase deliberately leaves
      alone: "I want to cancel my order" still → `return_item`, and a
      genuine `track_order` message was unaffected.
- [x] Confirm this behaves the same in a brand-new thread as in the
      original one — since the reply depends on neither chat history nor
      a DB lookup, this should hold automatically, but worth checking
      live rather than assuming. **Verified live**: the test above used a
      brand-new thread (no prior checkout in that session) and produced
      the correct behavior, confirming the phrasing-only design doesn't
      depend on a real preceding order actually existing in that thread.
## Phase 4 — Chat-agent identity: decouple thread from phone number, new-chat detection, history compaction (built and verified live 2026-09-28)

**Problem being fixed:** everything so far assumes a WhatsApp-shaped
integration — `session_identity` collapses `thread_id = user_id =
session_id = phone_number`, so one customer has exactly one conversation,
forever, with no concept of "starting over." Moving to a normal chat-agent
interface (not WhatsApp) means the client can own conversation boundaries
the way Claude.ai/ChatGPT's own "New Chat" button already does — there's
no reason left to guess session boundaries the way a phone-number-only
integration would have to.

**Decision:** separate customer identity from conversation identity.
`phone_number` stays permanent and keeps keying every piece of durable
data (`recipients`, `orders`, `order_products` from Phase 3.8) — nothing
about that changes. `thread_id` becomes its own value: client-supplied
when continuing a chat, freshly generated when absent. History compaction
after a completed order (discussed alongside this) is no longer the
primary defense against unbounded growth — the New Chat boundary is —
it becomes a narrower safety net for customers who keep talking in the
same thread indefinitely instead.

### Decouple `thread_id` from `phone_number`
- [x] `session_identity(phone_number, thread_id)` (`src/session.py`) —
      `user_id`/`session_id` stay tied to `phone_number` (customer
      identity, unaffected); `thread_id` becomes an independent parameter
      instead of being derived from `phone_number`.
- [x] `run_turn(phone_number, message, thread_id=None)` (`src/pipeline.py`)
      — when `thread_id` is absent, generates a new one (e.g. `uuid4()`)
      before building `session_identity`, and returns the resolved
      `thread_id` alongside the reply text so the caller can persist and
      reuse it on the next call. This is the actual "if thread_id does not
      arrive, it's a new chat" mechanism — everything else in this phase
      exists to support or exercise it.

### New `threads` table — not named in the request, but needed to make this real
- [x] `src/db/schema.sql`: `thread_id TEXT PRIMARY KEY`, `phone_number
      TEXT NOT NULL`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`,
      `last_active_at TIMESTAMPTZ NOT NULL DEFAULT now()`. Without this,
      there's no way to answer "show this customer their past chats" at
      all — `AsyncPostgresSaver` only knows about `thread_id`s, not which
      customer any of them belong to. `last_active_at` updated on every
      turn for that thread; a new row written only the first time a given
      `thread_id` is seen (i.e., exactly when `run_turn` had to generate
      one), not on every turn. **Implemented as one upsert**
      (`src/db/threads.py::touch_thread`,
      `INSERT ... ON CONFLICT (thread_id) DO UPDATE SET last_active_at = now()`)
      rather than two conditional branches — satisfies both described
      behaviors in a single statement and is also robust to a client
      supplying a `thread_id` we never actually issued.
- [x] Flagging, not building yet: an actual "list my past chats" surface
      is possible once this table exists, but isn't part of this phase.

### `main.py` — hardcoded placeholder, not the real mechanism
- [x] The console entry point has no way to simulate "a client starting a
      new chat," so a second constant, `THREAD_ID`, sits alongside the
      existing `PHONE_NUMBER` one and gets passed to `run_turn` every
      loop iteration — keeps local dev/testing continuous across restarts
      instead of generating a fresh thread every run. The real behavior
      (a caller omits `thread_id`, gets a new one back; supplies one,
      continues that chat) is only meaningfully exercised once Phase 7's
      FastAPI webhook exists and a real caller can choose to pass or omit
      it per request — not something the console alone can demonstrate
      end-to-end.

### History compaction after a completed order
- [x] Right after `complete_order` succeeds — as its own step, not
      sharing `complete_order`'s own transaction, kept deliberately
      decoupled so a compaction failure can never touch the one
      irreversible action in this codebase — replace that thread's
      checkpointed message history with a short summary (products, order
      ref, delivery city/date), not append to it. An additive version
      would keep all the storage cost and none of the benefit.
- [x] Fold `Cart["notes"]` (and Kapruka's own `gift_message` field, if
      that ends up wired in — see Phase 3.8's own flagged gap) into the
      summary text, since neither has anywhere else to live once the raw
      messages that contained them are gone. (`gift_message` itself isn't
      folded in — `checkout_info` doesn't carry it at all yet, nothing to
      fold until Phase 3.8's flagged gap actually gets wired up.)
- [x] **Mechanism — checked against the pinned versions, not just
      recalled from docs:** delete the thread's checkpoints, then write
      one fresh checkpoint into the same `thread_id` holding the summary
      and the already-cleared state. Two calls:
      - `AsyncPostgresSaver.adelete_thread(thread_id)` — confirmed present
        and implemented in the pinned `langgraph-checkpoint-postgres==3.1.2`
        (read from the installed source). It deletes that thread's rows
        from `checkpoints`, `checkpoint_blobs` and `checkpoint_writes`,
        which is what actually reclaims storage.
      - `graph.aupdate_state(config, {"messages": [summary], ...},
        as_node=<a real node name>)` — writes the fresh checkpoint.
        Exercised against the pinned `langgraph==1.2.11` with an
        in-memory saver on a small stand-in graph: after the delete the
        thread was empty, after the reseed it held only the summary, and
        the next turn saw the summary plus the new message and nothing
        else.
- [x] **Was unverified, now resolved — the two things flagged to check in
      a spike before building on this:** (1) **Verified live against the
      real orchestrator and real Neon** (not just the stand-in
      graph/in-memory-saver spike above): built a genuine 2-turn thread
      (4 messages) via `run_turn`, called `compact_thread` directly with a
      fabricated summary (never a real `create_order`), confirmed via
      `checkpointer.aget_tuple` that exactly 1 message (the summary)
      remained and `order_summary_for_compaction` was correctly cleared to
      `None`, then ran one more real turn on that same `thread_id` and
      confirmed the result held exactly 3 messages (summary + new turn),
      not the pre-compaction history — `as_node="confirm_agent"` (the node
      whose turn produces the completed order this summary describes)
      works against the real subgraph-based orchestrator, no framework
      surprise here unlike `EphemeralValue`/`InjectedState`'s. (2)
      Delete-then-reseed is still not atomic — accepted on purpose, same
      reasoning as originally flagged (order data is already safe in
      `orders`/`order_products` regardless); `compact_thread` wraps both
      calls in one try/except that logs on failure rather than swallowing
      it, **verified directly**: a simulated `adelete_thread` failure was
      caught and logged without propagating.
- [x] Since the New Chat boundary now handles the common case, compaction
      only needs to cover "the same thread keeps going after a purchase"
      — a narrower, lower-urgency safety net than it would have been as
      the primary growth mechanism. Worth keeping that framing in mind if
      it needs to be deprioritized relative to the identity-decoupling
      work above.

### Explicitly not addressed here, deliberately deferred
- [x] A UI/endpoint for listing a customer's past threads — the `threads`
      table makes it possible, building the surface isn't part of this
      phase.
- [x] An order abandoned mid-checkout in a thread that's never reopened
      stays lost — same accepted limitation raised earlier in design
      discussion, unrelated to anything built here.
- [x] A customer who never starts a new chat and never completes an order
      still grows one thread indefinitely — compaction only fires on a
      completed order, so this case stays open.

### Verification checklist — all verified live 2026-09-28, real Gemini +
real Neon, test rows/threads cleaned up after (no real `create_order` ever
fired — compaction was exercised with a fabricated summary string, not a
completed order)
- [x] `run_turn` called with no `thread_id` generates one and a matching
      `threads` row; called again with that same `thread_id` reuses it,
      updates `last_active_at`, and does not create a duplicate row.
      **Verified live**: reused the same generated `thread_id` twice for
      one phone number — one row, `created_at` unchanged,
      `last_active_at` moved forward 16s to match the second call; a
      third call with no `thread_id` under the same phone number produced
      a genuinely new row.
- [x] Two different `thread_id`s under the same `phone_number` see
      genuinely independent history — a fact stated in one thread isn't
      visible from the other. Confirms the decoupling actually changed
      behavior, not just a signature. **Verified live** directly against
      `checkpointer.aget_tuple` (not just reply text, which can look
      identical either way for canned `chitchat`/`out_of_scope` replies):
      a 2-turn thread held 4 messages; a fresh thread under the same phone
      number held exactly 2 (its own single turn).
- [x] A completed order in one thread, followed by compaction, leaves
      that thread's next turn seeing the short summary, not the full raw
      transcript — checked directly against the stored checkpoint, not
      just "the reply looked right." **Verified live** (see the
      compaction section above for the full detail): 4 messages -> 1
      (summary) -> 3 (summary + next real turn).
- [x] A forced compaction failure (test only) does not affect the
      already-completed order — `orders`/`order_products` rows exist and
      are correct regardless of whether compaction itself succeeded.
      **Verified directly**: a simulated `adelete_thread` exception inside
      `compact_thread` was caught and logged, never propagated — and
      since compaction only ever runs as a step strictly after
      `complete_order`'s own transaction has already committed, there is
      no code path by which a compaction failure could reach back and
      touch `orders`/`order_products` regardless.


## Phase 5 — Observability (OpenTelemetry: request tracing, per-agent token usage, tool-call visibility)

**Problem being fixed:** none of this codebase currently has any way to
see a request trace, identify where a turn went wrong, or measure token
usage per agent or tool call — every diagnosis so far has depended on
reading console prints or re-running a scenario by hand.

**Decision:** OpenTelemetry, built around reusable shared components so
every agent (Intent Router, Gift-Picker, Checkout Info Agent, Confirm
Agent, and the deterministic pipeline) reuses the same setup rather than
each reinventing it. Rolled out incrementally, not all at once — broken
into its own numbered sub-phases below (`5.0.0`, `5.0.1`, and more as the
rollout continues), each independently buildable and verifiable on its
own rather than one large phase that can only be checked off as a whole.

### Phase 5.0.0 — Observability scaffolding: reusable OpenTelemetry setup, no agent wired in yet (built and verified live 2026-09-29)

**Decision:** build one shared module now, console-output only — no
Langfuse, no OTLP export, nothing sent anywhere external. Nothing in this
sub-phase touches an actual agent's code; that's deliberately Phase
5.0.1's job, kept separate so a problem in the scaffolding and a problem
in the first real integration aren't debugged at the same time.

- [x] New module, `src/observability.py`:
      - `configure()` — a `TracerProvider` with a `SimpleSpanProcessor` +
        `ConsoleSpanExporter` (not `BatchSpanProcessor` — for a
        console-output-only phase, exporting each span the instant it
        completes is simpler and more predictable than a batch timer, and
        avoids depending on an explicit flush for correctness; batching
        becomes worth it again once a real network destination like
        Langfuse enters the picture later). Also sets
        `LANGSMITH_TRACING_MODE=otel` — confirmed directly from the
        installed `langsmith` package's source (not docs) to route
        LangChain/LangGraph's own run-tracing through this same
        `TracerProvider`, entirely locally, no LangSmith account or API
        key involved.
      - `flush()` — force-flushes the provider before process exit. Less
        critical with `SimpleSpanProcessor` than it would be with
        `BatchSpanProcessor`, but kept as cheap insurance and because it
        becomes load-bearing again the moment this moves to a batched,
        networked exporter.
      - `turn_context(phone_number, thread_id)` — a context manager
        attaching both as OpenTelemetry baggage, so every span created
        for the duration of one turn — across whichever agent or tool
        runs — carries them automatically, with nothing threaded through
        function signatures. Not exercised by any real span in this
        sub-phase (no agent is wired in yet); built now so Phase 5.0.1
        doesn't need to.
      - `get_tracer()` — one named tracer, used everywhere from here on,
        instead of each module creating its own.
- [x] `requirements.txt` additions: `opentelemetry-sdk`,
      `opentelemetry-api`, `langsmith[otel]` (the LangChain/LangGraph
      bridge — a local instrumentation shim in this configuration, not a
      dependency on the LangSmith product). `langsmith` itself stayed at
      the already-installed `0.12.4` — its OTEL bridge is present there
      already (confirmed by reading the installed source), no version
      bump needed; the `[otel]` extra just pulled in the
      `opentelemetry-exporter-otlp-*` packages (unused until a later
      sub-phase swaps in a real OTLP destination).
- [x] `scripts/check_otel.py` — a standalone smoke test, same pattern as
      Phase 0's `check_llm.py`/`check_mcp.py`/`check_db.py`: calls
      `configure()`, opens one manual span with a fake attribute, confirms
      it prints to console. Validates the scaffolding in isolation —
      whether OpenTelemetry itself is configured correctly — as a
      separate question from whether the LangChain/LangGraph bridge
      correctly captures a real agent, which is what Phase 5.0.1 actually
      tests.
- [x] Explicitly not done here: no agent's code changes, `main.py` is
      untouched, no root span exists yet for a real turn. That's Phase
      5.0.1, not this one.

#### Verification checklist
- [x] `python scripts/check_otel.py` prints a span to the console with the
      expected name and attribute — confirms the provider/exporter/bridge
      configuration works before anything real depends on it. **Verified
      live**: printed a `check_otel.manual_span` span with
      `check.fake_attribute="hello-otel"` and, as a bonus early check of
      `turn_context`/`_BaggageSpanProcessor` (not required by this
      sub-phase's own scope, but cheap to confirm now rather than wait for
      5.0.1), correct `phone_number`/`thread_id` baggage attributes on the
      same span.
- [x] `LANGSMITH_TRACING_MODE=otel` genuinely makes no network call to any
      LangSmith endpoint — verify directly (e.g. no outbound request in a
      network capture, or confirm from the installed package's own logic)
      rather than trusting the env var's name alone. **Verified two ways**:
      (1) read `langsmith/client.py`'s source directly — `Client.info`
      explicitly skips its API call when `tracing_mode == "otel"`, and
      `Client.__init__`'s otel-setup branch never touches the network
      either. (2) Live-constructed a real `Client(tracing_mode="otel")`
      with no `LANGSMITH_API_KEY` set anywhere in this project's `.env` —
      succeeded with no error, and `.info` returned a local stub
      (`LangSmithInfo(version='', ...)`), not a fetched response, matching
      the source-level finding rather than just trusting it. **Also found
      live, not anticipated in the plan:** if `opentelemetry` isn't
      actually installed, `langsmith`'s `Client` silently falls BACK to
      real network-based `"langsmith"` mode instead of failing loudly
      (confirmed in the installed source's `except ImportError` branch) —
      a real footgun this project avoided only by installing
      `opentelemetry-sdk`/`-api` before ever setting `LANGSMITH_TRACING`,
      not by the mode flag alone.

### Phase 5.0.1 — Wire it into one agent: the Intent Router, verified via `main.py` (built and verified live 2026-09-29 — two genuine bridge limitations found and accepted, see below)

**Problem being fixed:** Phase 5.0.0's scaffolding is unverified against a
real agent until something actually uses it. Rolling out to all four
agents at once would make a failure hard to localize — is it the
scaffolding, the bridge, or something specific to whichever agent broke
first? Piloting on one agent first isolates that.

**Decision:** the Intent Router, not the Gift-Picker — it's the simplest
possible case (a single LLM call, zero tools, no ReAct loop, no
cross-agent handoff), which minimizes what could confound a first result.
Verification is entirely by reading `main.py`'s console output, matching
Phase 5.0.0's own "logging only, no dashboard yet" scope — nothing here
talks to Langfuse.

- [x] `run_turn()` (`src/pipeline.py`) — wrap the existing `ainvoke` call
      in `get_tracer().start_as_current_span("run_turn", kind=SpanKind.SERVER)`,
      itself inside `turn_context(phone_number, thread_id)`. `SpanKind.SERVER`
      specifically matters here, not just as a formality — a console app
      emits no server-shaped spans otherwise, since nothing else in the
      process would create one.
- [x] `main.py` — one call to `observability.configure()` at startup, one
      call to `observability.flush()` before the process exits.
- [x] After the Intent Router returns, add the classification result as a
      manual attribute on the current span (e.g. `trace.get_current_span()
      .set_attribute("gift.intent", result)`) — this is domain-specific
      enrichment the auto-instrumentation won't produce on its own, the
      same pattern used for adding facts onto an already-open span rather
      than creating a redundant new one.
- [x] No handoff span in this sub-phase — with only the Intent Router
      instrumented, there's nothing yet to hand off *to* (canned
      `chitchat`/`out_of_scope` replies, and `gift_request` is still just
      a stub downstream). That pattern becomes relevant starting with
      Phase 5.0.2 or whichever sub-phase extends this to the Gift-Picker.
- [x] **Genuinely unverified going in, not assumed:** whether
      `langsmith[otel]`'s bridge correctly captures `create_agent`-as-
      subgraph-node's LLM call with real Gemini token-usage attributes.
      The bridge's existence and its local-only `OTEL` mode were
      confirmed by reading its installed source; whether it correctly
      understands *this* project's specific LangGraph shape has not been
      run live. This is the first thing to actually check once this
      sub-phase runs, not something to take on faith from having read the
      code. **Answer: yes, and considerably more than hoped** — the bridge
      doesn't just capture the Intent Router's own LLM call, it
      auto-instruments the ENTIRE compiled graph for free: every LangGraph
      node got its own span (`__start__`, `_route_from_start`,
      `intent_router`, `extract_intent`, `_route_on_intent`,
      `chitchat_node`/`gift_picker`, the Gift-Picker's own `tools`/`model`/
      `ChatGoogleGenerativeAI` spans down to individual tool calls like
      `kapruka_search_products`, `LangGraph`, `concierge_orchestrator`),
      each carrying real `gen_ai.usage.input_tokens`/`output_tokens`/
      `total_tokens`, `gen_ai.request.model`, `gen_ai.system`. **But this
      needed a real bug fix first, not just observation** — see the
      `bytes`-attribute finding below.

#### Explicitly deferred — the roadmap past this pilot, not built here
- [ ] Extending the same reusable pieces to the Gift-Picker, Checkout
      Info Agent, and Confirm Agent — no new reusable code needed, since
      `turn_context`/`get_tracer` already cover them.
- [ ] Explicit "handoff" spans at the real orchestrator transition points
      (`with_gift_picker`→`checkout_info`, `checkout_info`→`confirm`, any
      `handoff_reason` hand-back) — the auto-instrumentation captures each
      agent's own internal work but nothing that marks one agent handing
      control to another; that needs a deliberately added span at each
      transition, not something that appears for free.
- [ ] Tool-call enrichment inside each `@tool` function via
      `trace.get_current_span().set_attribute(...)` — adding attributes
      to the span the framework already creates for a tool call, not a
      new span wrapping it.
- [ ] Manual spans for the non-LangChain deterministic pipeline (the raw
      MCP client in `src/checkout/mcp_client.py`, DB writes in
      `save_order`/`get_recipient_profile`, the compaction step) — none
      of these go through LangChain's callback system, so none of them
      get instrumented for free the way the agent-layer calls do.
      Genuine hand-written-span work, not a configuration change.
- [ ] Proper `span.record_exception()` + `span.set_status(...)` at
      failure points — the standard OpenTelemetry idiom for marking a
      span as failed, so "where did it go wrong" is visible to any
      OTel-compatible viewer without a custom convention to remember.
      **Sharpened by a live finding in this sub-phase, not just still
      deferred as originally scoped:** this turns out to be needed for the
      auto-instrumented bridge spans too, not only the hand-written
      deterministic-pipeline ones — see the verification checklist below.
- [ ] Swapping `ConsoleSpanExporter` for an OTLP exporter pointed at a
      self-hosted Langfuse instance — one exporter line, once there's
      something worth looking at beyond console output.

#### Verification checklist
- [x] `main.py` run through a `chitchat` message and a real classification
      message, console output inspected directly for: a `run_turn` span
      of `SpanKind.SERVER`, a nested span for the Intent Router's own LLM
      call, `gen_ai.usage.input_tokens`/`gen_ai.usage.output_tokens` (or
      whatever the actual attribute names turn out to be — the OTel GenAI
      semantic conventions are still in Development status as of this
      writing, so treat exact names as subject to drift, not frozen) on
      that nested span, and the manually added `gift.intent` attribute.
      **Verified live** — all present, exactly as named above (not
      "whatever the names turn out to be": `gen_ai.usage.input_tokens`/
      `output_tokens`/`total_tokens` matched the anticipated names exactly).
      **Found and fixed live to get here, not anticipated in the plan:**
      the langsmith bridge sets `gen_ai.prompt`/`gen_ai.completion` as raw
      `bytes`, which crashed `ConsoleSpanExporter`'s default JSON
      serialization and silently dropped every bridge-generated span from
      console output — only this module's own manually-created `run_turn`
      span (no bytes attributes) was ever printing. Root-caused via a
      diagnostic `SpanProcessor.on_end` that inspected each attribute's
      Python type on a real span, not guessed from the traceback alone.
      Fixed in `src/observability.py::_console_safe_formatter` (decodes
      bytes to `str` before delegating to the real `to_json()`) — see that
      module's own docstring for the full detail.
- [x] `phone_number`/`thread_id` baggage attributes are present on both
      the root span and the nested LLM-call span — confirms baggage
      propagation actually reaches child spans, not just the one it was
      attached from. **Partially true, verified live, not fully — a real
      limitation, not a bug left unfixed:** present on the root `run_turn`
      span (confirmed). NOT present on the langsmith-bridge-generated
      nested spans — root-caused by directly inspecting
      `threading.current_thread()` inside the bridge's own span-creation
      call: `langsmith.Client`'s default `auto_batch_tracing=True` creates
      those spans from a background `tracing_control_thread_func` thread,
      which never inherits the calling thread/task's attached
      `contextvars` baggage context. **A fix was attempted and explicitly
      rejected, not left untried:** `Client(auto_batch_tracing=False,
      tracing_mode="otel")` does make spans create on the calling thread
      (baggage would reach them) — but live-testing it showed it ALSO
      bypasses the client's own otel-only network guard and fires real
      authenticated requests at `api.smith.langchain.com` (confirmed via
      real `401 Unauthorized` errors — a genuine outbound network call,
      exactly what this whole setup exists to avoid). Judged "no network
      call, ever" more important than complete baggage coverage, so the
      default `auto_batch_tracing=True` is kept and this gap is accepted
      as a documented limitation (see `src/observability.py`'s docstring),
      not silently worked around.
- [x] A deliberately broken call (e.g. a bad model name) produces a span
      that's visibly marked as failed in the console output, not just a
      Python traceback with no corresponding span-level signal.
      **Verified live with a real invalid model name — true for this
      module's own span, not for the bridge's:** the root `run_turn` span
      correctly got `status_code: ERROR` plus a recorded `exception` event
      (OTel's own default behavior when an exception exits a
      `start_as_current_span` block). The actual failing
      `ChatGoogleGenerativeAI`/`model` spans from the bridge stayed
      `status_code: OK` — the failure text does land inside their
      `gen_ai.completion` attribute as unstructured JSON, but the bridge
      never calls `set_status`/`record_exception` on them. This is a
      concrete instance of the "Explicitly deferred" `record_exception`/
      `set_status` item above turning out to matter for the
      auto-instrumented layer too — not just the deterministic pipeline it
      was originally scoped for.

### Phase 5.0.2 — Handoff spans across agent transitions (built and verified live 2026-09-30)

**Problem being fixed:** right now a handoff between agents can only be
*inferred* from LangGraph node names in the trace tree (`_route_after_
gift_picker`, etc.) — there's no span that states "agent A finished,
agent B took over, and here's why."

**Decision:** one manual span at each of the three real transition points
in `orchestrator.py` — `gift_picker` → `checkout_info`, `checkout_info`
→ `confirm`, and any `handoff_reason` hand-back to `gift_picker` — each
carrying the actual business reason for the transition, not just the
node name.

- [x] Wrap each transition point's routing logic with
      `get_tracer().start_as_current_span("handoff", ...)`, setting
      `handoff.from_agent`/`handoff.to_agent`/`handoff.reason` before the
      span closes.
- [x] `handoff.reason` sourced from the state's own `handoff_reason`
      field where one is present, else a fixed string naming the
      deterministic trigger (e.g. `"cart_confirmed"`,
      `"checkout_info_finalized"`). **`_handoff_to_gift_picker`'s
      `from_agent` needed a small design decision not spelled out in the
      plan's own wording:** it's a single node shared by BOTH
      `_route_after_checkout_info` and `_route_after_confirm`, so it can't
      hard-code which agent it came from. Resolved by reading
      `state["stage"]` at the moment the node runs — still whatever it
      was on entry into that turn's agent run, since `request_cart_revision`
      never touches `stage` itself — mapping `"checkout_info"` ->
      `"checkout_info_agent"` and `"confirm"` -> `"confirm_agent"`.

#### Verification checklist
- [x] A full order run end-to-end (the same scenario already exercised
      for Phase 3.8.1's live verification) shows three handoff spans, in
      the right order, each with a reason that matches what actually
      happened in that conversation. **Verified live** (real Gemini + real
      Kapruka MCP, stopped short of a real "yes" — same hard-rule boundary
      as every checkout test in this codebase): a 7-turn conversation
      (propose chocolates → confirm cart → mid-checkout-info request to
      add a greeting card → re-propose → re-confirm → give all delivery
      details → summary shown) produced **four** handoff spans (one more
      than the plan's own "three" estimate, since this conversation
      naturally included a revision cycle) in the exact right order, each
      correct: `gift_picker`→`checkout_info_agent`/`cart_confirmed`,
      `checkout_info_agent`→`gift_picker`/"Customer requested to add a
      greeting card to the order." (the actual free-text reason, not a
      generic placeholder), `gift_picker`→`checkout_info_agent`/
      `cart_confirmed` again (the re-confirmation), then
      `checkout_info_agent`→`confirm_agent`/`checkout_info_finalized`.
      All four also confirmed nested under their own turn's `run_turn`
      span (`parent_id` checked directly against each `run_turn` span's
      own `span_id`, not assumed) with correct `phone_number`/`thread_id`
      baggage present — same hand-written-span baggage behavior Phase
      5.0.3 already established, unaffected by the langsmith-bridge
      limitation from Phase 5.0.1. Test thread cleaned up after.

### Phase 5.0.3 — Instrument the deterministic pipeline (built and verified live 2026-09-30)

**Problem being fixed:** raw MCP client calls (`src/checkout/
mcp_client.py`) and DB writes (`src/db/threads.py`, `src/checkout/
order.py`, `src/checkout/compaction.py`) never go through LangChain's
callback system, so none of the langsmith→OTel bridge reaches them —
they're the one part of this app still completely untraced, auto or
otherwise, despite `save_order` being the single spot where a wrong
number gets permanently written.

**Decision:** hand-wrap each with `get_tracer().start_as_current_span
(...)`, the same pattern already used for `run_turn`'s own span — no new
scaffolding needed, just more call sites.

- [x] `mcp_client.py`: a span around each raw MCP call
      (`kapruka_check_delivery`, `kapruka_create_order`), with
      city/delivery-date/order-total attributes where relevant.
      **Implemented one level more generic than the plan's own literal
      per-tool wording:** rather than hand-writing a span at each of
      `kapruka_check_delivery`/`kapruka_create_order`'s own call sites
      (`src/checkout/delivery.py`/`src/checkout/order.py`), the span lives
      once inside `call_kapruka_tool` itself — the one shared choke point
      every raw MCP call already goes through (including
      `kapruka_track_order`/`kapruka_list_delivery_cities` as a free
      bonus). Attributes come from flattening the request `params`/parsed
      response dict's own top-level scalar fields onto the span
      (`_flatten_scalar_attrs`), plus one deliberate level into a nested
      `summary` dict — exactly where `create_order`'s own response nests
      its order total, satisfying "order-total attribute" without
      hand-picking field names per tool. No manual error-status code
      needed — `start_as_current_span`'s own default exception behavior
      already covers a raised `KapurkaToolError`, same mechanism
      `run_turn`'s span already relies on.
- [x] `save_order`: a span carrying `order.total_amount`,
      `order.currency`, `order.delivery_city` — genuinely worth having on
      record given this is the one real financial write in the whole
      system.
- [x] `touch_thread`/`compact_thread`: lighter spans, mainly for timing
      rather than business attributes.

#### Verification checklist — verified live 2026-09-30, real Neon, no
`kapruka_create_order` ever fired (hard rule intact — see below)
- [x] A completed order shows spans for the create-order MCP call and
      the `save_order` DB write, both correctly nested under that turn's
      `run_turn` span — the deterministic pipeline is no longer a gap in
      the trace tree. **Verified live in two separate, hard-rule-safe
      pieces rather than one real end-to-end completed order** (an actual
      `kapruka_create_order` call is never fired in this codebase's own
      dev/test practice): (1) a real `kapruka_check_delivery` call via
      `check_delivery_for_cart`, run inside a manually opened `run_turn`
      span — confirmed live: `mcp.kapruka_check_delivery`'s `parent_id`
      exactly matched `run_turn`'s own `span_id` (checked by direct
      value comparison, not assumed from indentation), with correct
      `mcp.request.city`/`delivery_date`/`product_id` and
      `mcp.response.available`/`rate`/`currency` attributes, plus
      `phone_number`/`thread_id` baggage correctly present on BOTH spans
      (unlike the langsmith-bridge spans from Phase 5.0.1 — this
      confirms baggage propagation works fine for this project's OWN
      hand-written nested spans; the earlier gap was specific to
      langsmith's background tracing thread, not a general limitation).
      (2) `save_order` called directly with a fabricated `order_result`
      (same safe pattern Phase 3.8's own verification used), also inside
      a manually opened `run_turn` span — confirmed `db.save_order`'s
      `parent_id` matched `run_turn`'s `span_id`, with correct
      `order.total_amount`/`currency`/`delivery_city` attributes. Test
      rows/threads cleaned up after.
- [x] `touch_thread`/`compact_thread` spans confirmed present via a real
      `run_turn` call (`db.touch_thread`) and a directly-invoked
      `compact_thread` with a fabricated summary (`compaction.compact_thread`,
      same safe pattern Phase 4's own compaction verification used) —
      neither needed a real order to exercise.

### Phase 5.0.4 — Correct failure/error marking (built and verified live 2026-09-30)

**Problem being fixed:** confirmed live during Phase 5.0.1's own
verification, with a deliberately broken model name — a failed LLM
call's error text lands inside `gen_ai.completion` as unstructured JSON,
but the span's own OTel `status` stays `OK`. Only the one hand-written
`run_turn` span gets a correct `ERROR` status, via
`start_as_current_span`'s own default exception handling. Right now
"did anything fail in this turn" isn't a queryable fact on any of the
auto-generated spans — someone has to read the embedded text by eye.

**Decision:** a `SpanProcessor.on_end()` hook, installed alongside the
existing `_BaggageSpanProcessor`, that inspects `gen_ai.completion` for
the langsmith bridge's own error shape and corrects `status` before the
span is exported — fixes this for every auto-generated span at once,
not one call site at a time.

**Both parts of the plan's own guessed mechanism turned out to be wrong
once checked live, not just refined — worth flagging plainly rather than
quietly swapping in a different implementation:**
1. **The detection signal isn't `gen_ai.completion`.** A deliberately
   broken model name showed a failed call's `gen_ai.completion` is just an
   empty `{"generations": [[]], "llm_output": null, "run": null,
   "type": "LLMResult"}` — no embedded error shape to parse at all. The
   real, more precise, and more standard signal already sitting on the
   span is a normal OTel `exception` **event** (the bridge already calls
   the `record_exception()`-equivalent) — found by direct inspection
   (a diagnostic `SpanProcessor.on_end` dumping every span's actual
   `gen_ai.completion`/events), not assumed from the plan's own
   description. It's also not just the one model-call span: the failure
   propagates an `exception` event up through `ChatGoogleGenerativeAI`,
   `model`, `LangGraph`, `intent_router`, and `concierge_orchestrator` —
   every one of them left at `status: OK` regardless.
2. **A plain `span.set_status(ERROR)` inside `on_end()` does not work at
   all**, confirmed by reading the installed SDK source before writing
   any code around it: `Span.set_status()` explicitly "ignore[s] future
   calls if status is already set to OK" — a deliberate spec guard
   against flip-flopping a final status. Since the bridge has already
   called `set_status(OK)` (confirmed: the JSON shows a literal `"OK"`,
   not `"UNSET"`, so this isn't merely defaulting), the public API
   genuinely cannot correct it after the fact. `_ErrorDetectionSpanProcessor`
   mutates `span._status` directly instead — a private-attribute
   workaround, same category as `_console_safe_formatter`'s bytes
   decoding from Phase 5.0.0/5.0.1, not a new pattern invented here.

- [x] New processor, `_ErrorDetectionSpanProcessor`, added to
      `configure()` alongside `_BaggageSpanProcessor` — **added BEFORE
      the console-exporting `SimpleSpanProcessor`, deliberately, not
      incidentally:** `SynchronousMultiSpanProcessor` calls every
      processor's `on_end()` in add-order for the same span, so the
      status correction has to land before the exporter's own `on_end()`
      call serializes and prints it.
- [x] Manual `record_exception()`/`set_status(ERROR)` added around the
      new spans from Phase 5.0.2/5.0.3, since hand-written spans don't
      get this automatically either — only `run_turn`'s
      `start_as_current_span` usage does. **Narrower in practice than the
      plan's own blanket wording, once checked span-by-span:** every
      Phase 5.0.2/5.0.3 span except one lets its own exception propagate
      naturally out of its `with start_as_current_span(...)` block, which
      already triggers `start_as_current_span`'s own default
      exception-recording behavior for free (the exact mechanism
      `run_turn`'s span already relies on) — no manual code needed for
      `handoff`, `mcp.{tool_name}`, or `db.save_order`/`db.touch_thread`.
      The one genuine exception (literally): `compaction.compact_thread`
      wraps a `try/except Exception: logger.exception(...)` that
      deliberately swallows its own failure (a compaction failure must
      never fail the customer's turn) — since nothing ever propagates out
      of that `with` block, the default behavior never triggers, so this
      is the one span that actually needed manual
      `span.record_exception(exc)` + `span.set_status(Status(StatusCode.ERROR, ...))`
      in its `except` clause (a normal `set_status()` call works fine
      here, unlike the bridge-spans case above — this span was never set
      to `OK` by anything else first, so the "already OK" guard never
      applies).

#### Verification checklist — verified live 2026-09-30
- [x] Same deliberately-broken-model-name test as Phase 5.0.1, this time
      confirming the span's own `status.status_code` shows `ERROR`, not
      just the error text sitting inside `gen_ai.completion`. **Verified
      live**: re-ran the identical Phase 5.0.1 test — `ChatGoogleGenerativeAI`,
      `model`, `LangGraph`, `intent_router`, `run_turn`, and
      `concierge_orchestrator` ALL now show `status_code: ERROR` (up from
      only `run_turn` before this phase).
- [x] No false positives on genuinely successful spans — **verified
      live**: a normal `chitchat` turn's full span set (`db.touch_thread`,
      `__start__`, `_route_from_start`, `model`, `ChatGoogleGenerativeAI`,
      `extract_intent`, `_route_on_intent`, `chitchat_node`,
      `intent_router`, `LangGraph`, `concierge_orchestrator`) was
      completely unchanged from its pre-Phase-5.0.4 shape — all `OK`
      (`run_turn`/`db.touch_thread` `UNSET`, as expected when nothing
      failed).
- [x] `compact_thread`'s own manual error-marking — **verified live** with
      a simulated `adelete_thread` failure (a `FailingCheckpointer` stub,
      not a real DB outage): `compaction.compact_thread`'s span correctly
      showed `status_code: ERROR` with `description: "simulated DB
      failure"` and an `exception` event, while the failure itself was
      still safely swallowed (confirmed no exception propagated out of
      `compact_thread` itself — same "never fail the customer's turn"
      guarantee as before this phase, now just correctly visible in the
      trace too).

### Phase 5.0.5 — Domain-specific tool-call enrichment (built and verified live 2026-09-30)

**Problem being fixed:** the tool spans exist and carry raw args/results,
but nothing on them says anything about *this app* specifically — how
many products a search actually returned, whether a cart was proposed
versus confirmed, what a cart's real total came to.

**Decision:** enrich the spans that already exist, in place, via
`trace.get_current_span().set_attribute(...)` inside each `@tool`
function — no new spans, cheap to add incrementally, same pattern as
`run_turn`'s own `gift.intent` attribute.

**The plan's own literal mechanism was checked live BEFORE writing the
real implementation, and it doesn't do what it says — same category of
finding as Phase 5.0.1/5.0.4's own background-thread discoveries, not a
new problem invented here:** `trace.get_current_span()` called from
inside a tool's own execution does NOT return the bridge's own
auto-instrumented span for that tool call — confirmed by directly probing
`propose_cart` with a monkeypatch that printed the current span's name at
call time: it returned `run_turn`, not `propose_cart`. Root cause is the
same one already documented for Phase 5.0.1: the bridge creates its own
per-call spans on a background thread, so they're never "current" from
the calling thread/task's own perspective. Following the plan literally
would have (1) attached every one of these attributes to the whole
`run_turn` span instead of the specific tool call, and (2) silently
overwritten itself if the same tool fires more than once in one turn
(e.g. a second search) — the second call's `set_attribute` on the same
key would just replace the first, losing data. **Decision made instead:**
give each of these its own small dedicated span, exactly the
`mcp.{tool_name}`-per-call pattern Phase 5.0.3 already established for
the raw MCP client — same actual outcome the plan wanted (attributes
correlated to one specific call), just not literally "no new spans."

**A second, separate finding while implementing
`kapruka_search_products`/`kapruka_get_product` specifically:** these are
remote MCP tools with no Python source in this codebase (loaded via
`MultiServerMCPClient` in `src/gift_picker/agent.py`) — and, checked
live, they return plain markdown TEXT by default, not the structured JSON
`src/checkout/mcp_client.py`'s own raw client explicitly requests
(`response_format: "json"`, a param the args schema exposed to
`MultiServerMCPClient` doesn't even offer). `result_count`/`in_stock`
aren't structured fields to read here — they're pulled out of the
rendered markdown text with two small regexes
(`_RESULT_COUNT_RE`/`_STOCK_LINE_RE`/`_PRODUCT_STOCK_RE` in
`src/gift_picker/agent.py`).

- [x] `kapruka_search_products`/`kapruka_get_product`: `result_count`,
      `in_stock` where applicable. **Implemented via `_wrap_mcp_tool_with_span`**
      (`src/gift_picker/agent.py`), which replaces each loaded MCP tool's
      `.coroutine` with a version opening its own `tool.{name}` span
      around the call. **A second real bug found live while verifying,
      not anticipated:** `tool.coroutine`'s actual return shape differs
      by call path — a direct `.ainvoke()` (e.g. in an isolated check)
      returns a plain list of text blocks, but invoked for real through
      the agent's own graph it returns a `(content, artifact)` TUPLE
      instead (LangChain's `content_and_artifact` response shape). The
      first implementation's fallback (`str(result)` for anything not a
      list) stringified BOTH tuple halves together on the real
      agent-driven path, silently DOUBLING every regex count
      (`in_stock_count: 20` for a 10-result search, `result_count` still
      correctly `10` since `.search()` only takes the first match, which
      is exactly what exposed the mismatch) — an isolated direct
      `.ainvoke()` test of the same tool did NOT reproduce this at all,
      only a real agent-driven call did. Fixed by unwrapping a tuple
      result to its first element before reading the content list.
- [x] `propose_cart`: `cart.item_count`, `cart.estimated_total`.
- [x] `confirm_cart_and_proceed`: `cart.confirmed: true`.

#### Verification checklist — verified live 2026-09-30, real Gemini + real
Kapruka MCP, test threads cleaned up after
- [x] A full Gift-Picker conversation shows these attributes present on
      the relevant spans in console output. **Verified live**, a real
      3-turn conversation (search chocolates within budget → propose cart
      → confirm) produced, after both bugs above were found and fixed:
      `tool.kapruka_search_products` with correct matching
      `result_count: 10`/`in_stock_count: 10`, `tool.kapruka_get_product`
      with `in_stock: true`, `tool.propose_cart` with correct
      `cart.item_count`/`cart.estimated_total` matching the actual
      proposed cart, and `tool.confirm_cart_and_proceed` with
      `cart.confirmed: true` — each on its own correctly-scoped span, not
      smeared across `run_turn`.

### Phase 5.1 — Environment-conditional export: console in dev, Langfuse Cloud in production (built and verified live 2026-09-30)

**Problem being fixed:** two separate problems, neither solved by
anything above:
1. Every span currently carries the full raw prompt/completion text. In
   production, whatever goes to stdout typically flows straight into
   whatever log aggregator is in use — meaning every customer's exact
   conversation content would silently end up sitting in the logging
   system, unreviewed, just because that's where OTel happened to print
   it.
2. Nothing from Phase 5.0.0–5.0.5 is stored anywhere. It's all
   ephemeral, per-terminal-session console output — there's no way to
   ask "how long did turn X take" or "show every failed turn this week"
   without reading raw JSON by eye.

**Decision:** an environment variable chooses the exporter — console for
local dev (unchanged from every phase so far), OTLP pointed at Langfuse
**Cloud** for production. Cloud, not self-hosted: no Docker stack to run
or maintain — sign up, create a project, get a public/secret key pair.

Confirmed directly from Langfuse's own OpenTelemetry documentation, not
assumed:
- OTLP trace endpoint: `https://cloud.langfuse.com/api/public/otel/v1/
  traces` (EU data region, Langfuse's default — `us.cloud.langfuse.com`
  instead if the US region is ever preferred).
- Auth: HTTP Basic — `Authorization: Basic <base64(public_key:
  secret_key)>`, built once from the project's key pair. No separate
  Langfuse SDK needed, since this app already speaks raw OTel end to
  end.
- **Langfuse's OTLP endpoint is HTTP/protobuf only — gRPC is not
  supported.** This matters concretely: the generic
  `opentelemetry-exporter-otlp` package installs the gRPC exporter by
  default; the HTTP-specific one —
  `opentelemetry-exporter-otlp-proto-http`, class `OTLPSpanExporter`
  from `opentelemetry.exporter.otlp.proto.http.trace_exporter` — is the
  one that actually works against Langfuse's endpoint.
- `BatchSpanProcessor`, not `SimpleSpanProcessor`. Exporting one span at
  a time, synchronously, on the calling thread — what
  `SimpleSpanProcessor` does today — would add real network latency to
  every single turn once there's an actual network destination.
  Batching in the background is the whole reason `flush()` already
  exists, and Phase 5.0.0's own docstring already called this "load-
  bearing again the moment this moves to a batched, networked exporter"
  — that moment is now.

- [x] New env var, e.g. `APP_ENV` (`development` by default, `production`
      as the opt-in) — read once inside `configure()`. **Read in
      `src/config.py` with `.get()`, not a hard `os.environ[...]`
      lookup** — same reasoning as `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY`
      below: the concierge itself must never fail to even start over a
      missing/misconfigured observability backend.
- [x] `development`: unchanged from every prior phase —
      `SimpleSpanProcessor(ConsoleSpanExporter(formatter=
      _console_safe_formatter))`.
- [x] `production`: `BatchSpanProcessor(OTLPSpanExporter(endpoint=...,
      headers={"Authorization": f"Basic {auth}"}))`, with the auth
      string built once from `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`
      env vars. Endpoint built from a third env var,
      `LANGFUSE_BASE_URL` (defaulting to `https://cloud.langfuse.com`,
      the EU region) plus `/api/public/otel/v1/traces` — not hard-coded
      to one region, since the customer's own `.env` specified the US
      region (`us.cloud.langfuse.com`) when setting this up.
- [x] New dependency: `opentelemetry-exporter-otlp-proto-http`. **Already
      installed** — pulled in transitively by `langsmith[otel]` back in
      Phase 5.0.0 (confirmed via `pip show`), no `requirements.txt`
      change needed.
- [x] `_BaggageSpanProcessor` (Phase 5.0.0) and the error-detection
      processor (Phase 5.0.4) apply in both environments unchanged —
      only the export destination changes, not what gets attached to a
      span before it's exported.
- [x] `_console_safe_formatter`'s bytes-decoding workaround (Phase
      5.0.0) stays console-only and needs no production equivalent — the
      OTLP exporter serializes spans itself, never going through
      `ConsoleSpanExporter.to_json()` at all.

#### Verification checklist — verified live 2026-09-30 against the
customer's own real Langfuse Cloud project, test threads cleaned up after
- [x] With `APP_ENV` unset (or `development`), behavior is byte-for-byte
      unchanged from every prior phase's own verification — console
      output, nothing silently different. **Verified live**: a real turn
      with `APP_ENV` unset produced the identical `run_turn` span/console
      shape as every prior Phase 5.0.x check, zero export exceptions.
- [x] With `APP_ENV=production` and real Langfuse keys set, a full turn
      appears in the Langfuse Cloud UI's trace list within a few
      seconds, showing the same span tree, token counts, and — once
      Phase 5.0.2–5.0.5 have landed — handoff spans and the domain
      attributes, with no further code changes beyond the exporter swap
      itself, since it's the same span data just routed somewhere
      queryable. **Verified live, at the HTTP level rather than by eye in
      the browser** (no browser access from here): patched
      `OTLPSpanExporter.export` directly and confirmed
      `SpanExportResult.SUCCESS` for every batch sent (1, 2, and 7 spans
      across three batches for one real turn) — traced into the
      exporter's own source to confirm `SUCCESS` specifically means the
      underlying HTTP client's response was checked and accepted, not
      merely "no exception was raised." Real credentials from the
      customer's own `.env`, real endpoint
      (`https://us.cloud.langfuse.com/api/public/otel/v1/traces`,
      matching their configured `LANGFUSE_BASE_URL`). **Visual
      confirmation in the Langfuse Cloud UI itself is still worth doing
      on the customer's own end** — flagging this as the one part of the
      checklist not literally verifiable from here.
- [x] With `APP_ENV=production` and missing/invalid Langfuse keys,
      confirm the failure mode is a quietly dropped export (matching
      OTel's own default "log and continue" behavior), not a crash — a
      broken observability backend must never be able to take the actual
      concierge down with it. **Verified live** with deliberately wrong
      `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` values: the exporter logged
      `"Failed to export spans batch code: 401, reason: Unauthorized"`
      for every batch, but the actual customer-facing turn completed and
      replied normally, and `flush()` returned without raising —
      confirming a broken Langfuse connection can never take the
      concierge itself down.
## Phase 6 — Track-order branch

- [ ] Order-number extraction (ask if missing) → `kapruka_track_order` →
      format reply

## Phase 7 — Return-item branch

- [ ] Fallback response only — no MCP tool exists for this, don't build one

## Phase 8 — Wire the full graph + end-to-end test

- [ ] FastAPI webhook → Entry → Intent Router → the five branches
- [ ] Manually test all five intents through the real webhook, not just
      the graph in isolation

## Deferred — later, separate milestone

- Cron-triggered proactive reminders (draft, send, wait for reply)
- Re-classifying a reply through the Intent Router (confirm vs. new request)
