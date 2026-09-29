# Kapruka Gift Concierge — Project Context

## What this is

A multi-intent conversational agent built on the public Kapruka MCP server
(`https://mcp.kapruka.com/mcp`) — real inventory, real delivery quoting, real
guest checkout, not a mock API. Purpose: deliberately practice a production-
shaped agentic system (agent-vs-router judgment, human-in-the-loop gating on
a real financial action, MCP tool integration) as a portfolio-grade project.

## Tech stack

- **Orchestration:** LangGraph (`langgraph`) + LangChain — LangGraph agent
  nodes for the real reasoning loops (Gift-Picker, Checkout Info Agent,
  Confirm Agent — see Agents below), a custom `StateGraph`
  (`ConciergeOrchestrator` graph, see below) for conditional dispatch on
  the active classifier/agent's result, compiled graph + `PostgresSaver`
  checkpointer to drive a turn from the FastAPI webhook
- **MCP client:** LangChain MCP adapters' `MultiServerMCPClient` — used to load and expose the Kapruka MCP tools to the graph
- **Human-in-the-loop:** a graph state `stage` state machine driving which
  agent owns a given turn (`src/orchestrator.py`), narrowed as of Phase 3.6
  to exactly one literal deterministic gate — the trigger for
  `kapruka_create_order` (`src/checkout/flow.py::is_confirmation`) — with
  everything else agentic; LangGraph's `interrupt()` is reserved for
  graph-level human-in-the-loop pauses and isn't used here. See "The one
  deterministic gate" section below for the reasoning and the actual
  mechanism.
- **Entry point:** FastAPI (webhook receiver) — a custom app calling
  the compiled graph's `ainvoke()` per inbound message, not a generic framework scaffold (that assumes a generic chat UI; we need webhook-shaped routing).
  `src/pipeline.py::run_turn()` is the shared "run one turn" call both this
  and the entry points below will use.
- **Dev/demo UI:** `gradio-chat.py` — a minimal `gr.ChatInterface`, not a
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
  holds the hand-rolled `recipients`/`orders`/`order_products` (Phase 3.8)/
  `threads` (Phase 4) tables (family profile, order history, per-item order
  detail, thread<->customer mapping). LangGraph's `PostgresSaver`
  checkpointer — `src/session.py`, `get_checkpointer()`, a cached singleton
  — points at the _same_ Neon instance for turn-by-turn conversation state,
  so both live in one database, in separate tables (LangGraph owns its
  checkpoint schema; ours is `recipients`/`orders`/`order_products`/
  `threads`). Identity convention: `phone_number` is the permanent customer
  identity (`user_id == session_id == phone_number`, unchanged since
  Phase 1); `thread_id` is a separate, per-conversation value as of
  Phase 4 (`src/session.py::session_identity(phone_number, thread_id)`) —
  see the Chat identity section below for why and how. Windows note:
  psycopg3's async mode needs `WindowsSelectorEventLoopPolicy` (default
  `ProactorEventLoop` doesn't support it) — set at import time in
  `src/session.py`.
- **LLM:** Google Gemini via LangChain (`langchain-google-genai`) — the configured Gemini API for local
  dev; the model integration can be switched later if needed
- **Observability:** OpenTelemetry (Phase 5, rolled out incrementally as
  `5.0.0`/`5.0.1`/... sub-phases) — `src/observability.py`'s reusable
  `configure()`/`flush()`/`get_tracer()`/`turn_context()`, console-output
  only so far (`ConsoleSpanExporter`, no Langfuse/OTLP yet).
  `LANGSMITH_TRACING_MODE=otel` routes LangChain/LangGraph's own run
  tracing through this same local `TracerProvider` — no LangSmith account,
  no network call. As of Phase 5.0.1, only `src/pipeline.py::run_turn`'s
  root span is wired in on purpose, but the LangChain/LangGraph bridge
  auto-instruments every node of the compiled graph underneath it for
  free (real Gemini token-usage attributes included) — see the
  Observability section below for the real limitations found live (a
  `bytes`-attribute serialization bug, incomplete baggage propagation, and
  an incomplete failure-status bridge gap) before trusting this further.

