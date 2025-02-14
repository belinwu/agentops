# OTEL Session Telemetry Redesign Design Document

## Current Context
- The current system uses a custom event mechanism with legacy signals (via Blinker) to manage the session lifecycle.
- The `Session` class tracks state, token cost, and event counts while relying on custom event serialization.
- LLM providers create child events which are later consumed by legacy export logic.
- Observability and telemetry are implemented in a non-standardized way, making it difficult to scale and correlate across distributed components.

## Requirements

### Functional Requirements
- **Native OTEL Integration:**  
  - The `Session` class must create a parent OpenTelemetry span with a unique trace ID on initialization.
  - All subsequent operations (agent actions, LLM calls) will create child spans that inherit OTEL context.
- **Context Propagation:**  
  - Use OTEL baggage to propagate critical session metadata (e.g., session IDs, trace IDs) across asynchronous and synchronous boundaries.
- **Modular Exporter Support:**  
  - Enable plug-and-play configuration for OTEL span processors and exporters (such as Jaeger, Zipkin, or OTLP) via a central configuration.
- **Comprehensive Telemetry Collection:**  
  - Capture detailed telemetry data across sessions, including timing, error statuses, and other custom attributes.
- **Error Handling and Resilience:**  
  - Implement batching (with a BatchSpanProcessor) and head-based sampling to reduce overhead.
  - Ensure graceful shutdown behavior so that no span data is lost on termination, even if span export failures occur.
- **Asynchronous Operations Support:**  
  - Ensure that asynchronous operations in LLM providers correctly propagate and maintain OTEL context.
- **No Backward Compatibility:**  
  - Since the architecture is being completely revamped, there is no requirement to support backward compatibility with legacy event systems.

### Non-Functional Requirements
- **Performance:**  
  - Use batch processing and configurable sampling (head-based sampling) to mitigate processing overhead while ensuring detailed telemetry.
- **Scalability:**  
  - Support concurrency by isolating OTEL context on a per-session basis.
- **Observability:**  
  - Enhance traceability with structured spans and associated metadata.
- **Security:**  
  - Securely propagate session context data and protect sensitive metadata.
- **Error Resilience:**  
  - Gracefully handle span export errors and ensure successful span flushing during shutdown.

## Design Decisions

### 1. Telemetry Integration Approach
Will implement native OpenTelemetry integration directly in the `Session` class.
- **Rationale:**
  - Aligns telemetry lifecycle with the session lifecycle.
  - Utilizes standardized OTEL components for tracing and context propagation.
- **Trade-offs:**
  - Requires re-architecting and removal of legacy event systems.
