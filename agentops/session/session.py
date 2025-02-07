from __future__ import annotations

import functools
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from uuid import UUID, uuid4

from blinker import Signal

# from opentelemetry import trace
# from opentelemetry.context import attach, detach, set_value
# from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler, LogRecord
# from opentelemetry.sdk._logs._internal import LogData
# from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogExporter, LogExportResult
# from opentelemetry.sdk.resources import SERVICE_NAME, Resource
# from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
# from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter, SpanExportResult
from opentelemetry.trace import Tracer
from termcolor import colored

from agentops.config import TESTING, Configuration
from agentops.event import ErrorEvent, Event
from agentops.exceptions import ApiServerException
from agentops.helpers import filter_unjsonable, get_ISO_time, safe_serialize
from agentops.http_client import HttpClient, Response
from agentops.instrumentation import cleanup_session_telemetry, setup_session_telemetry
from agentops.log_config import logger
from agentops.session.events import (
    event_recorded,
    session_ended,
    session_ending,
    session_initialized,
    session_initializing,
    session_started,
    session_starting,
    session_updated,
)
from agentops.session.registry import add_session, remove_session

from .exporters import SessionExporter, SessionLogExporter


class EndState(Enum):
    """
    Enum representing the possible end states of a session.

    Attributes:
        SUCCESS: Indicates the session ended successfully.
        FAIL: Indicates the session failed.
        INDETERMINATE (default): Indicates the session ended with an indeterminate state.
                       This is the default state if not specified, e.g. if you forget to call end_session()
                       at the end of your program or don't pass it the end_state parameter
    """

    SUCCESS = "Success"
    FAIL = "Fail"
    INDETERMINATE = "Indeterminate"  # Default


