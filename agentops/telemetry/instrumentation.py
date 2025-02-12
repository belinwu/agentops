from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Type, Union
from uuid import UUID, uuid4
import json

from opentelemetry import trace
from opentelemetry.context import attach, detach, set_value
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, Sampler, TraceIdRatioBased
from termcolor import colored

from agentops.config import TESTING, Configuration
from agentops.helpers import get_ISO_time, safe_serialize
from agentops.http_client import HttpClient
from agentops.log_config import logger
from agentops.session.encoders import EventToSpanEncoder
from agentops.session.exporters import EventExporter, SessionLogExporter
from agentops.session.signals import event_recorded, session_ended, session_started, session_updated

if TYPE_CHECKING:
    from agentops.client import Client
    from agentops.event import ErrorEvent, Event, EventType
    from agentops.session.session import Session

"""
This module handles OpenTelemetry instrumentation setup for AgentOps sessions.

Each AgentOps session requires its own telemetry setup to:
1. Track session-specific logs
2. Export logs to the AgentOps backend
3. Maintain isolation between different sessions running concurrently

The module uses a session-specific TracerProvider architecture where each session gets its own:
- TracerProvider: For session-specific resource attribution and sampling
- Tracer: For creating spans within the session's context
- SpanProcessor: For independent export pipeline configuration

This architecture enables:
- Complete isolation between concurrent sessions
- Independent lifecycle management
- Session-specific export configurations
- Easier debugging and monitoring per session

The module provides functions to:
- Set up logging telemetry components for a new session
- Clean up telemetry components when a session ends
"""

# Map of session_id to LoggingHandler
_session_handlers: Dict[UUID, LoggingHandler] = {}

# Map of session_id to session-specific telemetry components
# Each session gets its own TracerProvider, Tracer, and SpanProcessor for:
# - Isolation between concurrent sessions
# - Independent lifecycle management
# - Session-specific export configurations
# - Easier debugging and monitoring
_session_tracers: Dict[UUID, Tuple[TracerProvider, trace.Tracer, SpanProcessor]] = {}


def get_session_handler(session_id: UUID) -> Optional[LoggingHandler]:
    """Get the logging handler for a specific session.

    Args:
        session_id: The UUID of the session

    Returns:
        The session's LoggingHandler if it exists, None otherwise
    """
    return _session_handlers.get(session_id)


def set_session_handler(session_id: UUID, handler: Optional[LoggingHandler]) -> None:
    """Set or remove the logging handler for a session.

    Args:
        session_id: The UUID of the session
        handler: The handler to set, or None to remove
    """
    if handler is None:
        _session_handlers.pop(session_id, None)
    else:
        _session_handlers[session_id] = handler


def _setup_trace_provider(session_id: UUID, config: Configuration) -> Tuple[TracerProvider, trace.Tracer]:
    """Set up trace provider for a session.

    Creates a session-specific TracerProvider and Tracer to maintain isolation between
    concurrent sessions. This ensures that telemetry data from different sessions
    doesn't get mixed up and can be properly attributed and managed independently.

    Args:
        session_id: UUID identifier for the session
        config: Configuration instance with telemetry settings

    Returns:
        Tuple containing:
        - TracerProvider: Session-specific provider instance with proper resource attribution
        - Tracer: Session-specific tracer for creating spans in this session's context
    """
    resource = Resource.create({SERVICE_NAME: f"agentops.session.{str(session_id)}", "session.id": str(session_id)})
    provider = TracerProvider(resource=resource)
    tracer = provider.get_tracer(f"agentops.session.{str(session_id)}")
    return provider, tracer


def get_processor_cls() -> Type[SpanProcessor]:
    """Get the appropriate SpanProcessor class based on environment.

    Returns SimpleSpanProcessor for tests, BatchSpanProcessor otherwise.

    Returns:
        Type[SpanProcessor]: The SpanProcessor class to use
    """
    return BatchSpanProcessor


