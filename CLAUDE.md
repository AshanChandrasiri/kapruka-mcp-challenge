# Kapruka Gift Concierge — Project Context

## What this is

A multi-intent conversational agent built on the public Kapruka MCP server
(`https://mcp.kapruka.com/mcp`) — real inventory, real delivery quoting, real
guest checkout, not a mock API. Purpose: deliberately practice a production-
shaped agentic system (agent-vs-router judgment, human-in-the-loop gating on
a real financial action, MCP tool integration) as a portfolio-grade project.

## Tech stack

- **Orchestration:** LangGraph (`langgraph`) + LangChain — LangGraph agent node for the one
  real reasoning loop, plain async Python for the deterministic pipeline, a
  custom `StateGraph` (`ConciergeOrchestrator` graph, see below) for
  conditional dispatch on the Intent Router's result, compiled graph +
  `PostgresSaver` checkpointer to drive a turn from the FastAPI webhook
- **MCP client:** LangChain MCP adapters' `MultiServerMCPClient` — used to load and expose the Kapruka MCP tools to the graph
- **Human-in-the-loop:** a deterministic graph state's `stage` state
  machine (`src/checkout/flow.py`), not a deterministic state-machine gate; LangGraph's `interrupt()` is reserved for graph-level human-in-the-loop pauses. See
  the deterministic pipeline section below for the reasoning and the actual
  mechanism.
- **Entry point:** FastAPI (webhook receiver) — a custom app calling
  the compiled graph's `ainvoke()` per inbound message, not a generic framework scaffold (that assumes a generic chat UI; we need webhook-shaped routing).
  `src/pipeline.py::run_turn()` is the shared "run one turn" call both this
  and the entry points below will use.
- **Dev/demo UI:** `gradio_app.py` — a minimal `gr.ChatInterface`, not a
  production entry point. Doesn't use Gradio's own chat-history state as
  memory (conversation memory is the Postgres session, same as everywhere
  else); one throwaway `phone_number` identity per browser session via
  `gr.State`. Windows note: Gradio's own server ends up running request
  handlers on a `ProactorEventLoop` regardless of the
  `WindowsSelectorEventLoopPolicy` set in `src/session.py` (confirmed by
  testing — that fix alone isn't enough once Gradio/uvicorn's own loop is
  already running), so `respond()` runs the actual pipeline call in a
  short-lived thread with a freshly created loop instead of depending on
  whatever loop received the HTTP request. `main.py` (console) doesn't need
  this — a bare `asyncio.run()` picks up the policy fine on its own.
- **State / profile store:** PostgreSQL, hosted on Neon. `src/db/schema.sql`
  holds the hand-rolled `recipients`/`orders` tables (family profile, order
  history). LangGraph's `PostgresSaver` checkpointer — `src/session.py`,
  `get_checkpointer()`, a cached singleton — points at the _same_ Neon
  instance for turn-by-turn conversation state, so both live in one
  database, in separate tables (LangGraph owns its checkpoint schema; ours
  is `recipients`/`orders`). Session identity convention, used by every
  component: `phone_number == user_id == session_id`
  (`src/session.py::session_identity`) — one shared conversation, not a
  per-component one. Windows note: psycopg3's async mode needs
  `WindowsSelectorEventLoopPolicy` (default `ProactorEventLoop` doesn't
  support it) — set at import time in `src/session.py`.
- **LLM:** Google Gemini via LangChain (`langchain-google-genai`) — the configured Gemini API for local
  dev; the model integration can be switched later if needed

## Kapruka MCP server

- Endpoint: `https://mcp.kapruka.com/mcp` — Streamable HTTP, no auth required
- Rate limits: 60 requests/min per IP (all tools), 30 `kapruka_create_order`
  calls/hour per IP
- Orders are REAL — guest checkout creates a live 60-minute pay link. No sandbox.
- Full tool contracts: `docs/mcp/kapruka-mcp-tools.md`

