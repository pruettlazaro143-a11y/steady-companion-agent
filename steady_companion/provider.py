"""Backward-compatible Nebius configuration facade.

Core conversations consume client.complete(), not provider names. New access
profiles use chat_transport.ConnectionConfig with explicit routing fields.
Existing imports and NEBIUS_* environment behavior stay available here.
"""
from dataclasses import dataclass
import os
from .chat_transport import (ConnectionConfig, ChatCompletionsClient as Client,
    ProviderError, _endpoint, _NoRedirect, _status_error, _request_status_error,
    _validated_response, response_metadata)

DEFAULT_BASE_URL = "https://api.tokenfactory.us-central1.nebius.com/v1/"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"


@dataclass(frozen=True)
class Config(ConnectionConfig):
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    response_format: str = "json_schema"

    @classmethod
    def from_env(cls) -> "Config":
        try:
            timeout = float(os.environ.get("NEBIUS_TIMEOUT", "90"))
            max_tokens = int(os.environ.get("NEBIUS_MAX_TOKENS", "4096"))
        except (ValueError, TypeError, OverflowError):
            raise ProviderError("NEBIUS_TIMEOUT and NEBIUS_MAX_TOKENS must be valid numbers.") from None
        return cls(
            api_key=os.environ.get("NEBIUS_API_KEY", "").strip(),
            base_url=os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL).strip(),
            model=os.environ.get("NEBIUS_MODEL", DEFAULT_MODEL).strip(),
            response_format=os.environ.get("NEBIUS_RESPONSE_FORMAT", "json_schema").strip(),
            timeout=timeout,
            max_tokens=max_tokens,
        )