## Kapruka MCP server

- Endpoint: `https://mcp.kapruka.com/mcp` — Streamable HTTP, no auth required
- Rate limits: 60 requests/min per IP (all tools), 30 `kapruka_create_order`
  calls/hour per IP
- Orders are REAL — guest checkout creates a live 60-minute pay link. No sandbox.
- Full tool contracts: `docs/mcp/kapruka-mcp-tools.md`

## Agents — three real reasoning loops now (Phase 3.6 added two)

Through Phase 3.5 there was exactly one real ReAct loop in this system
(the Gift-Picker) plus two zero-tool structured-output classifiers (Intent
Router, and the now-retired Checkout Router). Phase 3.6 retired the
Checkout Router and Phase 3's deterministic one-field-per-turn machinery
entirely — replaced by two more real agents (Checkout Info Agent, Confirm
Agent), same class as the Gift-Picker, each scoped to a narrow tool set and
owning its own judgment. The Intent Router is still the only classifier
left; everything checkout-shaped is agentic now except one literal
deterministic keyword gate (see Human Confirm below).

All three checkout-time agents are embedded as literal, always-on subgraph
nodes (built once at graph-compile time, same mechanism the Gift-Picker
already used) — per-turn context that a static `system_prompt` string
can't express (a mid-checkout handoff reason, the current cart, today's
date, the order summary) reaches them via `dynamic_prompt` middleware,
which re-evaluates at actual model-call time inside each agent's own ReAct
loop rather than needing the node rebuilt. Tools that need to read live
cart/checkout_info without asking the model to retype them as arguments
use LangGraph's `InjectedState` (with a default value on the parameter —
**found live**: without one, a field that's genuinely absent on a fresh
checkout, e.g. `checkout_info` before it's ever been written, raises a
bare `KeyError` instead of injecting `None`).

### Gift-Picker Agent

- A LangGraph agent node (`src/gift_picker/agent.py::build_gift_picker_agent`)
  that searches, evaluates results, refines, decides when the cart is good
  enough — unchanged embedding mechanism since Phase 1/2.
- Tools: `kapruka_search_products`, `kapruka_get_product`, `kapruka_list_categories`
  via MCP tools scoped to just these three — it never even sees
  `kapruka_create_order`. Plus custom LangChain `@tool`s
  (`src/gift_picker/tools.py`):
  - `get_recipient_profile(recipient_name)` — reads the `recipients` table
    (`src/db/recipients.py`, raw async psycopg), matching loosely on name
    OR relationship. Returns `{"matches": [...]}` rather than forcing a
    single-best-match heuristic in Python — ambiguity resolution stays with
    the model, consistent with this project's agent-vs-router philosophy.
  - `suggest_products(products)` — writes graph state's `product_suggestions`,
    repeatable, each call replaces rather than adds. Live API field is `id`,
    not `product_id`; the instructions and docstring both call this out.
  - `propose_cart(items, estimated_total, notes)` — writes graph state's
    `cart`, clears `product_suggestions`. Cart-only as of Phase 3.6 — no
    `delivery_city`/`delivery_date` params anymore; delivery details are
    gathered later by the Checkout Info Agent, once approved.
  - `confirm_cart_and_proceed()` (Phase 3.6, new) — the customer approved
    the CURRENT cart with nothing further to change; sets `cart_confirmed`,
    the orchestrator's signal to advance to the Checkout Info Agent. A
    `propose_cart` diff alone, even the first one, no longer auto-advances
    anywhere — this is what actually fixes that.
  - `cancel_checkout()` (Phase 3.6, shared with the other two agents —
    `src/checkout/shared_tools.py`) — clears the whole checkout.
  - `propose_cart`/`suggest_products`/`confirm_cart_and_proceed` are plain
    LangChain `@tool`s, deliberately NOT structured output like the Router
    — there's a currently open LangGraph reliability issue
    (`google/adk-python#3969`) where the model intermittently ignores
    structured output when it's combined with tools on the same agent.