## Agents — only ONE real reasoning loop in this system

### Gift-Picker Agent

- The only agentic (ReAct-style) node: a LangGraph agent node
  (`src/gift_picker/agent.py::build_gift_picker_agent`) that searches,
  evaluates results, refines, decides when the cart is good enough
- Tools: `kapruka_search_products`, `kapruka_get_product`, `kapruka_list_categories`
  via MCP tools scoped to just these three —
  it never even sees `kapruka_create_order`. Plus three custom
  LangChain `@tool`s (`src/gift_picker/tools.py`):
  - `get_recipient_profile(recipient_name)` — reads the `recipients` table
    (`src/db/recipients.py`, raw async psycopg), matching loosely on name
    OR relationship. Returns `{"matches": [...]}` rather than forcing a
    single-best-match heuristic in Python — ambiguity resolution stays with
    the model, consistent with this project's agent-vs-router philosophy.
  - `suggest_products(products)` — writes graph state's `product_suggestions`,
    repeatable, each call replaces rather than adds. Live API field is `id`,
    not `product_id`; the instructions and docstring both call this out.
  - `propose_cart(items, estimated_total, notes)` — writes
    graph state's `cart`, clears `product_suggestions`. Calling it ends
    the Gift-Picker's loop for the turn.
  - `propose_cart`/`suggest_products` are plain LangChain `@tool`s, deliberately
    NOT structured output like the Router — there's a currently
    open LangGraph reliability issue (`google/adk-python#3969`) where the model
    intermittently ignores structured output when it's combined with tools on
    the same agent.
- Reads the family profile table (`recipients`); doesn't write to it yet —
  saving new recipient info is out of scope for this phase
- Called from the Orchestrator's `gift_request` branch (see below), after
  the Intent Router has classified the message. Called from two places once
  phase 2 (proactive, cron-triggered) exists — same agent, different
  downstream reachability; that path is still deferred (see Scope below).

## Routers — NOT agentic. Single-shot, no loop.

### Intent Router

- One structured-output LLM call (`src/router/intent_router.py`). Implemented
  as a LangGraph agent node (structured output, no `tools`) run through
  compiled LangGraph + the shared `PostgresSaver` checkpointer — same primitives as the
  Gift-Picker agent, for one consistent pattern across the codebase. The
  "not agentic" part is behavioral, not a different class: with zero tools
  attached there is nothing for it to call, so it cannot enter a ReAct loop
  no matter what class runs it. Prompt lives in `src/prompts.py`.
- Bounded history, but only on the standalone path: `classify_intent(
phone_number, message)` (`src/router/intent_router.py`) makes its own
  compiled LangGraph call with graph invocation configurationget_session_config=checkpoint/history configuration(
  num_recent_events=4))` (LangGraph checkpointer configuration lives at
LangGraph checkpointer configuration) — used for
isolated testing (`scripts/test_intent_router.py`) and anywhere the router
runs outside the orchestrator, where the token-cost saving is real and
unconditional. (the graph's message-history handling turned out to be binary —
`'default'`/`'none'` — not a partial-N lever, so it's unused.)
  **Production path (via the orchestrator) sees full history instead** —
  see Orchestrator section below for why.
- `build_router_agent()` builds a fresh LangGraph agent node instance per call (LangGraph
  agents can't be reused as a sub-agent of more than one parent) — the
  orchestrator embeds the result as a real sub-agent rather than going
  through `classify_intent`, so the user's message gets appended to the
  shared session exactly once per turn.
- Classifies each inbound message into one of five intents: `gift_request` |
  `track_order` | `return_item` | `chitchat` | `out_of_scope`
- `chitchat` and `out_of_scope` are canned/templated responses — no DB read,
  no Kapruka tool call, no agent loop. `chitchat` covers greetings/thanks/
  capability questions; `out_of_scope` covers anything unrelated to gifting
  on Kapruka (general knowledge, competitor comparisons, off-site requests).
  Watch for shared-vocabulary false positives here (e.g. "check this product
  on eBay" mentioning "product" should NOT become `gift_request`).
- This is the only place free-text ambiguity gets interpreted — everything
  downstream of it is either the agent loop or a deterministic pipeline.

### Checkout Router

- A second structured-output classifier (`src/router/checkout_router.py`),
  same class as the Intent Router (LangGraph agent node, structured output,
  no tools — genuinely single-shot for the same reason), but used only when
  graph state's `stage` is set (a checkout is in progress). Deliberately a
  separate classifier rather than folding checkout-time classification into
  the Intent Router's 5-way schema — keeps each prompt focused on one job:
  cold-start intent vs. mid-checkout digression.
- Classifies into `answers_pending | modify_request | cancel_checkout |
unrelated`. `answers_pending` also carries `extracted_value`, so a single
  call both classifies the reply and pulls the structured value out of free
  text (e.g. "yeah ship it to Colombo 05" → `extracted_value: "Colombo
