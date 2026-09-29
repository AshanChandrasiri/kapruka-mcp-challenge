"""Phase 5.0.0 check: OpenTelemetry scaffolding in isolation — is the
provider/exporter/bridge configuration correct, as a separate question from
whether the LangChain/LangGraph bridge correctly captures a real agent
(that's Phase 5.0.1's own check, via main.py).

Run: .venv/Scripts/python.exe scripts/check_otel.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src import observability


def main() -> None:
    observability.configure()
    tracer = observability.get_tracer()

    print("--- opening one manual span ---")
    with observability.turn_context("+94_check_otel", "check-otel-thread"):
        with tracer.start_as_current_span("check_otel.manual_span") as span:
            span.set_attribute("check.fake_attribute", "hello-otel")

    observability.flush()
    print("--- done: a span named 'check_otel.manual_span' with "
          "check.fake_attribute='hello-otel' and phone_number/thread_id "
          "baggage attributes should have printed above ---")


if __name__ == "__main__":
    main()