- Reads the family profile table (`recipients`); doesn't write to it yet —
  saving new recipient info is out of scope for this phase.
- Reachable two ways now: fresh, from the Intent Router's `gift_request`
  classification; and mid-checkout, via `handoff_reason` (set by
  `request_cart_revision`, called from the Checkout Info or Confirm agent)
  — same node either way, read by a `dynamic_prompt` middleware that adds
  "you're being consulted mid-checkout because: {reason}" to its system
  prompt when non-null.

### Checkout Info Agent (Phase 3.6, new — replaces Phase 3's deterministic Gate 1/2/3)

- `src/checkout/checkout_info_agent.py::build_checkout_info_agent`. Gathers
  everything `kapruka_create_order` needs besides the cart itself:
  delivery city, delivery date, recipient name/phone, delivery address,
  sender name. Replaces `_advance_checkout`'s hard-coded gate ordering with
  prompt-level sequencing guidance (settle city/date and validate delivery
  before asking for the rest) — the agent's own judgment, not a code gate.
- Tools: `resolve_city(query)` (thin wrapper over the existing
  `src/checkout/delivery.py::resolve_city`), `check_delivery(city, date)`
  (wraps `check_delivery_for_cart`, reads the live cart via
  `InjectedState("cart")` — no need to make the model retype cart
  contents), `finalize_checkout_info(...)` (writes all six fields, merging
  with whatever `check_delivery` already recorded rather than overwriting
  it — the `checkout_info_finalized` flag it also sets is the
  orchestrator's signal to advance to the Confirm Agent), plus the shared
  `request_cart_revision`/`cancel_checkout`.
- No lookup/autofill for the new fields — checked `src/db/schema.sql`
  directly: `recipients` only stores the customer's own name/relationship/
  preferences, nothing about a recipient's phone or a delivery address.
- Date resolution ("next thursday," "the 25th") is the agent's own
  reasoning against today's date (formatted into its prompt), not a regex
  — `check_delivery` independently rejects anything that isn't a real
  calendar date at least one day out as a backstop against bad arithmetic,
  same posture Phase 3's `_invalid_date_reason` had, adapted rather than
  reused verbatim (that function is retired).
- On a failed `check_delivery`, or any request to change the cart itself:
  calls `request_cart_revision` directly — no separate revision node.
- Never has `kapruka_create_order` — same tool-scoping exclusion as the
  Gift-Picker.

### Confirm Agent (Phase 3.6, new — replaces `handle_awaiting_confirm`'s non-yes fallback)

- `src/checkout/confirm_agent.py::build_confirm_agent`. Opens every model
  call with the existing `build_summary` output (unchanged, reused as-is)
  formatted into its system prompt via `dynamic_prompt` — no separate
  "show summary" tool.
- Tools: shared `request_cart_revision`/`cancel_checkout`, plus
  `ask_final_confirmation()` — arms `awaiting_final_yes` and prompts the
  model to ask a clear yes/no. **This agent never has
  `kapruka_create_order` as a tool, verifiable by inspection** — see Human
  Confirm below for the actual gate.
- **Found live:** its `dynamic_prompt` callback re-runs on every model call
  within its own ReAct loop, not just the first — if `cancel_checkout`
  fires mid-loop (clearing `checkout_info`) and the agent's own ReAct loop
  then makes one more call to generate its trailing "okay, cancelled!"
  reply, the callback used to call `build_summary` against an
  already-cleared `checkout_info` and crash with a bare `KeyError`. Fixed
  by checking all six required fields are still present before calling
  `build_summary`, falling back to a placeholder summary text otherwise —
  `build_summary` itself is untouched.

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
- This is the only place free-text ambiguity gets interpreted for a *fresh*
  request — everything downstream of it (checkout included, as of Phase
  3.6) is agentic, except one literal deterministic keyword gate (see
  Human Confirm below).

**Retired, Phase 3.6:** the Checkout Router (`src/router/checkout_router.py`,
deleted) — a second structured-output classifier used only mid-checkout.
Replaced by the Checkout Info and Confirm agents owning their own judgment
directly, rather than a classifier deciding how to route to deterministic
handlers. See the Agents section above and the `_route_from_start`
paragraph below for what replaced it and why.

## Orchestrator — dispatches on the active classifier/agent's result

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

**Entry routing, `_route_from_start` (Phase 3.7 shape):** a conditional
edge from `START` itself, checking graph state's `stage`. Absent →
Intent Router (as above). `with_gift_picker` → straight to the Gift-Picker
node. `checkout_info` → straight to the Checkout Info Agent node.
`confirm` → straight to the `confirm_agent` node regardless of
`awaiting_final_yes` — as of Phase 3.7 that flag is checked INSIDE the
node's own wrapper function (`_run_confirm_agent`), not at this routing
layer (see Human Confirm below; Phase 3.6 had a separate
`check_final_confirmation` node/branch for this, since retired). No
classifier sits in front of any checkout stage anymore — each stage routes
directly to the agent that owns it.

**Stages, final shape:** `with_gift_picker` (picking/revising a cart,
reached once a cart is proposed and awaiting approval, or from any
hand-back out of the other two agents — one destination either way, not
two the way Phase 3.5's `resolving_delivery_conflict` was a separate name
for the same thing) | `checkout_info` (gathering everything
`kapruka_create_order` needs besides the cart) | `confirm` (order summary
shown, working toward a final yes/no) | absent.

**Single exit condition per stage — what actually fixes the old
"auto-advance on any cart diff" complaint:** a `propose_cart` diff alone,
even the very first one, no longer advances anywhere by itself — only
`confirm_cart_and_proceed` firing (flagged via `cart_confirmed`, reset
`False` right before every Gift-Picker run) advances `with_gift_picker` ->
`checkout_info`, and only `finalize_checkout_info` firing (`checkout_info_finalized`,
same reset pattern) advances `checkout_info` -> `confirm`.

**`handoff_reason` replaces the old `_invoke_gift_picker_for_revision`
mechanism:** set by the shared `request_cart_revision` tool (Checkout Info
or Confirm agent), read by the Gift-Picker's `dynamic_prompt` middleware,
cleared right after the Gift-Picker node runs. Routing on it stays in the
parent graph, not inside the tool — a `Command(graph=Command.PARENT)` call
from inside a subgraph's tool was considered and rejected as untested in
this codebase; a conditional edge checking `handoff_reason` after each
checkout agent node reuses the same plain-`Command(update=...)` mechanism
`propose_cart` already proves works, just watched one level up.

**`cancel_checkout` is a shared tool now** (`src/checkout/shared_tools.py`),
not a router-reached node — any of the three agents can recognize "the
customer wants out" and call it directly; it clears `stage` to `None`,
which every post-agent routing check treats as "done, nothing further to
route this turn."

**Found live, a second instance of the exact bug already documented above
for the Intent Router:** routing straight from one embedded agent into the
next within the SAME turn (Gift-Picker -> Checkout Info Agent on
`cart_confirmed`; Checkout Info -> Confirm Agent on
`checkout_info_finalized`; either -> Gift-Picker on `handoff_reason`)
leaves shared `messages` ending on the FIRST agent's own final reply — a
normal assistant turn, since its ReAct loop always produces one after a
tool call — and Gemini refuses a request that doesn't end on a user
message or a function response. This didn't surface until two real agents
actually chained within one turn, live. Fixed the same way Phase 3's
`_invoke_gift_picker_for_revision` already fixed an analogous case: each
transition node injects a synthetic `[System note — not from the
customer: ...]` `HumanMessage` before handing off. The one exception:
the deterministic gate's own fall-through to the Confirm Agent needs no
such note, since that check makes no agent/LLM call itself and `messages`
still ends on the customer's own real reply.

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

### `gift_request` dispatch (`reset_before_gift_picker` -> `gift_picker` -> `_route_after_gift_picker`)

1. `_reset_before_gift_picker` resets `product_suggestions`/`cart_confirmed`
   and snapshots `cart` before invoking the Gift-Picker — structural, not
   left to the model to remember.
2. Runs the Gift-Picker as an embedded subgraph node sharing graph state.
3. `_route_after_gift_picker` checks `cart_confirmed` (Phase 3.6) — set
   this turn → advance to the Checkout Info Agent (`enter_checkout_info`,
   same turn). Not set → relay: sets `stage` to `with_gift_picker` if a
   cart currently exists (even the first proposal, still awaiting
   approval) or clears it back to absent otherwise (including right after
   `cancel_checkout`) — see the **found live** bug note below for why this
   can't just check "was stage already set."

**Found live:** `_relay_gift_picker`'s stage-setting originally checked
"was `stage` already non-`None` going in" to decide whether to keep it at
`with_gift_picker` — but that's ambiguous: a genuinely fresh `gift_request`
(stage never set) and a genuine `cancel_checkout` (stage just cleared)
both look identical from that check (`stage is None` either way), so the
very first `propose_cart` in a conversation was silently falling back to
Intent Router re-classification on the next turn instead of staying with
the Gift-Picker. Fixed by keying off whether `cart` currently exists
instead — a cart existing means "still with_gift_picker, awaiting
approval," matching the plan's own framing exactly (only Checkout Info and
Confirm agents are safe checking "was stage already set," since they're
never entered from a stage-absent start the way the Gift-Picker is).

