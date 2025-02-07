from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Dict, List, Optional, Union
from uuid import UUID, uuid4

from opentelemetry import trace
from opentelemetry.context import attach, detach, set_value
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, Sampler, TraceIdRatioBased
from termcolor import colored

from agentops.config import Configuration
from agentops.event import ErrorEvent, Event, EventType
from agentops.helpers import get_ISO_time, safe_serialize
from agentops.http_client import HttpClient
from agentops.log_config import logger
from agentops.session.events import event_recorded, session_ended, session_started, session_updated
from agentops.session.exporters import SessionExporter, SessionLogExporter

if TYPE_CHECKING:
    from agentops.client import Client

"""
This module handles OpenTelemetry instrumentation setup for AgentOps sessions.

Each AgentOps session requires its own telemetry setup to:
1. Track session-specific logs
2. Export logs to the AgentOps backend
3. Maintain isolation between different sessions running concurrently

The module provides functions to:
- Set up logging telemetry components for a new session
- Clean up telemetry components when a session ends
"""

# Map of session_id to LoggingHandler
_session_handlers: Dict[UUID, LoggingHandler] = {}


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

    Args:
        log_handler: The session's LoggingHandler to be removed and closed
        log_processor: The session's BatchLogRecordProcessor to be flushed and shutdown

    Used by:
        Session.end_session() to clean up logging components when the session ends
    """
    from agentops.log_config import logger

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


class SessionTracer:
    """Manages OpenTelemetry tracing for a session"""

    def __init__(self, session_id: UUID, config: Configuration):
        # Create session-specific resource and tracer
        resource = Resource.create({SERVICE_NAME: f"agentops.session.{str(session_id)}", "session.id": str(session_id)})
        self.tracer_provider = TracerProvider(resource=resource)
        self.tracer = self.tracer_provider.get_tracer(f"agentops.session.{str(session_id)}")

        from agentops.session.registry import get_session_by_id

        # Set up exporter
        self.exporter = SessionExporter(session=get_session_by_id(session_id))
        self.span_processor = BatchSpanProcessor(
            self.exporter,
            max_queue_size=config.max_queue_size,
            schedule_delay_millis=config.max_wait_time,
            max_export_batch_size=min(
                max(config.max_queue_size // 20, 1),
                min(config.max_queue_size, 32),
            ),
            export_timeout_millis=20000,
        )
        self.tracer_provider.add_span_processor(self.span_processor)

    def cleanup(self):
        """Clean up tracer resources"""
        if hasattr(self, "span_processor"):
            try:
                self.span_processor.force_flush(timeout_millis=5000)
                self.span_processor.shutdown()
            except Exception as e:
                logger.warning(f"Error during span processor cleanup: {e}")

        if hasattr(self, "exporter"):
            try:
                self.exporter.shutdown()
            except Exception as e:
                logger.warning(f"Error during exporter cleanup: {e}")


def _normalize_action_event(event_data: dict) -> None:
    """Normalize action event fields"""
    if "action_type" not in event_data and "name" in event_data:
        event_data["action_type"] = event_data["name"]
    elif "name" not in event_data and "action_type" in event_data:
        event_data["name"] = event_data["action_type"]
    else:
        event_data.setdefault("action_type", "unknown_action")
        event_data.setdefault("name", "unknown_action")


def _normalize_tool_event(event_data: dict) -> None:
    """Normalize tool event fields"""
    if "name" not in event_data and "tool_name" in event_data:
        event_data["name"] = event_data["tool_name"]
    elif "tool_name" not in event_data and "name" in event_data:
        event_data["tool_name"] = event_data["name"]
    else:
        event_data.setdefault("name", "unknown_tool")
        event_data.setdefault("tool_name", "unknown_tool")


@session_started.connect
def on_session_start(sender):
    """Initialize session tracer when session starts"""
    breakpoint()
    tracer = SessionTracer(sender.session_id, sender.config)
    sender._tracer = tracer
    # The tracer provider is accessed through the tracer object
    # No need to set it separately on the session

    with sender._tracer.tracer.start_as_current_span(
        name="session.start",
        attributes={
            "session.id": str(sender.session_id),
            "session.tags": ",".join(sender.tags) if sender.tags else "",
            "session.init_timestamp": sender.init_timestamp,
        },
    ) as span:
        span.set_attribute("session.start", True)


@session_ended.connect
def on_session_end(sender, end_state: str, end_state_reason: Optional[str]):
    """Clean up tracer when session ends"""
    assert getattr(sender, "_tracer", None) is not None, "Tracer not initialized"

    with sender._tracer.tracer.start_as_current_span(
        name="session.end",
        attributes={
            "session.id": str(sender.session_id),
            "session.end_state": end_state,
            "session.end_state_reason": end_state_reason or "",
            "session.end_timestamp": sender.end_timestamp or get_ISO_time(),
        },
    ) as span:
        span.set_attribute("session.end", True)

    sender._tracer.cleanup()


@event_recorded.connect
def on_event_record(sender, event: Union[Event, ErrorEvent], flush_now: bool = False):
    logger.debug(f"Event recorded: {event}")
    """Create span for recorded event"""
    assert getattr(sender, "_tracer", None) is not None, "Tracer not initialized"

    # Ensure event has required attributes
    if not hasattr(event, "id"):
        event.id = uuid4()
    if not hasattr(event, "init_timestamp"):
        event.init_timestamp = get_ISO_time()
    if not hasattr(event, "end_timestamp") or event.end_timestamp is None:
        event.end_timestamp = get_ISO_time()

    # Create event span
    event_data = {k: v for k, v in event.__dict__.items() if v is not None}

    # Add type-specific fields
    if isinstance(event, ErrorEvent):
        event_data["error_type"] = getattr(event, "error_type", event.event_type)
    elif isinstance(event.event_type, EventType):
        if event.event_type == EventType.ACTION:
            _normalize_action_event(event_data)
        elif event.event_type == EventType.TOOL:
            _normalize_tool_event(event_data)

    event_type = event.event_type.value if isinstance(event.event_type, EventType) else str(event.event_type)

    with sender._tracer.tracer.start_as_current_span(
        name=event_type,
        attributes={
            "event.id": str(event.id),
            "event.type": event_type,
            "event.timestamp": event.init_timestamp,
            "event.end_timestamp": event.end_timestamp,
            "session.id": str(sender.session_id),
            "session.tags": ",".join(sender.tags) if sender.tags else "",
            "event.data": safe_serialize(event_data),
        },
    ):
        if event_type in sender.event_counts:
            sender.event_counts[event_type] += 1

    # Handle manual flush if requested
    if flush_now:
        sender._tracer.span_processor.force_flush()