05"`) instead of a second round-trip.
- Needs `stage`, `collecting_field` (which field is actually pending), and a
  `cart`/`checkout_info` snapshot serialized explicitly into its prompt —
  none of that lives in the message transcript the way Intent Router
  context does.
- **Never a confirmation gate.** `answers_pending` at the `awaiting_confirm`
  stage is a routing decision, not itself a "yes." See Human Confirm below
  — `_is_confirmation`'s deterministic keyword check is still the only
  thing that can trigger `create_order`.

## Orchestrator — dispatches on the active classifier's result

`src/orchestrator.py::ConciergeOrchestrator`, a custom LangGraph `StateGraph`
builder — the root graph in `main.py` (and later the FastAPI webhook) drives
via the compiled graph for every turn, with no history bound on that top-level call
(full history, every turn — see below for why). The graph runs the Intent Router
node using the same shared graph state (not a second compiled graph invocation —
that would append the inbound message twice), reads the classification from graph
state, then dispatches: canned
response for `chitchat`/`out_of_scope`, the Gift-Picker sub-agent for
`gift_request`, a stub (log line + placeholder reply) for
`track_order`/`return_item` until their phases land.

**Entry routing, before either classifier runs:** `_route_from_start` (a
conditional edge from `START` itself) checks graph state's `stage` first.
`stage` absent → the Intent Router runs as described above. `stage` set (a
checkout already in progress) → the Checkout Router runs instead (see
Routers section above), and its result dispatches to
`_advance_checkout`/`handle_awaiting_confirm` (`answers_pending`), back to
the Gift-Picker sub-agent (`modify_request`/`unrelated` — reusing the same
hand-back mechanism the Deterministic pipeline section describes for a
failed delivery check), or a new cancel branch that clears checkout state
(`cancel_checkout`). Earlier, a stage in progress bypassed classification
entirely; every turn now goes through one classifier or the other.

Not a purely sequential graph — every node in a fixed sequence would run
unconditionally, with no way to skip based on the classification result. Not the
supervisor/conditional-edge pattern either — that pattern's routing decision comes
from an agent node's own reasoning via `conditional graph edges`/`graph routing`;
this dispatches on a plain string the router already returned. A custom
`StateGraph` is LangGraph's graph-based pattern for exactly this shape of
conditional routing.