## The one deterministic gate — Human Confirm

Phase 3.6 retired everything else that used to live under "deterministic
pipeline": `Check Delivery`/`Show Summary`/one-field-per-turn collection
are now the Checkout Info Agent's own tool calls and judgment (see Agents
above). Exactly one thing in the whole system is still deterministic: the
literal trigger for `kapruka_create_order`.

`src/checkout/flow.py` now holds only `is_confirmation` (the keyword check,
renamed from `_is_confirmation` — public now since `src/orchestrator.py`
calls it directly, cross-module, as the literal gate) and `complete_order`
(wraps `create_order`/`save_order`/`track_order_once`). Everything else
that used to live here — `_advance_checkout`, `handle_collecting_delivery`,
`_ask`, `FIELD_PROMPTS`, `_invalid_date_reason`,
`_invoke_gift_picker_for_revision`, `_hand_back_to_gift_picker`,
`handle_checkout_digression`, `cancel_checkout_node` as a node — is
retired, not adapted.

**Why not LangGraph's `interrupt()`, still true:** that mechanism's
pause/resume lives inside LangGraph's own execution/checkpointing, and
using it here would mean either routing `kapruka_create_order` through an
LLM agent's tool-calling turn (reintroducing agent/LLM judgment into the
one place this system deliberately keeps it out) or standing up LangGraph
resumability just for this one call. Instead (Phase 3.7 shape — folded the
check into the `confirm_agent` node itself, replacing Phase 3.6's separate
`check_final_confirmation` node and `_route_from_start` branch):
`_route_from_start` routes `stage == "confirm"` straight to `confirm_agent`
regardless of `awaiting_final_yes` (set by the Confirm Agent's
`ask_final_confirmation` tool); the node's own wrapper function,
`_run_confirm_agent` (`src/orchestrator.py`), checks `awaiting_final_yes`
first as plain code — if set, it runs `is_confirmation` directly on the
customer's raw reply, no agent/LLM call involved in that specific check at
all. Pass → `complete_order` (the only call site `kapruka_create_order`
has anywhere in this codebase). Fail → clear `awaiting_final_yes`, fall
through in the same function call to the actual compiled Confirm Agent, no
extra graph hop, to actually figure out what the customer meant.
**Live-verified the gate holds** post-Phase-3.6, before this Phase 3.7
relocation: a non-yes reply while `awaiting_final_yes` was armed did not
check out and correctly fell through to the Confirm Agent instead. The
safety property itself is unchanged by the Phase 3.7 move (plain code,
runs before any LLM call, single `complete_order` call site) — only where
it physically lives moved, from a separate node into the `confirm_agent`
node's own wrapper — but that specific relocation has not yet been
re-verified live. `kapruka_create_order` itself remains untested against a
live success response, by design — same deliberate boundary as every
prior phase, never send a real "yes" during development.

