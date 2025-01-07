from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from unittest.mock import patch

import agentops
from agentops.telemetry.config import OTELConfig
from agentops.config import Configuration
from agentops.telemetry.client import ClientTelemetry


class InstrumentationTester:
    """Helper class for testing OTEL instrumentation"""
    def __init__(self):
        self.tracer_provider = TracerProvider()
        self.memory_exporter = InMemorySpanExporter()
        span_processor = SimpleSpanProcessor(self.memory_exporter)
        self.tracer_provider.add_span_processor(span_processor)
        
        # Reset and set global tracer provider
        trace_api.set_tracer_provider(self.tracer_provider)
        self.memory_exporter.clear()

    def get_finished_spans(self):
        return self.memory_exporter.get_finished_spans()


@pytest.fixture
def instrumentation():
    """Fixture providing instrumentation testing utilities"""
    return InstrumentationTester()


def test_configuration_with_otel():
    """Test that Configuration properly stores OTEL config"""
    exporter = OTLPSpanExporter(endpoint="http://localhost:4317")
    otel_config = OTELConfig(additional_exporters=[exporter])
    
    config = Configuration()
    config.configure(None, telemetry=otel_config)
    
    assert config.telemetry == otel_config
    assert config.telemetry.additional_exporters == [exporter]


def test_init_accepts_telemetry_config():
    """Test that init accepts telemetry configuration"""
    exporter = OTLPSpanExporter(endpoint="http://localhost:4317")
    telemetry = OTELConfig(additional_exporters=[exporter])
    
    agentops.init(
        api_key="test-key",
        telemetry=telemetry
    )
    
    # Verify exporter was configured
    client = agentops.Client()
    assert client.telemetry.config.additional_exporters == [exporter]


def test_init_with_env_var_endpoint(monkeypatch, instrumentation):
    """Test initialization with endpoint from environment variable"""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://custom:4317")
    
    # Create config and client telemetry
    config = OTELConfig()
    telemetry = ClientTelemetry(None)  # Pass None as client for testing
    
    try:
        # Initialize telemetry with our config
        telemetry.initialize(config)
        
        # Check the exporters were configured correctly
        assert config.additional_exporters is not None
        assert len(config.additional_exporters) == 1
        
        # Create a test span
        tracer = instrumentation.tracer_provider.get_tracer(__name__)
        with tracer.start_span("test") as span:
            span.set_attribute("test", "value")
        
        # Verify span was captured
        spans = instrumentation.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "test"
        assert spans[0].attributes["test"] == "value"
        
    finally:
        telemetry.shutdown()


def test_telemetry_config_overrides_env_vars(instrumentation):
    """Test that explicit telemetry config takes precedence over env vars"""
    custom_exporter = InMemorySpanExporter()
    telemetry = OTELConfig(additional_exporters=[custom_exporter])
    
    with patch('os.environ.get') as mock_env:
        mock_env.return_value = "http://fromenv:4317"
        
        agentops.init(
            api_key="test-key",
            telemetry=telemetry
        )
        
        client = agentops.Client()
        assert client.telemetry.config.additional_exporters == [custom_exporter]


def test_multiple_exporters_in_config():
    """Test configuration with multiple exporters"""
    exporter1 = OTLPSpanExporter(endpoint="http://first:4317")
    exporter2 = OTLPSpanExporter(endpoint="http://second:4317")
    
    telemetry = OTELConfig(additional_exporters=[exporter1, exporter2])
    config = Configuration()
    config.configure(None, telemetry=telemetry)
    
    assert len(config.telemetry.additional_exporters) == 2
    assert config.telemetry.additional_exporters == [exporter1, exporter2] 
