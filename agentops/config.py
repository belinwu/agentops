import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set, TypedDict, Union
from uuid import UUID

from .helpers import get_env_bool, get_env_int, get_env_list
from .logging.config import logger


class ConfigDict(TypedDict):
    api_key: Optional[str]
    parent_key: Optional[str]
    endpoint: Optional[str]
    max_wait_time: Optional[int]
    max_queue_size: Optional[int]
    default_tags: Optional[List[str]]
    instrument_llm_calls: Optional[bool]
    auto_start_session: Optional[bool]
    skip_auto_end_session: Optional[bool]
    env_data_opt_out: Optional[bool]
    log_level: Optional[Union[str, int]]
    fail_safe: Optional[bool]


@dataclass
class Config:
    # API key for authentication with AgentOps services
    api_key: Optional[str] = field(default_factory=lambda: os.getenv('AGENTOPS_API_KEY'))
    
    # Parent API key for hierarchical organization of sessions
    parent_key: Optional[str] = field(default_factory=lambda: os.getenv('AGENTOPS_PARENT_KEY'))
    
    # Base URL for the AgentOps API
    endpoint: str = field(
        default_factory=lambda: os.getenv('AGENTOPS_API_ENDPOINT', 'https://api.agentops.ai')
    )
    
    # Maximum time in milliseconds to wait for API responses
    max_wait_time: int = field(
        default_factory=lambda: get_env_int('AGENTOPS_MAX_WAIT_TIME', 5000)
    )
    
    # Maximum number of events to queue before forcing a flush
    max_queue_size: int = field(
        default_factory=lambda: get_env_int('AGENTOPS_MAX_QUEUE_SIZE', 512)
    )
    
    # Default tags to apply to all sessions
    default_tags: Set[str] = field(
        default_factory=lambda: get_env_list('AGENTOPS_DEFAULT_TAGS')
    )
    
    # Whether to automatically instrument and track LLM API calls
    instrument_llm_calls: bool = field(
        default_factory=lambda: get_env_bool('AGENTOPS_INSTRUMENT_LLM_CALLS', True)
    )
    
    # Whether to automatically start a session when initializing
    auto_start_session: bool = field(
        default_factory=lambda: get_env_bool('AGENTOPS_AUTO_START_SESSION', True)
    )
    
    # Whether to skip automatically ending sessions on program exit
    skip_auto_end_session: bool = field(
        default_factory=lambda: get_env_bool('AGENTOPS_SKIP_AUTO_END_SESSION', False)
    )
    
    # Whether to opt out of collecting environment data
    env_data_opt_out: bool = field(
        default_factory=lambda: get_env_bool('AGENTOPS_ENV_DATA_OPT_OUT', False)
    )
    
    # Logging level for AgentOps logs
    log_level: Union[str, int] = field(
        default_factory=lambda: os.getenv('AGENTOPS_LOG_LEVEL', 'CRITICAL')
    )
    
    # Whether to suppress errors and continue execution when possible
    fail_safe: bool = field(
        default_factory=lambda: get_env_bool('AGENTOPS_FAIL_SAFE', False)
    )

    def configure(
        self,
        client: Any,
        api_key: Optional[str] = None,
        parent_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        max_wait_time: Optional[int] = None,
        max_queue_size: Optional[int] = None,
        default_tags: Optional[List[str]] = None,
        instrument_llm_calls: Optional[bool] = None,
        auto_start_session: Optional[bool] = None,
        skip_auto_end_session: Optional[bool] = None,
        env_data_opt_out: Optional[bool] = None,
        log_level: Optional[Union[str, int]] = None,
        fail_safe: Optional[bool] = None,
    ):
        """Configure settings from kwargs, validating where necessary"""
        if api_key is not None:
            try:
                UUID(api_key)
                self.api_key = api_key
            except ValueError:
                message = f"API Key is invalid: {{{api_key}}}.\n\t    Find your API key at {self.endpoint}/settings/projects"
                client.add_pre_init_warning(message)
                logger.error(message)

        if parent_key is not None:
            try:
                UUID(parent_key)
                self.parent_key = parent_key
            except ValueError:
                message = f"Parent Key is invalid: {parent_key}"
                client.add_pre_init_warning(message)
                logger.warning(message)

        if endpoint is not None:
            self.endpoint = endpoint

        if max_wait_time is not None:
            self.max_wait_time = max_wait_time

        if max_queue_size is not None:
            self.max_queue_size = max_queue_size

        if default_tags is not None:
            self.default_tags = set(default_tags)

        if instrument_llm_calls is not None:
            self.instrument_llm_calls = instrument_llm_calls

        if auto_start_session is not None:
            self.auto_start_session = auto_start_session

        if skip_auto_end_session is not None:
            self.skip_auto_end_session = skip_auto_end_session

        if env_data_opt_out is not None:
            self.env_data_opt_out = env_data_opt_out

        if log_level is not None:
            if isinstance(log_level, str):
                level = log_level.upper()
                if hasattr(logging, level):
                    self.log_level = getattr(logging, level)
                else:
                    message = f"Invalid log level: {log_level}"
                    client.add_pre_init_warning(message)
                    logger.warning(message)
            elif isinstance(log_level, int):
                self.log_level = log_level
            else:
                message = f"Log level must be string or int, got {type(log_level)}"
                client.add_pre_init_warning(message)
                logger.warning(message)

        if fail_safe is not None:
            self.fail_safe = fail_safe


TESTING = "pytest" in sys.modules


if TESTING:
    def hook_pdb():
        import sys
        def info(type, value, tb):
            if hasattr(sys, "ps1") or not sys.stderr.isatty():
                sys.__excepthook__(type, value, tb)
            else:
                import pdb
                import traceback
                traceback.print_exception(type, value, tb)
                pdb.post_mortem(tb)
        sys.excepthook = info

    hook_pdb()