**Why `Check Delivery` moved into an agent instead of staying a
deterministic per-item loop:** Phase 3's `_advance_checkout` hard-coded a
Gate-1-before-2-before-3 field order and a strict `datetime.strptime`
date parse that rejected anything but a bare ISO date ("next thursday"
included). The live `kapruka_create_order` schema (checked directly, not
assumed) still requires `recipient{name,phone}`, `delivery{address,city,date}`,
`sender{name}` — `propose_cart` still only captures the cart itself; the
Checkout Info Agent gathers the rest, with prompt-level sequencing
guidance (settle city/date, validate delivery, then ask for the rest)
replacing the old hard gate order, and its own reasoning against an
injected today's-date replacing the regex date parse.

On success (`complete_order`, `src/checkout/flow.py`): as of Phase 3.8,
`src/checkout/order.py::save_order` is a two-table transactional insert,
not the old single JSONB-blob row — one `orders` row (`phone_number`,
`total_amount`/`items_total`/`delivery_fee`/`addons_total`/`currency` from
`order_result["summary"]`, the six `checkout_info` fields, `payment_url`
(`order_result["checkout_url"]`) plus a `payment_url_expires_at` computed
as `now() + 60 minutes` — Kapruka's own guest-checkout pay-link window,
`kapruka_order_ref` (`order_result["order_ref"]`), `status`), then one
`order_products` row per cart item (`kapruka_product_id`, `product_name`,
`product_url`, `product_image_url`, `unit_price`, `quantity`) — replacing
the old `items` (JSONB) / `product_summary` columns entirely, dropped in
the same migration. `product_url`/`product_image_url` depend on
`propose_cart` actually carrying `url`/`image_url` forward per item (its
docstring was tightened in Phase 3.8 to require both, plus an explicit
`quantity` field — one row per distinct product, never one row per unit;
`create_order`'s own cart-quantity was previously hardcoded to `1`
regardless of what was in the cart, fixed to `item.get("quantity", 1)` in
the same phase). Then structurally clears
`cart`/`checkout_info`/`stage`/`handoff_reason`/`awaiting_final_yes`.
`Track Order` here (`src/checkout/order.py::track_order_once`)
is a best-effort immediate status check right after checkout — distinct
from Phase 5's `track_order` _intent_, which is a customer asking about a
past order out of the blue. Same MCP tool, two different callers.

