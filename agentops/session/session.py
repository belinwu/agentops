from __future__ import annotations

import functools
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum, StrEnum, auto
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union
from uuid import UUID, uuid4

from blinker import Signal
from opentelemetry import trace
from requests import Response
# from opentelemetry.context import attach, detach, set_value
# from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from termcolor import colored

import agentops
from agentops import session
from agentops.api.session import SessionApiClient
from agentops.config import Config, default_config
from agentops.exceptions import ApiServerException
from agentops.helpers import filter_unjsonable, get_ISO_time
from agentops.helpers.serialization import AgentOpsJSONEncoder
from agentops.logging import logger
from agentops.session.tracer_adapter import SessionTelemetryAdapter

from .state import SessionState
from .state import SessionStateDescriptor as session_state_field

if TYPE_CHECKING:
    from agentops.config import Config

from .signals import *



@dataclass
class Session(SessionTelemetryAdapter):
    """Data container for session state with minimal public API"""

    session_id: UUID = field(default_factory=uuid4)
    config: Config = field(default_factory=default_config)
    tags: List[str] = field(default_factory=list)
    host_env: Optional[dict] = None
    end_state_reason: Optional[str] = None
    jwt: Optional[str] = None
    video: Optional[str] = None
    event_counts: Dict[str, int] = field(
        default_factory=lambda: {"llms": 0, "tools": 0, "actions": 0, "errors": 0, "apis": 0}
    )

    # Define the state descriptor at class level
    state = session_state_field()

    ############################################################################################
    # kw-only fields below (controls)
    auto_start: bool = field(default=True, kw_only=True, repr=False, compare=False)

    @property
    def is_running(self) -> bool:
        """Whether session is currently running"""
        return self.state.is_alive

    def __post_init__(self):
        """Initialize session components after dataclass initialization"""
        # Initialize session-specific components
        self._lock = threading.Lock()
        self._end_session_lock = threading.Lock()

        if self.config.api_key is None:
            self.state = SessionState.FAILED
            if not self.config.fail_safe:
                raise ValueError("API key is required")
            logger.error("API key is required")
            return

        self.api = SessionApiClient(self)

        # Signal session is initialized
        session_initialized.send(self)

        # Initialize session only if auto_start is True
        if self.auto_start:
            try:
                if not self.start():
                    self.state = SessionState.FAILED
                    if not self.config.fail_safe:
                        raise RuntimeError("Session.start() did not succeed", self)
                    logger.error("Session initialization failed")
                    return
            except Exception as e:
                if not self.config.fail_safe:
                    raise
                self.state = SessionState.FAILED
                logger.error(f"Failed to initialize session: {e}")
                self.end(str(SessionState.FAILED), f"Exception during initialization: {str(e)}")

    @property
    def token_cost(self) -> str:
        """
        Processes token cost based on the last response from the API.
        """
        try:
            # Get token cost from either response or direct value
            cost = Decimal(0)
            if self.api.last_response is not None:
                cost_value = self.api.last_response.json().get("token_cost", "unknown")
                if cost_value != "unknown" and cost_value is not None:
                    cost = Decimal(str(cost_value))

            # Format the cost
            return (
                "{:.2f}".format(cost)
                if cost == 0
                else "{:.6f}".format(cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
            )
        except (ValueError, AttributeError):
            return "0.00"


    @property
    def analytics(self) -> Optional[Dict[str, Union[int, str]]]:
        """Get session analytics"""
        formatted_duration = self._format_duration(self.init_timestamp, self.end_timestamp)

        return {
            "LLM calls": self.event_counts["llms"],
            "Tool calls": self.event_counts["tools"],
            "Actions": self.event_counts["actions"],
            "Errors": self.event_counts["errors"],
            "Duration": formatted_duration,
            "Cost": self.token_cost,
        }

    @property
    def session_url(self) -> str:
        """URL to view this trace in the dashboard"""
        return f"{self.config.endpoint}/drilldown?session_id={self.session_id}"

    def _map_end_state(self, state: str) -> SessionState:
        """Map common end state strings to SessionState enum values"""
        state_map = {
            "Success": SessionState.SUCCEEDED,
            "SUCCEEDED": SessionState.SUCCEEDED,
            "Succeeded": SessionState.SUCCEEDED,
            "Fail": SessionState.FAILED,
            "FAILED": SessionState.FAILED,
            "Failed": SessionState.FAILED,
            "Indeterminate": SessionState.INDETERMINATE,
            "INDETERMINATE": SessionState.INDETERMINATE
        }
        try:
            # First try to map the string directly
            return state_map.get(state, SessionState(state))
        except ValueError:
            logger.warning(f"Invalid end state: {state}, using INDETERMINATE")
            return SessionState.INDETERMINATE

    def end(
        self, 
        end_state: Optional[str] = None,
        end_state_reason: Optional[str] = None,
        video: Optional[str] = None
    ) -> None:
        """End the session"""
        with self._end_session_lock:
            if self.state.is_terminal:
                logger.debug(f"Session {self.session_id} already ended")
                return

            # Update state before sending signal
            if end_state is not None:
                self.state = SessionState.from_string(end_state)
            if end_state_reason is not None:
                self.end_state_reason = end_state_reason
            if video is not None:
                self.video = video

            # Send signal with current state
            session_ending.send(self, 
                session_id=self.session_id,
                end_state=str(self.state),
                end_state_reason=self.end_state_reason
            )

            self.end_timestamp = get_ISO_time()

            session_data = json.loads(
                json.dumps(asdict(self), cls=AgentOpsJSONEncoder)
            )
            self.api.update_session(session_data)

            session_updated.send(self)
            session_ended.send(self, 
                session_id=self.session_id,
                end_state=str(self.state),
                end_state_reason=self.end_state_reason
            )
            logger.debug(f"Session {self.session_id} ended with state {self.state}")

    def start(self):
        """Start the session"""
        with self._lock:
            if self.state != SessionState.INITIALIZING:
                logger.warning("Session already started")
                return False

            session_starting.send(self)
            self.init_timestamp = get_ISO_time()

            try:
                session_data = json.loads(
                    json.dumps(asdict(self), cls=AgentOpsJSONEncoder)
                )
                self.jwt = self.api.create_session(session_data)

                logger.info(
                    colored(
                        f"\x1b[34mSession Replay: {self.session_url}\x1b[0m",
                        "blue",
                    )
                )

                # Set state before sending signal so registry sees correct state
                self.state = SessionState.RUNNING
                
                # Send session_started signal with self as sender
                session_started.send(self)
                logger.debug(f"{self.session_id} Sessionstarted successfully")
                return True

            except ApiServerException as e:
                logger.error(f"{self.session_id} Could not start session - {e}")
                self.state = SessionState.FAILED
                if not self.config.fail_safe:
                    raise
                return False

    def flush(self):
        self.api.update_session()
        session_updated.send(self)

    def _format_duration(self, start_time, end_time) -> str:
        """Format duration between two timestamps"""
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

    ##########################################################################################
    def __repr__(self) -> str:
        """String representation"""
        parts = [f"Session(id={self.session_id}, status={self.state}"]
        
        if self.tags:
            parts.append(f"tags={self.tags}")
            
        if self.state.is_terminal and self.end_state_reason:
            parts.append(f"reason='{self.end_state_reason}'")
            
        return ", ".join(parts) + ")"

    def add_tags(self, tags: List[str]) -> None:
        """Add tags to the session
        
        Args:
            tags: List of tags to add
        """
        if self.state.is_terminal:
            logger.warning(f"{self.session_id} Cannot add tags to ended session")
            return
        
        self.tags.extend(tags)
        session_updated.send(self)
    def dict(self) -> dict:
        """Convert session to dictionary, excluding private and non-serializable fields"""
        return {
            "session_id": self.session_id,
            "config": asdict(self.config),  # Serialize config separately
            "tags": self.tags,
            "host_env": self.host_env,
            "state": str(self.state),
            "jwt": self.jwt,
            "video": self.video,
            "event_counts": self.event_counts,
        }

    def set_tags(self, tags: List[str]) -> None:
        """Set session tags, replacing existing ones
        
        Args:
            tags: List of tags to set
        """
        if self.state.is_terminal:
            logger.warning("Cannot set tags on ended session")
            return
        
        self.tags = tags
        session_updated.send(self)
    def json(self):
        return json.dumps(self.dict(), cls=AgentOpsJSONEncoder)
