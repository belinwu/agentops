from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from opentelemetry import trace
from opentelemetry.context import Context, attach, detach, set_value
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.trace import Status, StatusCode

from agentops.event import ErrorEvent
from agentops.helpers import get_ISO_time

from .encoders import EventToSpanEncoder


class SessionSpanProcessor(SpanProcessor):
    """Processes spans for AgentOps sessions and their related events.

    Responsibilities:
    1. Add session context to spans
    2. Track event counts
    3. Handle error propagation
    4. Export in-flight spans periodically
    5. Forward spans to wrapped processor

    Architecture:
        SessionSpanProcessor
            |
            |-- Session Context
            |-- Event Counting
            |-- Error Handling
            |-- In-flight Tracking
            |-- Wrapped Processor
    """

    def __init__(self, session_id: UUID, processor: SpanProcessor):
        self.session_id = session_id
        self.processor = processor
        self.event_counts = {"llms": 0, "tools": 0, "actions": 0, "errors": 0, "apis": 0}

        # Track in-flight spans
        self._in_flight: Dict[int, Span] = {}
        self._lock = Lock()
        self._stop_event = Event()
        self._export_thread = Thread(target=self._export_periodically, daemon=True)
        self._export_thread.start()

    def _export_periodically(self) -> None:
        """Export in-flight spans periodically"""
        while not self._stop_event.is_set():
            time.sleep(1)  # Export every second
            with self._lock:
                to_export = [span for span in self._in_flight.values()]
                if to_export:
                    for span in to_export:
                        self.processor.on_end(span)

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        """Process span start, adding session context and tracking in-flight spans.

        Args:
            span: The span being started
            parent_context: Optional parent context
        """
        if not span.is_recording() or not hasattr(span, "context") or span.context is None:
            return

        # Add entity context
        token = set_value("entity.id", str(self.session_id))
        try:
            token = attach(token)

            # Add common attributes
            span.set_attributes(
                {
                    "session.id": str(self.session_id),
                    "event.timestamp": get_ISO_time(),
                }
            )

            # Update event counts if this is an AgentOps event
            if hasattr(span, "attributes") and span.attributes is not None:
                event_type = span.attributes.get("event.type")
                if event_type in self.event_counts:
                    self.event_counts[event_type] += 1

            # Track in-flight span
            with self._lock:
                self._in_flight[span.context.span_id] = span

            # Forward to wrapped processor
            self.processor.on_start(span, parent_context)
        finally:
            detach(token)

    def on_end(self, span: ReadableSpan) -> None:
        """Process span end, handling error events and forwarding to wrapped processor.

        Args:
            span: The span being ended
        """
        if not span.context:
            return

        if not span.context.trace_flags.sampled:
            return

        # Remove from in-flight tracking
        with self._lock:
            self._in_flight.pop(span.context.span_id, None)

        # Handle error events by updating the current span
        if hasattr(span, "attributes") and span.attributes is not None:
            if "error" in span.attributes:
                current_span = trace.get_current_span()
                if current_span and current_span.is_recording():
                    current_span.set_status(Status(StatusCode.ERROR))
                    for key, value in span.attributes.items():
                        if key.startswith("error."):
                            current_span.set_attribute(key, value)

        # Forward to wrapped processor
        self.processor.on_end(span)

    def shutdown(self) -> None:
        """Shutdown the processor and stop periodic exports."""
        self._stop_event.set()
        self._export_thread.join()
        self.processor.shutdown()

    def force_flush(self, timeout_millis: Optional[int] = 30000) -> bool:
        """Force flush the processor.

        Args:
            timeout_millis: Timeout in milliseconds, defaults to 30000

        Returns:
            bool: True if flush succeeded
        """
        # Use default timeout if None provided
        timeout = 30000 if timeout_millis is None else timeout_millis
        return self.processor.force_flush(timeout)
