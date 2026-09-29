"""Phase 5.0.0: reusable OpenTelemetry scaffolding — console-output only,
no Langfuse/OTLP export yet. Every agent (Intent Router as of Phase 5.0.1;
Gift-Picker/Checkout Info/Confirm Agent in a later sub-phase) reuses this
same setup rather than each reinventing it.

`LANGSMITH_TRACING_MODE=otel` (confirmed directly from the installed
langsmith package's source, not docs — see PLAN.md Phase 5.0.0) routes
LangChain/LangGraph's own run-tracing through this same TracerProvider,
entirely locally: no LangSmith account or API key involved, no network
call (`Client.info` explicitly skips its API call in otel-only mode).
`LANGSMITH_TRACING=true` is also required — `LANGSMITH_TRACING_MODE` only
picks a *destination*, it doesn't turn tracing on by itself
(`langsmith.utils.tracing_is_enabled` checks `LANGSMITH_TRACING`/
`LANGSMITH_TRACING_V2` independently of the mode).

**Found while implementing, not assumed from reading the docs:** if the
`opentelemetry` packages aren't actually installed, langsmith's `Client`
silently falls back to real network-based "langsmith" tracing mode instead
of failing loudly (confirmed in the installed source,
`langsmith/client.py::Client.__init__`'s `except ImportError` branch) — so
`opentelemetry-sdk`/`opentelemetry-api` must genuinely be installed before
`LANGSMITH_TRACING` is ever turned on here, not just assumed present.

No built-in "copy baggage onto every new span's attributes" processor
ships in `opentelemetry-sdk` (checked directly — no such submodule) —
`_BaggageSpanProcessor` below is a small hand-rolled version of the
standard OTel recipe for this, not a third-party dependency.

**Found live while verifying Phase 5.0.1, not anticipated in the plan:**
the langsmith OTel bridge sets `gen_ai.prompt`/`gen_ai.completion` as raw
`bytes` (a JSON-encoded payload, never decoded to `str`) on EVERY span it
creates for EVERY LangGraph node — not just the model call, the whole
graph is traced automatically, far more granular than "one nested span
for the Intent Router's own LLM call." `ConsoleSpanExporter`'s default
`formatter` calls `span.to_json()`, which does a plain `json.dumps` with
no `bytes` handling — so it raised and silently dropped every one of
those spans from console output (OTel's own SDK logs "Exception while
exporting Span" per dropped span but keeps running); only this module's
own manually-created spans, which never carry a bytes-valued attribute,
were printing. `_console_safe_formatter` below decodes bytes attributes
(utf-8, falling back to `repr()` if that fails) before delegating to the
real `to_json()` — found and fixed by directly inspecting a real span's
attributes live (a diagnostic `SpanProcessor.on_end` that reported each
attribute's Python type), not guessed from a traceback alone.

**Two more genuine limitations found live, accepted rather than papered
over:**

1. **`turn_context`'s baggage does not reach the langsmith-bridge-generated
   spans** (only this module's own manually-created spans, e.g.
   `run_turn`'s). Root cause, confirmed by directly inspecting
   `threading.current_thread()` inside the bridge's own span-creation
   call: `langsmith.Client` (with its default `auto_batch_tracing=True`)
   creates those spans from a background `tracing_control_thread_func`
   thread, which never inherits the calling thread/task's attached
   `contextvars` context — baggage genuinely is thread-local propagation,
   not magic. **A tempting fix was tried and rejected**: constructing a
   `Client(auto_batch_tracing=False, tracing_mode="otel")` and installing
   it via `langsmith.run_trees.configure(client=...)` does make spans get
   created on the calling thread (so baggage WOULD reach them) — but it
   also made the client bypass its own otel-only network guard and fire
   real authenticated HTTP requests at `api.smith.langchain.com` (confirmed
   live: real 401 Unauthorized errors, meaning a real outbound network
   call). Preserving "no network call, ever" was judged more important
   than complete baggage coverage, so the default `auto_batch_tracing=True`
   config is kept and this gap is accepted, not worked around.
2. **The bridge doesn't call `span.set_status(ERROR)`/`record_exception()`
   on the model-call span that actually failed** — verified live with a
   deliberately invalid model name: the failure text does end up embedded,
   as unstructured JSON, inside that span's `gen_ai.completion` attribute,
   but the span's own OTel status stays `OK`. Only spans this module
   creates itself (`run_turn`, via `start_as_current_span`'s own default
   exception handling) get a correct `ERROR` status + `exception` event.
   Sharpens, rather than duplicates, the already-planned "Explicitly
   deferred" `record_exception`/`set_status` work — that work turns out to
   be needed for the auto-instrumented spans too, not just the hand-written
   ones over the deterministic pipeline.
"""