@dataclass
class Session:
    """Data container for session state with minimal public API"""

    session_id: UUID
    config: Configuration
    tags: List[str] = field(default_factory=list)
    host_env: Optional[dict] = None
    token_cost: Decimal = field(default_factory=lambda: Decimal(0))
    end_state: str = field(default_factory=lambda: EndState.INDETERMINATE.value)
    end_state_reason: Optional[str] = None
    end_timestamp: Optional[str] = None
    jwt: Optional[str] = None
    video: Optional[str] = None
    event_counts: Dict[str, int] = field(
        default_factory=lambda: {"llms": 0, "tools": 0, "actions": 0, "errors": 0, "apis": 0}
    )
    init_timestamp: str = field(default_factory=get_ISO_time)
    is_running: bool = field(default=True)

    def __post_init__(self):
        """Initialize session components after dataclass initialization"""
        self._lock = threading.Lock()
        self._end_session_lock = threading.Lock()
        self._log_handler = None
        self._log_processor = None
        self._log_exporter = None
        self._tracer = None  # Initialize tracer attribute

        # Initialize session
        try:
            init_success = self._initialize()
        except Exception:
            init_success = False

        if not init_success:
            self.is_running = False

    def _cleanup(self):
        pass

    def _initialize(self) -> bool:
        """Initialize session components"""
        try:
            # Signal session is initializing
            session_initializing.send(self, session_id=self.session_id)

            # Signal session is initialized (this adds to registry)
            session_initialized.send(self, session_id=self.session_id)

            # Get JWT from API
            if not self._get_jwt():
                return False

            # Initialize logging
            if not self._setup_logging():
                return False

            logger.info(colored(f"\x1b[34mSession Replay: {self.session_url}\x1b[0m", "blue"))
            return True

        except Exception as e:
            if TESTING:
                raise e
            logger.error(f"Failed to initialize session: {e}")
            return False

    def _get_jwt(self) -> bool:
        """Get JWT from API server"""
        payload = {"session": self.__dict__}
        try:
            res = HttpClient.post(
                f"{self.config.endpoint}/v2/create_session",
                safe_serialize(payload).encode("utf-8"),
                api_key=self.config.api_key,
                parent_key=self.config.parent_key,
            )
            if not res:
                logger.error("Failed to get response from API server")
                return False

            if not (jwt := res.body.get("jwt")):
                logger.error("No JWT in API response")
                return False

            self.jwt = jwt
            return True

        except Exception as e:
            logger.error(f"Failed to get JWT: {e}")
            return False

    def _setup_logging(self) -> bool:
        """Set up logging for the session"""
        try:
            self._log_exporter = SessionLogExporter(session=self)
            self._log_handler, self._log_processor = setup_session_telemetry(self.session_id, self._log_exporter)
            logger.addHandler(self._log_handler)
            return True
        except Exception as e:
            logger.error(f"Failed to setup logging: {e}")
            return False

    def record(self, event: Union[Event, ErrorEvent], flush_now=False) -> None:
        """Record an event in this trace"""
        if not self.is_running:
            return

        # Emit event signal - OTEL instrumentation will catch this and create spans
        event_recorded.send(self, event=event, flush_now=flush_now)

    def add_tags(self, tags: List[str]) -> None:
        """
        Append to session tags at runtime.
        """
        if not self.is_running:
            return

        if not (isinstance(tags, list) and all(isinstance(item, str) for item in tags)):
            if isinstance(tags, str):
                tags = [tags]

        # Initialize tags if None
        if self.tags is None:
            self.tags = []

        # Add new tags that don't exist
        for tag in tags:
            if tag not in self.tags:
                self.tags.append(tag)

        # Update session state immediately
        self._update_session()

    def set_tags(self, tags):
        """Set session tags, replacing any existing tags"""
        if not self.is_running:
            return

        if not (isinstance(tags, list) and all(isinstance(item, str) for item in tags)):
            if isinstance(tags, str):
                tags = [tags]

        # Set tags directly
        self.tags = tags.copy()  # Make a copy to avoid reference issues

        # Update session state immediately
        self._update_session()

    def end(
        self,
        end_state: str = EndState.INDETERMINATE.value,
        end_state_reason: Optional[str] = None,
        video: Optional[str] = None,
    ) -> Union[Decimal, None]:
        with self._end_session_lock:
            if not self.is_running:
                return None

            if not any(end_state == state.value for state in EndState):
                logger.warning("Invalid end_state. Please use one of the EndState")
                return None

            try:
                # Signal session is ending
                session_ending.send(self, end_state=end_state, end_state_reason=end_state_reason)

                # Update trace state
                self.end_timestamp = get_ISO_time()
                self.end_state = end_state
                self.end_state_reason = end_state_reason
                if video is not None:
                    self.video = video

                # Clean up trace components
                self._cleanup()

                # Log final analytics
                if analytics_stats := self.get_analytics():
                    logger.info(
                        f"Session Stats - "
                        f"{colored('Duration:', attrs=['bold'])} {analytics_stats['Duration']} | "
                        f"{colored('Cost:', attrs=['bold'])} ${analytics_stats['Cost']} | "
                        f"{colored('LLMs:', attrs=['bold'])} {analytics_stats['LLM calls']} | "
                        f"{colored('Tools:', attrs=['bold'])} {analytics_stats['Tool calls']} | "
                        f"{colored('Actions:', attrs=['bold'])} {analytics_stats['Actions']} | "
                        f"{colored('Errors:', attrs=['bold'])} {analytics_stats['Errors']}"
                    )
                    logger.info(colored(f"\x1b[34mSession Replay: {self.session_url}\x1b[0m", "blue"))
                    return self.token_cost

            except Exception as e:
                logger.exception(f"Error during session end: {e}")
            finally:
                self.is_running = False
                session_ended.send(self, end_state=end_state, end_state_reason=end_state_reason)

            return None

    def _send_event(self, event):
        """Direct event sending for testing"""
        try:
            payload = {
                "events": [
                    {
                        "id": str(event.id),
                        "event_type": event.event_type,
                        "init_timestamp": event.init_timestamp,
                        "end_timestamp": event.end_timestamp,
                        "data": filter_unjsonable(event.__dict__),
                    }
                ]
            }

            HttpClient.post(
                f"{self.config.endpoint}/v2/create_events",
                json.dumps(payload).encode("utf-8"),
                jwt=self.jwt,
            )
        except Exception as e:
            logger.error(f"Failed to send event: {e}")

    def _reauthorize_jwt(self) -> Union[str, None]:
        with self._lock:
            payload = {"session_id": self.session_id}
            try:
                serialized_payload = safe_serialize(payload).encode("utf-8")
                res = HttpClient.post(
                    f"{self.config.endpoint}/v2/reauthorize_jwt",
                    serialized_payload,
                    self.config.api_key,
                )
                if not res:
                    return None
                jwt = res.body.get("jwt")
                self.jwt = jwt
                return jwt
            except Exception as e:
                logger.error(f"Failed to reauthorize JWT: {e}")
                return None

    def _start_session(self):
        """Start the session after initialization"""
        with self._lock:
            # Signal session is starting
            session_starting.send(self, session_id=self.session_id)

            # Set running state
            self.is_running = True

            # Signal session has started - this will initialize tracing via listeners
            session_started.send(self, session_id=self.session_id)

            return True

    def _update_session(self) -> None:
        """Update session state on the server"""
        if not self.is_running:
            return

        with self._lock:
            # Emit session updated signal
            session_updated.send(self, session_id=self.session_id)

            payload = {"session": self.__dict__}

            try:
                HttpClient.post(
                    f"{self.config.endpoint}/v2/update_session",
                    json.dumps(filter_unjsonable(payload)).encode("utf-8"),
                    # self.config.api_key,
                    jwt=self.jwt,
                )
            except ApiServerException as e:
                return logger.error(f"Could not update session - {e}")

    def create_agent(self, name, agent_id):
        if not self.is_running:
            return
        if agent_id is None:
            agent_id = str(uuid4())

        payload = {
            "id": agent_id,
            "name": name,
        }

        serialized_payload = safe_serialize(payload).encode("utf-8")
        try:
            HttpClient.post(
                f"{self.config.endpoint}/v2/create_agent",
                serialized_payload,
                api_key=self.config.api_key,
                jwt=self.jwt,
            )
        except ApiServerException as e:
            return logger.error(f"Could not create agent - {e}")

        return agent_id

    def patch(self, func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            kwargs["session"] = self
            return func(*args, **kwargs)

        return wrapper

    def _get_response(self) -> Optional[Response]:
        payload = {"session": self.__dict__}
        try:
            response = HttpClient.post(
                f"{self.config.endpoint}/v2/update_session",
                json.dumps(filter_unjsonable(payload)).encode("utf-8"),
                api_key=self.config.api_key,
                jwt=self.jwt,
            )
        except ApiServerException as e:
            return logger.error(f"Could not end session - {e}")

        logger.debug(response.body)
        return response

    def _format_duration(self, start_time, end_time) -> str:
        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        duration = end - start

        hours, remainder = divmod(duration.total_seconds(), 3600)
        minutes, seconds = divmod(remainder, 60)

        parts = []
        if hours > 0:
            parts.append(f"{int(hours)}h")
        if minutes > 0:
            parts.append(f"{int(minutes)}m")
        parts.append(f"{seconds:.1f}s")

        return " ".join(parts)

    def _get_token_cost(self, response: Response) -> Decimal:
        token_cost = response.body.get("token_cost", "unknown")
        if token_cost == "unknown" or token_cost is None:
            return Decimal(0)
        return Decimal(token_cost)

    def _format_token_cost(self, token_cost: Decimal) -> str:
        return (
            "{:.2f}".format(token_cost)
            if token_cost == 0
            else "{:.6f}".format(token_cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
        )

    def get_analytics(self) -> Optional[Dict[str, Any]]:
        if not self.end_timestamp:
            self.end_timestamp = get_ISO_time()

        formatted_duration = self._format_duration(self.init_timestamp, self.end_timestamp)

        if (response := self._get_response()) is None:
            return None

        self.token_cost = self._get_token_cost(response)

        return {
            "LLM calls": self.event_counts["llms"],
            "Tool calls": self.event_counts["tools"],
            "Actions": self.event_counts["actions"],
            "Errors": self.event_counts["errors"],
            "Duration": formatted_duration,
            "Cost": self._format_token_cost(self.token_cost),
        }

    @property
    def session_url(self) -> str:
        """URL to view this trace in the dashboard"""
        return f"https://app.agentops.ai/drilldown?session_id={self.session_id}"

    def end_session(self, *args, **kwargs):
        """
        Deprecated: Use end() instead.
        Kept for backward compatibility.
        """
        return self.end(*args, **kwargs)

    def __repr__(self) -> str:
        """Return a string representation of the Session."""
        status = "Running" if self.is_running else "Ended"
        tag_str = f", tags={self.tags}" if self.tags else ""
        end_state_str = f", end_state={self.end_state}" if not self.is_running else ""

        return f"Session(id={self.session_id}, status={status}" f"{tag_str}{end_state_str})"
