import json
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Sequence
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID, uuid4

import pytest
import requests_mock
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode
from opentelemetry.trace.span import TraceState

import agentops
from agentops import ActionEvent, Client
from agentops.config import Configuration
from agentops.event import ErrorEvent, LLMEvent, ToolEvent
from agentops.http_client import HttpClient, HttpStatus, Response
from agentops.session import Session
from agentops.singleton import clear_singletons


@pytest.fixture(autouse=True, scope="function")
def setup_teardown(mock_req):
    clear_singletons()
    yield
    agentops.end_all_sessions()  # teardown part


import logging

logging.warning("ATTENTION: This test suite is legacy")


"""
Time patching demo:

class TestSession:
    @patch('time.monotonic')
    def test_session_timing(self, mock_time):
        # Mock a sequence of timestamps (in seconds)
        timestamps = [
            0.0,    # Session start
            0.1,    # First event
            0.2,    # Second event
            0.3     # Session end
        ]
        mock_time.side_effect = timestamps
        
        # Start session
        session = agentops.start_session()
        
        # First event - time will be 0.1
        session.record(ActionEvent("test_event"))
        
        # Second event - time will be 0.2
        session.record(ActionEvent("test_event"))
        
        # End session - time will be 0.3
        session.end_session("Success")
        
        # Verify duration calculation
        analytics = session.get_analytics()
        assert analytics["Duration"] == "0.3s"  # Duration from 0.0 to 0.3
"""


class TestNonInitializedSessions:
    def setup_method(self):
        self.api_key = "11111111-1111-4111-8111-111111111111"
        self.event_type = "test_event_type"

    def test_non_initialized_doesnt_start_session(self, mock_req):
        agentops.set_api_key(self.api_key)
        session = agentops.start_session()
        assert session is None