Response field names (`order_ref`, `checkout_url`, `summary.grand_total`)
are confirmed directly against the live tool schema, not inferred — see
Phase 3's own PLAN.md entry. `kapruka_create_order` itself remains
untested against a live SUCCESS response, by design (see the gate section
above) — everything up to and including a real `kapruka_check_delivery`
call has been verified live, but no real "yes" has ever been sent.

**Hard rule: `kapruka_create_order` is only ever called after an explicit
human confirm. Never let the agent call checkout directly, and never skip
the confirmation step "to save a round trip."**

## Chat identity — thread_id decoupled from phone_number (Phase 4)

Through Phase 3.8.1, `session_identity` collapsed `thread_id = user_id =
session_id = phone_number` — a WhatsApp-shaped assumption (one customer,
one conversation, forever, no concept of "starting over"). Phase 4 moves
to a normal chat-agent interface instead, where the client owns
conversation boundaries the way Claude.ai/ChatGPT's own "New Chat" button
already does — there's no reason left to guess session boundaries the way
a phone-number-only integration would have to.

`phone_number` stays the permanent customer identity, keying
`recipients`/`orders`/`order_products`/`threads` — nothing about that
changes. `thread_id` is now independent: `src/pipeline.py::run_turn(
phone_number, message, thread_id=None)` generates a fresh one (`uuid4()`)
whenever the caller doesn't supply one — that's the actual "no thread_id
means a new chat" mechanism — and returns `(reply_text, thread_id)` so the
caller can persist and reuse it on the next call. **Verified live, against
the real checkpointer, not just by signature:** two turns on the same
`thread_id` land in the same checkpoint (4 messages after 2 turns);
supplying no `thread_id` a second time under the same `phone_number`
produces a genuinely separate checkpoint (a fresh 1-turn history) — the
decoupling was checked directly against `checkpointer.aget_tuple`, not
inferred from the reply text (a plain-text reply can look identical
either way for `chitchat`/`out_of_scope`-shaped messages).

