"""OpenTelemetry tracing (§16.2) -> Cloud Trace.

One trace per submission: ingestion stores the W3C trace context on the submission row, and every
later stage (other process, other service, after a crash) starts its span with that parent. So
Cloud Trace shows the whole path - ingestion, profile, fetch, evidence, retrieval, every ADK agent
and model call, persistence, Sheets - under one trace ID.

Attributes are an allow-list of IDs and metrics. Never source code, prompts, tokens or personal data.
Exporter: OTEL_TRACES_EXPORTER=gcp (Cloud Run) | console | none (default; tests use in-memory).
"""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span

ALLOWED = {
    "tenant_id",
    "event_id",
    "submission_id",
    "evaluation_id",
    "correlation_id",
    "stage",
    "agent",
    "model",
    "team_version",
    "commit",
    "files",
    "findings",
    "chunks",
    "hits",
    "claims",
    "tokens",
    "cost_usd",
    "status",
    "error_category",
    "tabs_written",
    "overrides",
    "attempt",
}
_provider: TracerProvider | None = None


def setup(service: str) -> TracerProvider:
    """Install the global tracer provider once per process (ADK's own spans attach to it too)."""
    global _provider
    if _provider is None:
        _provider = TracerProvider(resource=Resource.create({"service.name": service}))
        kind = os.environ.get("OTEL_TRACES_EXPORTER", "none")
        if kind == "gcp":
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            _provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter()))
        elif kind == "console":
            _provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(_provider)
    return _provider


def add_exporter(exporter: SpanExporter) -> None:
    """Tests: attach an in-memory exporter to the process provider."""
    setup("test").add_span_processor(SimpleSpanProcessor(exporter))


def _clean(attrs: Mapping[str, Any]) -> dict[str, Any]:
    return {
        f"jb.{k}": (v if isinstance(v, int | float | bool) else str(v))
        for k, v in attrs.items()
        if k in ALLOWED and v is not None
    }


@contextmanager
def span(name: str, parent: Mapping[str, str] | None = None, **attrs: Any) -> Iterator[Span]:
    ctx = propagate.extract(dict(parent)) if parent else None
    with trace.get_tracer("judgebench").start_as_current_span(name, context=ctx, attributes=_clean(attrs)) as s:
        yield s


def annotate(**attrs: Any) -> None:
    trace.get_current_span().set_attributes(_clean(attrs))


def carrier() -> dict[str, str]:
    """W3C traceparent of the current span, to persist on the submission row."""
    out: dict[str, str] = {}
    propagate.inject(out)
    return out