class TestSingleSessions:
    def setup_method(self):
        self.api_key = "11111111-1111-4111-8111-111111111111"
        self.event_type = "test_event_type"
        agentops.init(api_key=self.api_key, max_wait_time=5000, auto_start_session=False)

    def test_session(self, mock_req):
        session: Session = agentops.start_session()
        agentops.record(ActionEvent(self.event_type))
        agentops.record(ActionEvent(self.event_type))

        session.flush()  # Forces the exporter to flush

        # 3 Requests: check_for_updates, start_session, create_events (2 in 1)
        assert len(mock_req.request_history) >= 3

        assert mock_req.last_request.headers["Authorization"] == "Bearer some_jwt"
        request_json = mock_req.last_request.json()
        assert request_json["events"][0]["event_type"] == self.event_type

        end_state = "Success"
        agentops.end_session(end_state)

        # We should have 4 requests (additional end session)
        assert len(mock_req.request_history) == 4
        assert mock_req.last_request.headers["Authorization"] == "Bearer some_jwt"
        request_json = mock_req.last_request.json()
        assert request_json["session"]["end_state"] == end_state
        assert len(request_json["session"]["tags"]) == 0

        agentops.end_all_sessions()

    def test_add_tags(self, mock_req):
        # Arrange
        tags = ["GPT-4"]
        agentops.start_session(tags=tags)
        agentops.add_tags(["test-tag", "dupe-tag"])
        agentops.add_tags(["dupe-tag"])

        # Act
        end_state = "Success"
        agentops.end_session(end_state)

        # Assert 3 requests, 1 for session init, 1 for event, 1 for end session
        request_json = mock_req.last_request.json()
        assert request_json["session"]["end_state"] == end_state
        assert request_json["session"]["tags"] == ["GPT-4", "test-tag", "dupe-tag"]

        agentops.end_all_sessions()

    def test_tags(self, mock_req):
        # Arrange
        tags = ["GPT-4"]
        session = agentops.start_session(tags=tags)

        # Act
        agentops.record(ActionEvent(self.event_type))

        # Act
        end_state = "Success"
        agentops.end_session(end_state)

        agentops.flush()

        # 4 requests: check_for_updates, start_session, record_event, end_session
        assert len(mock_req.request_history) >= 4
        assert mock_req.last_request.headers["X-Agentops-Api-Key"] == self.api_key
        request_json = mock_req.last_request.json()
        assert request_json["session"]["end_state"] == end_state
        assert request_json["session"]["tags"] == tags
        session.end_session()

    def test_inherit_session_id(self, mock_req):
        # Arrange
        inherited_id = "4f72e834-ff26-4802-ba2d-62e7613446f1"
        session = agentops.start_session(tags=["test"], inherited_session_id=inherited_id)

        # Act
        # session_id correct
        request_json = mock_req.last_request.json()
        assert request_json["session"]["session_id"] == inherited_id

        # Act
        end_state = "Success"
        session.end_session(end_state)

    def test_add_tags_with_string(self, mock_req):
        agentops.start_session()
        agentops.add_tags("wrong-type-tags")

        request_json = mock_req.last_request.json()
        assert request_json["session"]["tags"] == ["wrong-type-tags"]

    def test_session_add_tags_with_string(self, mock_req):
        session = agentops.start_session()
        session.add_tags("wrong-type-tags")

        request_json = mock_req.last_request.json()
        assert request_json["session"]["tags"] == ["wrong-type-tags"]

    def test_set_tags_with_string(self, mock_req):
        agentops.start_session()
        agentops.set_tags("wrong-type-tags")

        request_json = mock_req.last_request.json()
        assert request_json["session"]["tags"] == ["wrong-type-tags"]

    def test_session_set_tags_with_string(self, mock_req):
        session = agentops.start_session()
        assert session is not None

        session.set_tags("wrong-type-tags")

        request_json = mock_req.last_request.json()
        assert request_json["session"]["tags"] == ["wrong-type-tags"]

    def test_set_tags_before_session(self, mock_req):
        agentops.configure(default_tags=["pre-session-tag"])
        agentops.start_session()

        request_json = mock_req.last_request.json()
        assert request_json["session"]["tags"] == ["pre-session-tag"]

    def test_safe_get_session_no_session(self, mock_req):
        session = Client()._safe_get_session()
        assert session is None

    def test_safe_get_session_with_session(self, mock_req):
        agentops.start_session()
        session = Client()._safe_get_session()
        assert session is not None

    def test_safe_get_session_with_multiple_sessions(self, mock_req):
        agentops.start_session()
        agentops.start_session()

        session = Client()._safe_get_session()
        assert session is None

    def test_get_analytics(self, mock_req):
        # Arrange
        session = agentops.start_session()
        session.add_tags(["test-session-analytics-tag"])
        assert session is not None

        # Record some events to increment counters
        session.record(LLMEvent())
        session.record(ToolEvent())
        session.record(ActionEvent("test-action"))
        session.record(ErrorEvent())

        agentops.flush()

        # Act
        analytics = session.get_analytics()

        # Assert
        assert isinstance(analytics, dict)
        assert all(key in analytics for key in ["LLM calls", "Tool calls", "Actions", "Errors", "Duration", "Cost"])

        # Check specific values
        assert analytics["LLM calls"] == 1
        assert analytics["Tool calls"] == 1
        assert analytics["Actions"] == 1
        assert analytics["Errors"] == 1

        # Check duration format
        assert isinstance(analytics["Duration"], str)
        assert "s" in analytics["Duration"]

        # Check cost format (mock returns token_cost: 5)
        assert analytics["Cost"] == "5.000000"

        # End session and cleanup
        session.end_session(end_state="Success")
        agentops.end_all_sessions()