**History bounding — resolved, not simplified away:** Phase 1 bounded the
router to its last ~4 events at the top-level compiled LangGraph call. Phase 2's
Gift-Picker needs full history in that same shared context (it's the one
node that genuinely needs the whole back-and-forth as working memory), and
there's no way to know in advance — before the router has classified the
message — whether a given turn will even reach the Gift-Picker. So the
top-level bound was removed entirely rather than kept as a half-measure:
full history is a correctness requirement for the Gift-Picker, where the
router's bound was only ever a cost optimization, and that optimization
stops paying off anyway on any turn where the Gift-Picker also runs (the
expensive full fetch happens regardless once it's needed). The bounded path
lives on in `classify_intent()`'s own standalone compiled LangGraph call, which is
what `scripts/test_intent_router.py` exercises.

### `gift_request` dispatch (`ConciergeOrchestrator._run_gift_picker`)

1. Yields its own housekeeping a graph state update
   {"product_suggestions": []}))`before invoking the Gift-Picker — a
structural reset, not something left to the model to remember. Confirmed
(by reading the LangGraph compiled LangGraph source) that a yielded event's state update
is applied to graph state via`append_event` _before_ the
   generator resumes, so the Gift-Picker sub-agent genuinely sees the reset.
2. Runs the Gift-Picker as a sub-agent sharing shared graph state, watching its yielded
   events for a `"cart"` key in `the node's returned state update` — i.e. did
   `propose_cart` actually fire _this turn_ — rather than checking whether
   graph state's `cart` merely exists (which could be stale from an
   earlier turn and would wrongly suppress a genuinely new cart later in
   the same conversation).
3. Cart present this turn → hands off to `src/checkout/flow.py::start_checkout`
   (Phase 3, below). Absent → relay the Gift-Picker's own final text as the
   orchestrator's own event (keeps "who speaks to the customer" uniformly
   `concierge_orchestrator`, matching `chitchat`/`out_of_scope`),
   console-print `product_suggestions` as a stand-in for "cards" until
   there's a real UI.

## Deterministic pipeline — no LLM judgment involved

`Check Delivery` (`kapruka_check_delivery`) → `Show Summary` → `Human Confirm`
→ `Checkout` (`kapruka_create_order`) → `Track Order` (`kapruka_track_order`)

`src/checkout/` — plain async Python calling MCP tools directly via a raw
`mcp` client (`mcp_client.py`), not `MultiServerMCPClient` from `langchain-mcp-adapters`. No agent loop, no
LLM judgment, with one exception: resolving a conflict re-invokes the
_same_ Gift-Picker sub-agent (reuse, not a new feature — see below).

**Why a raw MCP client instead of `MultiServerMCPClient`:** every Kapruka tool wraps
its arguments in a single `params` object and supports
`response_format: "json"` (confirmed against the live tool schemas — not
guessed), and the JSON payload comes back double-encoded as a JSON _string_
inside `structuredContent["result"]`, not `structuredContent` itself
(confirmed via a live call). `MultiServerMCPClient` is built for an LLM's tool-calling
loop (schema exposure to the model, `interrupt()` tied into LangGraph's
own flow processor); none of that applies to a deterministic call site, so
`src/checkout/mcp_client.py::call_kapruka_tool()` talks to the server
directly instead.

