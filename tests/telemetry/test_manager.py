from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from agentops.telemetry.manager import TelemetryManager
from agentops.telemetry.config import OTELConfig
from agentops.telemetry.exporters.session import SessionExporter
from agentops.telemetry.processors import EventProcessor


@pytest.fixture
def config():
    """Create test config"""
    return OTELConfig(
        endpoint="https://test.agentops.ai",
        api_key="test-key"
    )


@pytest.fixture
def manager():
    """Create test manager"""
    return TelemetryManager()


class TestTelemetryManager:
    def test_initialization(self, manager: TelemetryManager, config: OTELConfig):
        """Test manager initialization"""
        manager.initialize(config)
        
        assert manager.config == config
        assert isinstance(manager._provider, TracerProvider)
        assert isinstance(manager._provider.sampler, ParentBased)
        
        # Verify global provider was set
        assert trace.get_tracer_provider() == manager._provider

    def test_initialization_with_custom_resource(self, manager: TelemetryManager):
        """Test initialization with custom resource attributes"""
        config = OTELConfig(
            endpoint="https://test.agentops.ai",
            resource_attributes={"custom.attr": "value"}
        )
        
        manager.initialize(config)
        resource = manager._provider.resource
        
        assert resource.attributes["service.name"] == "agentops"
        assert resource.attributes["custom.attr"] == "value"

    def test_create_session_tracer(self, manager: TelemetryManager, config: OTELConfig):
        """Test session tracer creation"""
        manager.initialize(config)
        session_id = uuid4()
        
        tracer = manager.create_session_tracer(session_id, "test-jwt")
        
        # Verify exporter was created
        assert session_id in manager._session_exporters
        assert isinstance(manager._session_exporters[session_id], SessionExporter)
        
        # Verify processor was added
        assert len(manager._processors) == 1
        assert isinstance(manager._processors[0], EventProcessor)
        
        # Verify tracer was created
        assert tracer.instrumentation_info.name == f"agentops.session.{session_id}"

    def test_cleanup_session(self, manager: TelemetryManager, config: OTELConfig):
        """Test session cleanup"""
        manager.initialize(config)
        session_id = uuid4()
        
        # Create session
        manager.create_session_tracer(session_id, "test-jwt")
        exporter = manager._session_exporters[session_id]
        
        # Clean up
        with patch.object(exporter, 'shutdown') as mock_shutdown:
            manager.cleanup_session(session_id)
            mock_shutdown.assert_called_once()
            
        assert session_id not in manager._session_exporters

    def test_shutdown(self, manager: TelemetryManager, config: OTELConfig):
        """Test manager shutdown"""
        manager.initialize(config)
        session_id = uuid4()
        
        # Create session
        manager.create_session_tracer(session_id, "test-jwt")
        exporter = manager._session_exporters[session_id]
        
        # Shutdown
        with patch.object(exporter, 'shutdown') as mock_shutdown:
            manager.shutdown()
            mock_shutdown.assert_called_once()
            
        assert not manager._session_exporters
        assert not manager._processors
        assert manager._provider is None

    def test_error_handling(self, manager: TelemetryManager):
        """Test error handling"""
        # Test initialization without config
        with pytest.raises(ValueError):
            manager.initialize(None)
        
        # Test creating tracer without initialization
        with pytest.raises(RuntimeError):
            manager.create_session_tracer(uuid4(), "test-jwt") 