from contextlib import contextmanager
from typing import Iterator

from opentelemetry import baggage as otel_baggage
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

_TRACER_NAME = "kapruka-gift-concierge"

_provider: TracerProvider | None = None


class _BaggageSpanProcessor(SpanProcessor):
    """Copies every current baggage entry onto a span's attributes the
    moment it starts — this is what makes turn_context's phone_number/
    thread_id baggage actually visible on spans (baggage by itself only
    propagates through context, it isn't automatically rendered as
    attributes anywhere).
    """

    def on_start(self, span: Span, parent_context=None) -> None:
        for key, value in otel_baggage.get_all(parent_context).items():
            span.set_attribute(key, value)

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _decode_bytes_attributes(attributes) -> dict | None:
    if attributes is None:
        return None
    decoded = {}
    for key, value in dict(attributes).items():
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError:
                value = repr(value)
        decoded[key] = value
    return decoded


def _console_safe_formatter(span: ReadableSpan) -> str:
    """ConsoleSpanExporter's own default formatter (`span.to_json()`)
    crashes on the langsmith bridge's bytes-valued gen_ai.prompt/completion
    attributes — see this module's own docstring. `to_json()` has no hook
    to override how attributes are serialized, so this swaps the span's
    attributes for a decoded copy just for the duration of this call, then
    restores the original — safe here since SimpleSpanProcessor exports
    synchronously, one span at a time, not concurrently.
    """
    original = span._attributes  # noqa: SLF001
    span._attributes = _decode_bytes_attributes(original)  # noqa: SLF001
    try:
        return span.to_json() + "\n"
    finally:
        span._attributes = original  # noqa: SLF001


def configure() -> None:
    """Set up the global TracerProvider (console-output only) and route
    LangChain/LangGraph's own tracing through it via langsmith's OTEL
    bridge. Call once, at process startup, before any agent runs — idempotent,
    safe to call more than once (e.g. accidentally, from a test script).
    """
    global _provider
    if _provider is not None:
        return

    import os

    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_TRACING_MODE", "otel")

    provider = TracerProvider()
    provider.add_span_processor(_BaggageSpanProcessor())
    provider.add_span_processor(
        SimpleSpanProcessor(ConsoleSpanExporter(formatter=_console_safe_formatter))
    )
    trace.set_tracer_provider(provider)
    _provider = provider


def flush() -> None:
    """Force-flush the provider before process exit. Less critical with
    SimpleSpanProcessor (each span is already exported the instant it
    completes) than it would be with a batched exporter, but kept as cheap
    insurance — and it becomes load-bearing again the moment this moves to
    a batched, networked exporter (e.g. Langfuse via OTLP).
    """
    if _provider is not None:
        _provider.force_flush()


def get_tracer() -> trace.Tracer:
    """One named tracer, used everywhere from here on instead of each
    module creating its own.
    """
    return trace.get_tracer(_TRACER_NAME)


@contextmanager
def turn_context(phone_number: str, thread_id: str) -> Iterator[None]:
    """Attaches phone_number/thread_id as OpenTelemetry baggage for the
    duration of one turn — every span created within it (across whichever
    agent or tool runs, e.g. src/pipeline.py::run_turn's own root span
    opened *inside* this context manager) picks them up automatically via
    _BaggageSpanProcessor above, with nothing threaded through function
    signatures.
    """
    ctx = otel_baggage.set_baggage("phone_number", phone_number)
    ctx = otel_baggage.set_baggage("thread_id", thread_id, context=ctx)
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)