- **References:**
  - [OpenTelemetry Error Handling Specifications](https://opentelemetry.io/docs/specs/otel/error-handling/)

### 2. Asynchronous Operations Support
Will ensure that asynchronous provider requests are handled using OTEL's asynchronous context mechanisms (e.g., using `contextvars` and async context managers).
- **Rationale:**
  - Ensures that the OTEL context is preserved even in async operations.
- **Trade-offs:**
  - Increased complexity in managing async contexts.
- **References:**
  - [OpenTelemetry Python Documentation](https://opentelemetry.io/docs/instrumentation/python/)

### 3. Sampling and Batch Processing
Will implement batch span processing and head-based sampling.
- **Rationale:**
  - Improve performance by reducing per-span export overhead.
  - Ensure robust telemetry in high-throughput environments.
- **Trade-offs:**
  - Some lower-level spans may be sampled out.
- **References:**
  - [Head-Based Sampling Paper](https://umu.diva-portal.org/smash/get/diva2:1877027/FULLTEXT01.pdf)

### 4. Instrumentation Helpers
Will integrate with external tools like OpenLLMetry (@Traceloop) for out-of-box instrumentation of providers.
- **Rationale:**
  - Reduces boilerplate code in each provider module.
  - Ensures consistency in span creation and metadata enrichment.

## Technical Design

### 1. Core Components
```python
# Example interface: (Pseudo-code)
class TelemetryManager:
    """
    Handles global OTEL tracer initialization, baggage propagation,
    batching of spans, and graceful shutdown of OTEL components.
    """
    pass

class Session:
    """
    Session class augmented with OTEL parent span creation,
    context propagation via baggage, and integration with TelemetryManager.
    """
    pass
```

### 2. Data Models
```python
# Example telemetry data model: (Pseudo-code)
class SessionTelemetryData:
    """
    Data model to capture detailed telemetry attributes for a session.
    """
    pass
```

### 3. Integration Points
- **LLM Providers:**  
  Update providers in `agentops/llms/providers/` to create child spans using the global tracer and current OTEL context.
- **API Communication:**  
  Enhance `SessionApiClient` to attach telemetry attributes to outgoing requests.
- **Configuration:**  
  Extend the existing `Config` model to support OTEL-specific settings such as exporter configurations, sampling rates, etc.

## Implementation Plan

1. **Phase 1: Global Telemetry Setup**
   - Initialize the global OTEL tracer and create a TelemetryManager.
   - Modify the `Session` class to create a parent span with appropriate baggage on initialization.
   - **Expected Timeline:** 1-2 weeks

2. **Phase 2: Provider and API Integration**
   - Refactor LLM providers to create and manage child spans.
   - Update `SessionApiClient` to forward telemetry data alongside API calls.
   - Integrate instrumentation helpers like OpenLLMetry.
   - **Expected Timeline:** 2-3 weeks

3. **Phase 3: Performance & Resilience Enhancements**
   - Implement batch span processing and head-based sampling.
   - Integrate graceful shutdown routines to flush remaining spans.
   - Enhance support for asynchronous operations.
   - **Expected Timeline:** 2 weeks

## Testing Strategy

### Unit Tests
- Validate that the `Session` class creates the correct OTEL parent span with proper attributes.
- Use the `InMemorySpanExporter` to capture and verify spans during tests.
- Assert correct OTEL baggage propagation.

### Integration Tests
- Simulate LLM provider workflows and verify that child spans are linked to the session's parent span.
- Test asynchronous spans to ensure context is maintained across async operations.
- Use pytest and pytest-vcr to simulate API interactions and verify telemetry data is included.

## Observability

### Logging
- Implement structured logging at key lifecycle events (session start, span creation, errors).
- Use appropriate log levels (e.g., INFO for events, ERROR for failures).

### Metrics
- Track metrics such as span creation rate, export success rates, and batching efficiency.
- Integrate with external monitoring solutions like Prometheus.

## Future Considerations

### Potential Enhancements
- Further tuning of sampling strategies based on dynamic workload parameters.
- Extended distributed tracing across microservices.
- Evaluation of advanced retry mechanisms for span export failures.

### Known Limitations
- Complexity in completely phasing out the legacy event system during the transition.
- Potential configuration challenges in very high throughput scenarios without additional tuning.

## Dependencies

### Runtime Dependencies
- `opentelemetry-api`
- `opentelemetry-sdk`
- `opentelemetry-exporter-jaeger` (or alternative exporters)
- OpenLLMetry (for streamlined provider instrumentation)

### Development Dependencies
- `pytest`
- `pytest-vcr`
- `InMemorySpanExporter` (for unit testing)
- Additional OTEL instrumentation packages as needed

## Security Considerations
- Secure propagation of session context information.
- Avoid embedding sensitive data in span attributes.
- Ensure compliance with data protection standards.

## Rollout Strategy
1. **Development Phase:**
   - Develop the TelemetryManager and refactor the `Session` class.
2. **Testing Phase:**
   - Run comprehensive unit and integration tests.
3. **Staging Deployment:**
   - Deploy to a staging environment and monitor telemetry data.
4. **Production Deployment:**
   - Roll out to production post-validation.
5. **Monitoring Period:**
   - Schedule a monitoring period to assess stability and performance.

## References
- [OpenTelemetry Error Handling Specifications](https://opentelemetry.io/docs/specs/otel/error-handling/)
- [Head-Based Sampling Paper](https://umu.diva-portal.org/smash/get/diva2:1877027/FULLTEXT01.pdf)
- [OpenTelemetry Python Documentation](https://opentelemetry.io/docs/instrumentation/python/) 