**New `threads` table** (`src/db/schema.sql`, `src/db/threads.py::touch_thread`)
— `AsyncPostgresSaver` only knows about `thread_id`s, not which customer
any of them belong to, so without this table there's no way to answer
"show this customer their past chats" at all (the surface itself isn't
built yet, just made possible). One upsert per turn
(`INSERT ... ON CONFLICT (thread_id) DO UPDATE SET last_active_at = now()`)
handles both "first time this thread_id is seen" (inserts) and "every
other turn" (updates `last_active_at`) in one statement.

`main.py`'s `THREAD_ID` constant is a hardcoded placeholder, not the real
mechanism — the console has no way to simulate "a client starting a new
chat," so it just keeps local dev/testing continuous across restarts.
`gradio-chat.py` generates its own throwaway `thread_id` once per browser
session (a second `gr.State` callable-default, same pattern as its
existing `phone_number` one) rather than round-tripping through
`run_turn`'s own generation, since it wants one continuous conversation
per tab. The real behavior (a caller genuinely choosing to pass or omit
`thread_id` per request) is only meaningfully exercised once the FastAPI
webhook exists.

**History compaction after a completed order** — no longer the primary
defense against unbounded history growth (the New Chat boundary is); a
narrower safety net for a customer who keeps talking in the same thread
after checkout. `src/checkout/flow.py::complete_order` builds a short
summary (products, order ref, delivery city/date, `Cart["notes"]` if
present — Kapruka's own `gift_message` field isn't folded in, since
`checkout_info` doesn't carry it at all yet, see Phase 3.8's own flagged
gap) and returns it as `order_summary_for_compaction`, read directly off
`ainvoke`'s return value by `run_turn` — not re-fetched from state — right
after that turn's graph invocation returns. `src/checkout/compaction.py::compact_thread`
then runs as a deliberately separate step (never inside a still-running
graph invocation, where its own checkpoint writes would race the graph's
own end-of-step write and lose): `checkpointer.adelete_thread(thread_id)`
followed by `graph.aupdate_state(config, {...}, as_node="confirm_agent")`
to seed one fresh checkpoint holding just the summary. Not atomic — a
crash between the two calls leaves the thread's checkpoint history empty,
not corrupted; accepted on purpose, since the order itself is already
safely committed to `orders`/`order_products` by this point, so the worst
case is lost chat context, not lost order data. Failures are logged, not
swallowed silently, and can never affect the already-completed order —
compaction only runs after `complete_order`'s own transaction has already
succeeded, in a separate step with its own independent try/except.
**Verified live, against the real orchestrator and real Neon (no real
order involved — a fabricated summary string was reseeded directly,
never `kapruka_create_order`):** a 2-turn thread (4 messages) was
compacted down to exactly 1 message; the very next real turn on that same
`thread_id` correctly continued from the summary (3 messages: summary +
new turn), not the pre-compaction history — resolving what was flagged as
a genuinely open risk during design (delete-then-reseed against the real
subgraph-based orchestrator, not just a stand-in graph, had never been
exercised). A forced compaction failure (simulated) was confirmed caught
and logged without propagating or affecting anything already written.

