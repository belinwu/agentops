from __future__ import annotations

import threading
from datetime import datetime
from decimal import Decimal
from functools import cached_property
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from termcolor import colored

from agentops.event import ErrorEvent, Event
from agentops.helpers import get_ISO_time
from agentops.log_config import logger

from .api import SessionApiClient
from .log_capture import LogCapture
from .registry import add_session, remove_session
from .telemetry import SessionTelemetry

if TYPE_CHECKING:
    from .session import Session


class SessionManager:
    """Handles session lifecycle and state management"""

    def __init__(self, session: "Session"):
        self._state = session
        self._lock = threading.Lock()
        self._end_session_lock = threading.Lock()

        # Initialize log capture
        self._log_capture = LogCapture(self._state)

    @cached_property
    def api(self) -> SessionApiClient:
        """Get API client"""
        return SessionApiClient(
            endpoint=self._state.config.endpoint, session_id=self._state.session_id, api_key=self._state.config.api_key
        )

    @cached_property
    def telemetry(self) -> SessionTelemetry:
        # Initialize telemetry with our API client
        from .telemetry import SessionTelemetry

        return SessionTelemetry(self._state, api_client=self.api)

    def start_session(self) -> bool:
        """Start and initialize session"""
        with self._lock:
            success, jwt = self.api.create_session(self._serialize_session(), self._state.config.parent_key)
            if success:
                self._state.jwt = jwt
                self.api.jwt = jwt  # Update JWT on API client
                add_session(self._state)
            return success

    def create_agent(self, name: str, agent_id: Optional[str] = None) -> Optional[str]:
        """Create a new agent"""
        with self._lock:
            if agent_id is None:
                from uuid import uuid4

                agent_id = str(uuid4())

            if not self._state.api:
                return None

            success = self._state.api.create_agent(name=name, agent_id=agent_id)
            return agent_id if success else None

    def add_tags(self, tags: Union[str, List[str]]) -> None:
        """Add tags to session"""
        with self._lock:
            if isinstance(tags, str):
                if tags not in self._state.tags:
                    self._state.tags.append(tags)
            elif isinstance(tags, list):
                self._state.tags.extend(t for t in tags if t not in self._state.tags)

            if self._state.api:
                self._state.api.update_session({"tags": self._state.tags})

    def set_tags(self, tags: Union[str, List[str]]) -> None:
        """Set session tags"""
        with self._lock:
            if isinstance(tags, str):
                self._state.tags = [tags]
            elif isinstance(tags, list):
                self._state.tags = list(tags)

            if self._state.api:
                self._state.api.update_session({"tags": self._state.tags})

    def record_event(self, event: Union["Event", "ErrorEvent"], flush_now: bool = False) -> None:
        """Update event counts and record event"""
        with self._lock:
            # Update counts
            if event.event_type in self._state.event_counts:
                self._state.event_counts[event.event_type] += 1

            # Record via telemetry
            if self.telemetry:
                self.telemetry.record_event(event, flush_now)

    def end_session(
        self, end_state: str, end_state_reason: Optional[str], video: Optional[str]
    ) -> Union[Decimal, None]:
        """End session and cleanup"""
        with self._end_session_lock:
            if not self._state.is_running:
                return None

            try:
                # Flush any pending telemetry
                if self.telemetry:
                    self.telemetry.flush(timeout_millis=5000)

                self._state.end_timestamp = get_ISO_time()
                self._state.end_state = end_state
                self._state.end_state_reason = end_state_reason
                self._state.video = video if video else self._state.video
                self._state.is_running = False

                if analytics := self._get_analytics():
                    self._log_analytics(analytics)
                    remove_session(self._state)
                    return self._state.token_cost
                return None
            except Exception as e:
                logger.exception(f"Error ending session: {e}")
                return None

    def _get_analytics(self) -> Optional[Dict[str, Union[int, str]]]:
        """Get session analytics"""
        if not self._state.end_timestamp:
            self._state.end_timestamp = get_ISO_time()

        formatted_duration = self._format_duration(self._state.init_timestamp, self._state.end_timestamp)

        if not self._state.api:
            return None

        response = self._state.api.update_session(self._serialize_session())
        if not response:
            return None

        # Update token cost from API response
        if "token_cost" in response:
            self._state.token_cost = Decimal(str(response["token_cost"]))

        return {
            "LLM calls": self._state.event_counts["llms"],
            "Tool calls": self._state.event_counts["tools"],
            "Actions": self._state.event_counts["actions"],
            "Errors": self._state.event_counts["errors"],
            "Duration": formatted_duration,
            "Cost": self._format_token_cost(self._state.token_cost),
        }

    def _serialize_session(self) -> Dict[str, Any]:
        """Convert session to API-friendly dict format"""
        # Get only the public fields we want to send to API
        return {
            "session_id": str(self._state.session_id),
            "tags": self._state.tags,
            "host_env": self._state.host_env,
            "token_cost": float(self._state.token_cost),
            "end_state": self._state.end_state,
            "end_state_reason": self._state.end_state_reason,
            "end_timestamp": self._state.end_timestamp,
            "jwt": self._state.jwt,
            "video": self._state.video,
            "event_counts": self._state.event_counts,
            "init_timestamp": self._state.init_timestamp,
            "is_running": self._state.is_running,
        }

    def _format_duration(self, start_time: str, end_time: str) -> str:
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

    def _format_token_cost(self, token_cost: Decimal) -> str:
        """Format token cost for display"""
        # Always format with 6 decimal places for consistency with tests
        return "{:.6f}".format(token_cost)

    def _log_analytics(self, stats: Dict[str, Union[int, str]]) -> None:
        """Log analytics in a consistent format"""
        analytics = (
            f"Session Stats - "
            f"{colored('Duration:', attrs=['bold'])} {stats['Duration']} | "
            f"{colored('Cost:', attrs=['bold'])} ${stats['Cost']} | "
            f"{colored('LLMs:', attrs=['bold'])} {str(stats['LLM calls'])} | "
            f"{colored('Tools:', attrs=['bold'])} {str(stats['Tool calls'])} | "
            f"{colored('Actions:', attrs=['bold'])} {str(stats['Actions'])} | "
            f"{colored('Errors:', attrs=['bold'])} {str(stats['Errors'])}"
        )
        logger.info(analytics)

    def start_log_capture(self):
        """Start capturing terminal output"""
        self._log_capture.start()

    def stop_log_capture(self):
        """Stop capturing terminal output"""
        self._log_capture.stop()
