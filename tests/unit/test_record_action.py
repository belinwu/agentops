import time
from datetime import datetime

import pytest

import agentops
from agentops import record_action


class TestRecordAction:
    @pytest.fixture(autouse=True)
    def setup(self, api_key, base_url):
        self.url = base_url
        self.api_key = api_key
        self.event_type = "test_event"
        agentops.init(self.api_key, max_wait_time=50, auto_start_session=False)

    def test_record_action_decorator(self, mock_req, agentops_session):
        @record_action(event_name=self.event_type)
        def add_two(x, y):
            return x + y

        # Act
        add_two(3, 4)
        time.sleep(0.1)

        # Find the record_action request
        action_requests = [r for r in mock_req.request_history if "/v2/create_events" in r.url]
        assert len(action_requests) > 0
        last_action_request = action_requests[-1]

        assert last_action_request.headers["X-Agentops-Api-Key"] == self.api_key
        request_json = last_action_request.json()
        assert request_json["events"][0]["action_type"] == self.event_type
        assert request_json["events"][0]["params"] == {"x": 3, "y": 4}
        assert request_json["events"][0]["returns"] == 7

        agentops.end_session(end_state="Success")

    def test_record_action_default_name(self, mock_req, agentops_session):
        @record_action()
        def add_two(x, y):
            return x + y

        # Act
        add_two(3, 4)

        agentops_session._tracer.force_flush()

        # Find the record_action request
        action_requests = [r for r in mock_req.request_history if "/v2/create_events" in r.url]
        assert len(action_requests) > 0
        last_action_request = action_requests[-1]

        assert last_action_request.headers["X-Agentops-Api-Key"] == self.api_key
        request_json = last_action_request.json()
        assert request_json["events"][0]["action_type"] == "add_two"
        assert request_json["events"][0]["params"] == {"x": 3, "y": 4}
        assert request_json["events"][0]["returns"] == 7

        agentops.end_session(end_state="Success")

    def test_record_action_decorator_multiple(self, mock_req, agentops_session):
        # Arrange
        @record_action(event_name=self.event_type)
        def add_three(x, y, z=3):
            return x + y + z

        # Act
        add_three(1, 2)
        add_three(1, 2, 4)

        agentops_session._tracer.force_flush()

        # time.sleep(1.5)

        # Find the record_action request
        action_requests = [r for r in mock_req.request_history if "/v2/create_events" in r.url]
        assert len(action_requests) > 0
        last_action_request = action_requests[-1]

        assert last_action_request.headers["X-Agentops-Api-Key"] == self.api_key
        request_json = last_action_request.json()

        assert request_json["events"][1]["action_type"] == self.event_type
        assert request_json["events"][1]["params"] == {"x": 1, "y": 2, "z": 4}
        assert request_json["events"][1]["returns"] == 7

        assert request_json["events"][0]["action_type"] == self.event_type
        assert request_json["events"][0]["params"] == {"x": 1, "y": 2, "z": 3}
        assert request_json["events"][0]["returns"] == 6

        agentops.end_session(end_state="Success")

    @pytest.mark.asyncio
    async def test_async_action_call(self, mock_req, agentops_session):
        @record_action(self.event_type)
        async def async_add(x, y):
            time.sleep(0.1)
            return x + y

        # Act
        result = await async_add(3, 4)

        agentops_session._tracer.force_flush()
        # Assert
        assert result == 7

        # Find the record_action request
        action_requests = [r for r in mock_req.request_history if "/v2/create_events" in r.url]
        assert len(action_requests) > 0
        last_action_request = action_requests[-1]

        assert last_action_request.headers["X-Agentops-Api-Key"] == self.api_key
        request_json = last_action_request.json()
        assert request_json["events"][0]["action_type"] == self.event_type
        assert request_json["events"][0]["params"] == {"x": 3, "y": 4}
        assert request_json["events"][0]["returns"] == 7

        init = datetime.fromisoformat(request_json["events"][0]["init_timestamp"])
        end = datetime.fromisoformat(request_json["events"][0]["end_timestamp"])

        assert (end - init).total_seconds() >= 0.1

        agentops.end_session(end_state="Success")

    def test_multiple_sessions_sync(self, mock_req):
        session_1 = agentops.start_session()
        session_2 = agentops.start_session()
        assert session_1 is not None
        assert session_2 is not None

        # Arrange
        @record_action(event_name=self.event_type)
        def add_three(x, y, z=3, *, session=None):
            return x + y + z

        # Act
        add_three(1, 2, session=session_1)
        add_three(1, 2, 3, session=session_2)

        session_1._tracer.force_flush()
        session_2._tracer.force_flush()

        # Find action requests
        action_requests = [r for r in mock_req.request_history if "/v2/create_events" in r.url]
        assert len(action_requests) >= 2  # Should have at least 2 action requests

        # Verify session_2's request (last request)
        last_request = action_requests[-1]
        assert last_request.headers["X-Agentops-Api-Key"] == self.api_key
        assert last_request.headers["Authorization"] == f"Bearer {mock_req.session_jwts[str(session_2.session_id)]}"
        request_json = last_request.json()
        assert request_json["events"][0]["action_type"] == self.event_type
        assert request_json["events"][0]["params"] == {"x": 1, "y": 2, "z": 3}
        assert request_json["events"][0]["returns"] == 6

        # Verify session_1's request (second to last request)
        second_last_request = action_requests[-2]
        assert second_last_request.headers["X-Agentops-Api-Key"] == self.api_key
        assert (
            second_last_request.headers["Authorization"] == f"Bearer {mock_req.session_jwts[str(session_1.session_id)]}"
        )
        request_json = second_last_request.json()
        assert request_json["events"][0]["action_type"] == self.event_type
        assert request_json["events"][0]["params"] == {"x": 1, "y": 2, "z": 3}
        assert request_json["events"][0]["returns"] == 6

        session_1.end_session(end_state="Success")
        session_2.end_session(end_state="Success")

    @pytest.mark.asyncio
    async def test_multiple_sessions_async(self, mock_req):
        session_1 = agentops.start_session()
        session_2 = agentops.start_session()
        assert session_1 is not None
        assert session_2 is not None

        # Arrange
        @record_action(self.event_type)
        async def async_add(x, y, *, session=None):
            time.sleep(0.1)
            return x + y

        # Act
        await async_add(1, 2, session=session_1)
        time.sleep(0.1)
        await async_add(1, 2, session=session_2)
        time.sleep(0.1)

        # Find action requests
        action_requests = [r for r in mock_req.request_history if "/v2/create_events" in r.url]
        assert len(action_requests) >= 2  # Should have at least 2 action requests

        # Verify session_2's request (last request)
        last_request = action_requests[-1]
        assert last_request.headers["X-Agentops-Api-Key"] == self.api_key
        assert last_request.headers["Authorization"] == f"Bearer {mock_req.session_jwts[str(session_2.session_id)]}"
        request_json = last_request.json()
        assert request_json["events"][0]["action_type"] == self.event_type
        assert request_json["events"][0]["params"] == {"x": 1, "y": 2}
        assert request_json["events"][0]["returns"] == 3

        # Verify session_1's request (second to last request)
        second_last_request = action_requests[-2]
        assert second_last_request.headers["X-Agentops-Api-Key"] == self.api_key
        assert (
            second_last_request.headers["Authorization"] == f"Bearer {mock_req.session_jwts[str(session_1.session_id)]}"
        )
        request_json = second_last_request.json()
        assert request_json["events"][0]["action_type"] == self.event_type
        assert request_json["events"][0]["params"] == {"x": 1, "y": 2}
        assert request_json["events"][0]["returns"] == 3

        session_1.end_session(end_state="Success")
        session_2.end_session(end_state="Success")

    def test_require_session_if_multiple(self, mock_req):
        session_1 = agentops.start_session()
        session_2 = agentops.start_session()

        # Arrange
        @record_action(self.event_type)
        def add_two(x, y):
            time.sleep(0.1)
            return x + y

        with pytest.raises(ValueError):
            # Act
            add_two(1, 2)