## Observability — OpenTelemetry, rolled out incrementally (Phase 5)

`src/observability.py` — reusable pieces so every agent shares one setup
instead of reinventing it: `configure()` (a `TracerProvider` +
`SimpleSpanProcessor(ConsoleSpanExporter(...))`, console-output only, no
Langfuse/OTLP yet), `flush()`, `get_tracer()`, and `turn_context(phone_number,
thread_id)` (a context manager attaching both as OTel baggage for one
turn). `configure()` also sets `LANGSMITH_TRACING`/`LANGSMITH_TRACING_MODE=otel`
so LangChain/LangGraph's own run-tracing routes through this same local
provider — confirmed directly from the installed `langsmith` source (not
docs): no LangSmith account, no API key, no network call in this mode.
**Found live, load-bearing:** if `opentelemetry-sdk`/`-api` aren't actually
installed, `langsmith`'s `Client` silently falls back to real
network-based tracing instead of failing loudly — installing them first is
what actually keeps this local-only, not the mode flag by itself.

**Phase 5.0.1 wired this into exactly one call site on purpose** —
`src/pipeline.py::run_turn`'s own root span (`SpanKind.SERVER`, wrapping
the graph invocation, with the Intent Router's classification result added
as a manual `gift.intent` attribute) — piloted on the simplest agent
(Intent Router: one LLM call, zero tools, no loop) before touching the
Gift-Picker/Checkout Info/Confirm agents. **What showed up underneath was
far more than that one call site, though:** the LangChain/LangGraph OTel
bridge auto-instruments the ENTIRE compiled graph for free — every node
(`intent_router`, `gift_picker`, its own `tools`/`model` spans down to
individual tool calls like `kapruka_search_products`, `LangGraph`,
`concierge_orchestrator`) gets its own span with real
`gen_ai.usage.input_tokens`/`output_tokens`/`total_tokens`,
`gen_ai.request.model`, `gen_ai.system` — no extra code needed per agent.

**Three genuine bridge limitations found live, each verified directly
rather than assumed, and each either fixed or deliberately accepted rather
than silently left broken:**
1. **Fixed:** the bridge sets `gen_ai.prompt`/`gen_ai.completion` as raw
   `bytes`, which crashed `ConsoleSpanExporter`'s default JSON
   serialization and silently dropped every bridge-generated span from
   console output (only this module's own manually-created spans, with no
   bytes attributes, were ever printing). Root-caused with a diagnostic
   `SpanProcessor.on_end` inspecting real attribute types, not guessed
   from the traceback. Fixed via `_console_safe_formatter`, which decodes
   bytes before delegating to the real `to_json()`.
2. **Accepted, not fixed — a real trade-off, not an oversight:**
   `turn_context`'s baggage reaches this module's own spans but not the
   bridge's, because `langsmith.Client`'s default `auto_batch_tracing=True`
   creates those spans from a background thread that never inherits the
   calling context. A fix (`auto_batch_tracing=False`) was tried and
   rejected live: it also bypassed the client's otel-only network guard
   and fired real authenticated requests at `api.smith.langchain.com`
   (confirmed via real `401` errors) — preserving "no network call, ever"
   was judged more important than complete baggage coverage.
3. **Accepted, flagged as follow-on work:** the bridge never calls
   `span.set_status(ERROR)`/`record_exception()` on a span that actually
   failed (verified live with a deliberately invalid model name) — the
   failure text lands as unstructured JSON inside `gen_ai.completion`, but
   the span's own OTel status stays `OK`. Only spans this module creates
   itself get correctly marked. Sharpens (doesn't just restate) the
   already-planned future work of adding `record_exception`/`set_status`
   calls — turns out to be needed for the auto-instrumented layer too, not
   only the hand-written spans planned for the deterministic pipeline
   (raw MCP client, DB writes, compaction) once those get instrumented.

See `PLAN.md`'s Phase 5.0.0/5.0.1 entries for the full verification detail
and exact commands run.

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