**State machine** (graph state's `stage`, `src/checkout/flow.py`), checked
by `_route_from_start` to decide which classifier runs a given turn — the
Checkout Router when a stage is set, the Intent Router otherwise (see
Orchestrator section above): `collecting_delivery` |
`resolving_delivery_conflict` | `awaiting_confirm` | absent (normal flow).
`Check Delivery` runs once per _distinct product_, not once per cart —
deliverability is scoped per item, not per shipment (food/liquor/hotel-cake
items reach far fewer cities than flowers). A failed check injects a
`[System note — not from the customer: ...]` event describing what failed,
then re-invokes the Gift-Picker sub-agent — the same hand-back mechanism
the Checkout Router's `modify_request`/`unrelated` results also use, since
all three cases are really "hand it back to the agent that knows how to
build a cart."

**`collecting_delivery` collects more than the plan originally named:**
the live `kapruka_create_order` schema (checked directly, not assumed)
requires `recipient{name,phone}`, `delivery{address,city,date}`,
`sender{name}` — not just city/date. `propose_cart` only captures
city/date (see the Gift-Picker section above); `collecting_delivery` asks
for whatever else is missing, one field per turn. The field write itself
still has no LLM judgment — the Checkout Router (see Routers section
above) is what classifies the reply and extracts the value before
`_advance_checkout` ever writes it. Known rough edge, still open: the
Gift-Picker often already states these details in its own narration when
the customer gives them up front, so re-asking can read as repetitive —
not fixed by the Checkout Router, would mean growing `propose_cart`'s
schema further.

**Human Confirm — why not LangGraph's `interrupt()`:** that mechanism's
pause/resume lives inside LangGraph's graph execution and interrupt mechanism
(`interrupt()` / checkpointing) and LangGraph's
resumability feature (confirmed by reading the source, not assumed) —
using it here would mean either routing `kapruka_create_order` through an
LLM agent's tool-calling turn (reintroducing agent/LLM judgment into
checkout, which this phase is explicitly avoiding) or standing up LangGraph
resumability just for this one call. Instead, "explicit human confirm" is
enforced structurally: a deterministic keyword check
(`src/checkout/flow.py::_is_confirmation`, not a classifier call) gates the
_only_ call site `kapruka_create_order` has anywhere in this codebase
(`src/checkout/order.py::create_order`, called only from
`handle_awaiting_confirm` after that check passes) — verifiable by
inspection, and live-verified that the gate itself holds (a bare "yes"
with no checkout in progress does nothing; a non-yes reply during confirm
does not check out). **Unchanged by the Checkout Router (see Routers
section above):** its `answers_pending` classification only decides that a
reply should reach `handle_awaiting_confirm` at all — `_is_confirmation`
still independently gates `create_order` on the raw text every time.

On success: writes `phone_number`, `items` (JSONB), `product_summary`,
`total_amount`, `delivery_city`, `delivery_date`, `kapruka_order_id`,
`status` to `orders` (`src/checkout/order.py::save_order` — the `orders`
table gained those four columns via an `ALTER TABLE` in
`src/db/schema.sql`), then structurally clears `cart`/`checkout_info`/
`stage`/`collecting_field`. `Track Order` here
(`src/checkout/order.py::track_order_once`) is a best-effort immediate
status check right after checkout — distinct from Phase 4's `track_order`
_intent_, which is a customer asking about a past order out of the blue.
Same MCP tool, two different callers.

**Untested against a live success response, by design:** `kapruka_create_order`
is a real, live financial action (a real pay link), so unlike the rest of
this pipeline it was never exercised end-to-end during development — the
whole flow up to `awaiting_confirm` (including a real `kapruka_check_delivery`
call returning a real delivery fee) was verified live, but no "yes" was
ever sent. The response field names `create_order`/`save_order` assume
(`order_ref`, a pay-link key) are inferred from the tool's own schema/docs,
not confirmed — verify (and adjust if needed) the first time this actually
runs for real.

**Hard rule: `kapruka_create_order` is only ever called after an explicit
human confirm. Never let the agent call checkout directly, and never skip
the confirmation step "to save a round trip."**

## Known capability gap

Kapruka MCP has no return/refund tool. `return_item` is a fallback branch
(share return policy / hand off to a human), not a working agent — don't try
to build a "return agent" against a tool that doesn't exist.

## Scope for this build (v1)

Interactive/conversational path only. The cron-triggered proactive reminder
flow (drafting gift suggestions for saved occasions) is deferred to a later
milestone — see `docs/architecture/target-architecture.drawio` for the full
picture and `docs/architecture/v1-conversational-flow.drawio` for what's
actually being built right now.

**Known stale doc:** both `.drawio` diagrams still label nodes with
LangGraph/LangChain terms (StateGraph, `interrupt()`, `MultiServerMCPClient`)
from before the move to LangGraph + LangChain — needs a manual pass in the drawio editor
to relabel; not safe to hand-edit as raw XML.

## Working conventions

- Current phase and status live in `PLAN.md` — check it at the start of a
  session, update it at the end.
- When something in here goes stale (a decision changes, scope shifts),
  update this file in the same session — it should always reflect current
  reality, not the original plan.