def _setup_span_processor(session_id: UUID, config: Configuration) -> SpanProcessor:
    """Set up span processor for a session.

    Creates a session-specific SpanProcessor that handles the export pipeline for
    this session's telemetry data. This allows for:
    - Independent export configurations per session
    - Isolated error handling (issues in one session don't affect others)
    - Clean shutdown when the session ends

    Args:
        session_id: UUID identifier for the session
        config: Configuration instance with telemetry settings

    Returns:
        SpanProcessor: Session-specific processor instance
    """
    from agentops.session.registry import get_session_by_id

    # Set up exporter
    exporter = EventExporter(session=get_session_by_id(session_id))

    # Get appropriate processor class
    processor_cls = get_processor_cls()

    if processor_cls == SimpleSpanProcessor:
        return processor_cls(exporter)

    # For BatchSpanProcessor, we need to configure the batch settings
    return BatchSpanProcessor(
        exporter,
        schedule_delay_millis=config.max_wait_time,
        max_queue_size=config.max_queue_size,
        max_export_batch_size=min(
            max(config.max_queue_size // 20, 1),
            min(config.max_queue_size, 32),
        ),
    )


def setup_session_tracer(session_id: UUID, config: Optional[Configuration] = None) -> trace.Tracer:
    """Set up OpenTelemetry tracing components for a session.

    This function orchestrates the creation of all session-specific telemetry components:
    - TracerProvider: For session-specific resource attribution
    - Tracer: For creating spans within the session's context
    - SpanProcessor: For independent export pipeline configuration

    The components are stored in _session_tracers for lifecycle management.

    Args:
        session_id: UUID identifier for the session
        config: Configuration instance with telemetry settings

    Returns:
        Tracer: Session-specific tracer for creating spans
    """
    if config is None:
        config = Configuration()

    # Set up provider and tracer
    provider, tracer = _setup_trace_provider(session_id, config)

    # Set up processor
    processor = _setup_span_processor(session_id, config)
    provider.add_span_processor(processor)

    # Store components for cleanup
    _session_tracers[session_id] = (provider, tracer, processor)

    return tracer


def cleanup_session_tracer(session_id: UUID) -> None:
    """Clean up tracing components for a session.

    Handles the graceful shutdown of a session's telemetry components:
    - Flushes any pending telemetry data
    - Shuts down the span processor
    - Shuts down the tracer provider

    This ensures clean session termination without affecting other active sessions.

    Args:
        session_id: UUID identifier for the session
    """
    if components := _session_tracers.pop(session_id, None):
        provider, _, processor = components
        try:
            processor.force_flush(timeout_millis=5000)
            processor.shutdown()
            provider.shutdown()
        except Exception as e:
            logger.warning(f"Error during tracer cleanup: {e}")


def setup_session_telemetry(session_id: UUID, log_exporter) -> tuple[LoggingHandler, BatchLogRecordProcessor]:
    """Set up OpenTelemetry logging components for a new session.

    Args:
        session_id: UUID identifier for the session, used to tag telemetry data
        log_exporter: SessionLogExporter instance that handles sending logs to AgentOps backend

    Returns:
        Tuple containing:
        - LoggingHandler: Handler that should be added to the logger
        - BatchLogRecordProcessor: Processor that batches and exports logs
    """
    # Create logging components
    resource = Resource.create({SERVICE_NAME: f"agentops.session.{str(session_id)}"})
    logger_provider = LoggerProvider(resource=resource)

    # Create processor and handler
    log_processor = BatchLogRecordProcessor(log_exporter)
    logger_provider.add_log_record_processor(log_processor)

    log_handler = LoggingHandler(
        level=logging.INFO,
        logger_provider=logger_provider,
    )

    # Register handler with session
    set_session_handler(session_id, log_handler)

    return log_handler, log_processor


def cleanup_session_telemetry(log_handler: LoggingHandler, log_processor: BatchLogRecordProcessor) -> None:
    """Clean up OpenTelemetry logging components when a session ends.

    This function ensures proper cleanup by:
    1. Removing the handler from the logger
    2. Closing the handler to free resources
    3. Flushing any pending logs in the processor
    4. Shutting down the processor
    5. Disconnecting signal handlers

    Args:
        log_handler: The session's LoggingHandler to be removed and closed
        log_processor: The session's BatchLogRecordProcessor to be flushed and shutdown
    """
    try:
        # Remove and close handler
        logger.removeHandler(log_handler)
        log_handler.close()

        # Remove from session handlers
        for session_id, handler in list(_session_handlers.items()):
            if handler is log_handler:
                set_session_handler(session_id, None)
                break

        # Shutdown processor
        log_processor.force_flush(timeout_millis=5000)
        log_processor.shutdown()
    except Exception as e:
        logger.warning(f"Error during logging cleanup: {e}")


def _on_session_start(sender):
    """Initialize session tracer when session starts"""
    # Initialize tracer when session starts - this is the proper time
    tracer = setup_session_tracer(sender.session_id, sender.config)
    sender._tracer = tracer

    if not tracer:
        logger.error("Failed to initialize tracer")
        return

    # Record session start span
    with tracer.start_as_current_span(
        name="session.start",
        attributes={
            "session.id": str(sender.session_id),
            "session.tags": ",".join(sender.tags) if sender.tags else "",
            "session.init_timestamp": sender.init_timestamp,
        },
    ) as span:
        span.set_attribute("session.start", True)


def _on_session_end(sender, end_state: str, end_state_reason: Optional[str]):
    """Clean up tracer when session ends"""
    # By this point tracer should exist since session was started
    if not sender._tracer:
        logger.error("No tracer found during session end")
        return

    with sender._tracer.start_as_current_span(
        name="session.end",
        attributes={
            "session.id": str(sender.session_id),
            "session.end_state": end_state,
            "session.end_state_reason": end_state_reason or "",
            "session.end_timestamp": sender.end_timestamp or get_ISO_time(),
        },
    ) as span:
        span.set_attribute("session.end", True)

    cleanup_session_tracer(sender.session_id)


def _on_session_event_recorded(sender: Session, event: Event, flush_now=False, **kwargs):
    """Handle completion of event recording for telemetry"""
    if not sender._tracer:
        return

    # Create spans within the session's trace context
    with trace.use_span(sender.span):
        span_definitions = EventToSpanEncoder.encode(event)
        
        for span_def in span_definitions:
            with sender._tracer.start_as_current_span(
                name=span_def.name,
                kind=span_def.kind,
                attributes=span_def.attributes,
            ) as span:
                event.end_timestamp = get_ISO_time()
                
                # Update event counts
                event_type = span_def.attributes.get("event_type")
                if event_type in sender.event_counts:
                    sender.event_counts[event_type] += 1

    if flush_now:
        flush_session_telemetry(sender.session_id)


def register_handlers():
    """Register signal handlers"""
    # Disconnect signal handlers to ensure clean state
    unregister_handlers()
    import agentops.event  # Ensure event.py handlers are registered before instrumentation.py handlers

    session_started.connect(_on_session_start)
    session_ended.connect(_on_session_end)
    event_recorded.connect(_on_session_event_recorded)


def unregister_handlers():
    """Unregister signal handlers"""
    session_started.disconnect(_on_session_start)
    session_ended.disconnect(_on_session_end)
    event_recorded.disconnect(_on_session_event_recorded)


def flush_session_telemetry(session_id: UUID) -> bool:
    """Force flush any pending telemetry data for a session.

    Args:
        session_id: The UUID of the session to flush

    Returns:
        bool: True if flush was successful, False if components not found
    """
    components = _session_tracers.get(session_id)
    if not components:
        return False

    _, _, processor = components
    processor.force_flush()
    return True


register_handlers()

# class SessionSpanProcessor(BatchSpanProcessor):
#     def on_start(self, span, parent_context):
#         if span.attributes.get("class") == "Session":
#             super().on_start(span, parent_context)

#     def on_end(self, span):
#         if span.attributes.get("class") == "Session":
#             super().on_end(span)

# class EventSpanProcessor(BatchSpanProcessor):
#     def on_start(self, span, parent_context):
#         if span.attributes.get("class") in ["ActionEvent", "LLMEvent", "ToolEvent", "ErrorEvent"]:
#             super().on_start(span, parent_context)

#     def on_end(self, span):
#         if span.attributes.get("class") in ["ActionEvent", "LLMEvent", "ToolEvent", "ErrorEvent"]:
#             super().on_end(span)

# # Setup providers with different processors
# provider = TracerProvider()
# provider.add_span_processor(SessionSpanProcessor(session_exporter))
# provider.add_span_processor(EventSpanProcessor(event_exporter))