class TestMultiSessions:
    def setup_method(self):
        self.api_key = "11111111-1111-4111-8111-111111111111"
        self.event_type = "test_event_type"
        agentops.init(api_key=self.api_key, max_wait_time=500, auto_start_session=False)

    def test_two_sessions(self, mock_req):
        session_1 = agentops.start_session()
        session_2 = agentops.start_session()
        assert session_1 is not None
        assert session_2 is not None

        assert len(agentops.Client().current_session_ids) == 2
        assert agentops.Client().current_session_ids == [
            str(session_1.session_id),
            str(session_2.session_id),
        ]

        # Requests: check_for_updates, 2 start_session
        assert len(mock_req.request_history) == 3

        session_1.record(ActionEvent(self.event_type))
        session_2.record(ActionEvent(self.event_type))

        agentops.flush()

        # 5 requests: check_for_updates, 2 start_session, 2 record_event
        assert len(mock_req.request_history) == 5
        assert mock_req.last_request.headers["Authorization"] == "Bearer some_jwt"
        request_json = mock_req.last_request.json()
        assert request_json["events"][0]["event_type"] == self.event_type

        end_state = "Success"

        session_1.end_session(end_state)

        # Additional end session request
        assert len(mock_req.request_history) == 6
        assert mock_req.last_request.headers["Authorization"] == "Bearer some_jwt"
        request_json = mock_req.last_request.json()
        assert request_json["session"]["end_state"] == end_state
        assert len(request_json["session"]["tags"]) == 0

        session_2.end_session(end_state)
        # Additional end session request
        assert len(mock_req.request_history) == 7
        assert mock_req.last_request.headers["Authorization"] == "Bearer some_jwt"
        request_json = mock_req.last_request.json()
        assert request_json["session"]["end_state"] == end_state
        assert len(request_json["session"]["tags"]) == 0

    def test_add_tags(self, mock_req):
        # Arrange
        session_1_tags = ["session-1"]
        session_2_tags = ["session-2"]

        session_1 = agentops.start_session(tags=session_1_tags)
        session_2 = agentops.start_session(tags=session_2_tags)
        assert session_1 is not None
        assert session_2 is not None

        session_1.add_tags(["session-1-added", "session-1-added-2"])
        session_2.add_tags(["session-2-added"])

        # Act
        end_state = "Success"
        session_1.end_session(end_state)
        session_2.end_session(end_state)

        # Assert 3 requests, 1 for session init, 1 for event, 1 for end session
        req1 = mock_req.request_history[-1].json()
        req2 = mock_req.request_history[-2].json()

        session_1_req = req1 if req1["session"]["session_id"] == session_1.session_id else req2
        session_2_req = req2 if req2["session"]["session_id"] == session_2.session_id else req1

        assert session_1_req["session"]["end_state"] == end_state
        assert session_2_req["session"]["end_state"] == end_state

        assert session_1_req["session"]["tags"] == [
            "session-1",
            "session-1-added",
            "session-1-added-2",
        ]

        assert session_2_req["session"]["tags"] == [
            "session-2",
            "session-2-added",
        ]

    def test_get_analytics_multiple_sessions(self, mock_req):
        session_1 = agentops.start_session()
        session_1.add_tags(["session-1", "test-analytics-tag"])
        session_2 = agentops.start_session()
        session_2.add_tags(["session-2", "test-analytics-tag"])
        assert session_1 is not None
        assert session_2 is not None

        # Record events in the sessions
        session_1.record(LLMEvent())
        session_1.record(ToolEvent())
        session_2.record(ActionEvent("test-action"))
        session_2.record(ErrorEvent())

        agentops.flush()

        # Act
        analytics_1 = session_1.get_analytics()
        analytics_2 = session_2.get_analytics()

        # Assert 2 record_event requests - 2 for each session
        assert analytics_1["LLM calls"] == 1
        assert analytics_1["Tool calls"] == 1
        assert analytics_1["Actions"] == 0
        assert analytics_1["Errors"] == 0

        assert analytics_2["LLM calls"] == 0
        assert analytics_2["Tool calls"] == 0
        assert analytics_2["Actions"] == 1
        assert analytics_2["Errors"] == 1

        # Check duration format
        assert isinstance(analytics_1["Duration"], str)
        assert "s" in analytics_1["Duration"]
        assert isinstance(analytics_2["Duration"], str)
        assert "s" in analytics_2["Duration"]

        # Check cost format (mock returns token_cost: 5)
        assert analytics_1["Cost"] == "5.000000"
        assert analytics_2["Cost"] == "5.000000"

        end_state = "Success"

        session_1.end_session(end_state)
        session_2.end_session(end_state)
