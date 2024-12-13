"""Voyage AI provider integration for AgentOps."""
import inspect
import warnings
import sys
import json
import pprint
import voyageai
from typing import Any, Dict, Optional, Callable
from agentops.llms.providers.instrumented_provider import InstrumentedProvider
from agentops.session import Session
from agentops.event import LLMEvent, ErrorEvent
from agentops.helpers import check_call_stack_for_agent_id, get_ISO_time
from agentops.log_config import logger
from agentops.singleton import singleton


def _check_python_version() -> None:
    """Check if the current Python version meets Voyage AI requirements."""
    if sys.version_info < (3, 9):
        warnings.warn(
            "Voyage AI SDK requires Python >=3.9. Some functionality may not work correctly.",
            UserWarning,
            stacklevel=2,
        )


@singleton
class VoyageProvider(InstrumentedProvider):
    """Provider for Voyage AI SDK integration.

    Handles embedding operations and tracks usage through AgentOps.
    Requires Python >=3.9 for full functionality.

    Args:
        client: Initialized Voyage AI client instance
    """

    def __init__(self, client=None):
        """Initialize VoyageProvider with optional client."""
        super().__init__(client or voyageai)
        self._provider_name = "Voyage"
        self._client = client or voyageai
        self.original_embed = None
        self.original_aembed = None
        _check_python_version()

    def embed(self, input_text: str, **kwargs) -> Dict[str, Any]:
        """Synchronous embed method."""
        init_timestamp = get_ISO_time()
        session = kwargs.pop("session", None)  # Extract and remove session from kwargs

        try:
            # Call the patched function
            response = self._client.embed(input_text, **kwargs)

            # Handle response and create event
            if session:
                self.handle_response(response, init_timestamp=init_timestamp, session=session)

            return response
        except Exception as e:
            if session:
                self._safe_record(session, ErrorEvent(exception=e))
            raise  # Re-raise the exception without wrapping

    async def aembed(self, input_text: str, **kwargs) -> Dict[str, Any]:
        """Asynchronous embed method."""
        init_timestamp = get_ISO_time()
        session = kwargs.pop("session", None)  # Extract and remove session from kwargs

        try:
            # Call the patched function
            response = await self._client.aembed(input_text, **kwargs)

            # Handle response and create event
            if session:
                self.handle_response(response, init_timestamp=init_timestamp, session=session)

            return response
        except Exception as e:
            if session:
                self._safe_record(session, ErrorEvent(exception=e))
            raise  # Re-raise the exception without wrapping

    def handle_response(
        self, response: Dict[str, Any], init_timestamp: str = None, session: Optional[Session] = None
    ) -> None:
        """Handle the response from the API call."""
        if not session:
            return

        # Extract usage information
        usage = response.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = prompt_tokens + completion_tokens

        # Create LLM event
        event = LLMEvent(
            init_timestamp=init_timestamp or get_ISO_time(),
            completion_timestamp=get_ISO_time(),
            provider="voyage",
            model=response.get("model", "unknown"),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost=0.0,  # Voyage AI doesn't provide cost information
            error=None,
        )

        # Add event to session
        session.add_event(event)

    def override(self):
        """Override the original SDK methods with instrumented versions."""
        self._override_sync_embed()
        self._override_async_embed()

    def _override_sync_embed(self):
        """Override synchronous embed method."""
        # Store the original method
        self.original_embed = self._client.__class__.embed

        def patched_embed(client_self, input_text: str, **kwargs):
            """Sync patched embed method."""
            try:
                return self.original_embed(client_self, input_text, **kwargs)
            except Exception as e:
                raise  # Re-raise without wrapping

        # Override method with instrumented version
        self._client.__class__.embed = patched_embed

    def _override_async_embed(self):
        """Override asynchronous embed method."""
        # Store the original method
        self.original_aembed = self._client.__class__.aembed

        async def patched_embed_async(client_self, input_text: str, **kwargs):
            """Async patched embed method."""
            try:
                return await self.original_aembed(client_self, input_text, **kwargs)
            except Exception as e:
                raise  # Re-raise without wrapping

        # Override method with instrumented version
        self._client.__class__.aembed = patched_embed_async

    def undo_override(self):
        """Restore the original SDK methods."""
        if self.original_embed is not None:
            self._client.__class__.embed = self.original_embed
        if self.original_aembed is not None:
            self._client.__class__.aembed = self.original_aembed
