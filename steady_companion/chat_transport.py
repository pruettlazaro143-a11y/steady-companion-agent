"""Provider-neutral synchronous Chat Completions transport using only the Python standard library.

API keys never appear in configuration reprs or public exceptions. Requests are
not retried automatically, and redirects are refused to keep credentials on the
configured HTTPS endpoint. The application, rather than this module, executes
any tool calls returned by the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import json
import math
import socket
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
GENERIC_POLICY = "chat-completions-v1"
DEEPSEEK_POLICY = "deepseek-official-v1"


def validate_policy(config, policy):
    """Explicit host policy, never inferred from a model name or URL."""
    if policy not in (GENERIC_POLICY, DEEPSEEK_POLICY):
        raise ProviderError("Unknown transport policy.", code="configuration")
    if policy == DEEPSEEK_POLICY:
        if config.response_format != "json_object":
            raise ProviderError("DeepSeek official policy requires json_object for structured auxiliaries; "
                                "json_schema is not supported by this adapter. No request or fallback.", code="configuration")
        if (config.base_url != "https://api.deepseek.com" or config.model != "deepseek-flash"
                or config.max_tokens != 4096 or config.timeout != 90):
            raise ProviderError("DeepSeek official policy requires the documented endpoint, Flash route "
                                "and host limits 4096 tokens / 90 seconds.", code="configuration")


class ProviderError(RuntimeError):
    """A safe, user-facing configuration, transport, or response error."""
    def __init__(self,message,*,code='unknown'):
        super().__init__(message)
        self.code=code



def _endpoint(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url:
        raise ProviderError("base_url must be an HTTPS base URL.")
    if any(c.isspace() or ord(c) < 32 for c in base_url):
        raise ProviderError("base_url must not contain whitespace or control characters.")
    try:
        parts = urllib.parse.urlsplit(base_url)
        port = parts.port
        valid = (
            parts.scheme == "https"
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and "?" not in base_url
            and "#" not in base_url
            and "\\" not in base_url
            and (port is None or 1 <= port <= 65535)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ProviderError(
            "base_url must use HTTPS without embedded credentials, a query, or a fragment."
        )
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path.rstrip("/") + "/chat/completions", "", "")
    )


@dataclass(frozen=True)
class ConnectionConfig:
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    model: str = ""
    timeout: float = 90.0
    max_tokens: int = 4096
    response_format: str = "off"

    def __post_init__(self) -> None:
        _endpoint(self.base_url)
        if self.response_format not in ("off", "json_object", "json_schema"):
            raise ProviderError("response_format must be off, json_object, or json_schema.", code="configuration")
        if not isinstance(self.api_key, str) or any(
            c.isspace() or ord(c) < 32 or ord(c) > 126 for c in self.api_key
        ):
            raise ProviderError("the configured credential must be a single non-whitespace ASCII token.")
        if not isinstance(self.model, str) or not self.model.strip() or any(
            ord(c) < 32 for c in self.model
        ):
            raise ProviderError("model must be a nonempty model routing key.")
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
            or not 0 < self.timeout <= 600
        ):
            raise ProviderError("timeout must be a number greater than 0 and at most 600 seconds.")
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int) or self.max_tokens < 1:
            raise ProviderError("max_tokens must be a positive integer.")



class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _status_error(status: int) -> ProviderError:
    if 300 <= status < 400:
        message = "The endpoint requested a redirect; it was refused. Check the HTTPS base URL with your inference provider."
    elif status == 401:
        message = "Inference service rejected the API key (401). Check the configured credential locally."
    elif status == 403:
        message = "Inference service denied access (403). Check your project's model access and permissions."
    elif status == 404:
        message = "Inference service could not find the endpoint or model (404). Copy the base URL and routing key from your console."
    elif status == 429:
        message = "Inference service limited this request (429). Check your quota or credit, and try again later."
    elif 500 <= status < 600:
        message = "Inference service is temporarily unavailable. Try again later."
    else:
        message = "Inference service rejected the request. Check the model's supported parameters and your configuration."
    error=ProviderError(message + " No automatic retry was made.",code="http")
    error.http_status=status if 300<=status<=599 else None
    return error


def _request_status_error(status, response_format):
    error = _status_error(status)
    if status in (400, 422) and response_format is not None:
        error = ProviderError(
            "Structured request rejected. Check model/endpoint response_format support and schema configuration; "
            "capability is unverified. No mode switch or retry was made.", code="http")
        error.http_status = status
        error.configuration_hint = "check_response_format_capability"
    return error


def _validated_response(payload: Any) -> dict[str, Any]:
    error = "Inference service returned an unexpected response format. No automatic retry was made."
    if not isinstance(payload, dict):
        raise ProviderError(error,code="response_shape")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError(error,code="response_shape")
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ProviderError(error,code="response_shape")
    content = message.get("content")
    calls = message.get("tool_calls")
    if content is not None and not isinstance(content, str):
        raise ProviderError(error,code="response_shape")
    if calls is not None:
        if not isinstance(calls, list):
            raise ProviderError(error,code="response_shape")
        for call in calls:
            if not isinstance(call, dict):
                raise ProviderError(error,code="response_shape")
            function = call.get("function")
            if (
                call.get("type") != "function"
                or not isinstance(call.get("id"), str)
                or not call["id"]
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not function["name"]
                or not isinstance(function.get("arguments"), str)
            ):
                raise ProviderError(error,code="response_shape")
    if not (isinstance(content, str) and content.strip()) and not calls:
        raise ProviderError(
            "The model returned no visible answer or tool call. Check the token budget and model settings. "
            "No automatic retry was made.",code="visible_text"
        )
    if "usage" in payload and payload["usage"] is not None and not isinstance(payload["usage"], dict):
        raise ProviderError(error,code="response_shape")
    # Reasoning is never substituted for an answer, nor passed on for display.
    for choice in choices:
        if isinstance(choice, dict) and isinstance(choice.get("message"), dict):
            choice["message"].pop("reasoning_content", None)
            choice["message"].pop("reasoning", None)
    return payload


class ChatCompletionsClient:
    def __init__(self, config: ConnectionConfig, opener: Any = None, *, policy=GENERIC_POLICY):
        validate_policy(config, policy)
        self.policy = policy
        self.config = config
        self._url = _endpoint(config.base_url)
        transport = opener if opener is not None else urllib.request.build_opener(_NoRedirect())
        self._open = transport.open if hasattr(transport, "open") else transport

    def complete(
        self,
        messages: list,
        tools: list | None = None,
        tool_choice: str | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> dict[str, Any]:
        if not self.config.api_key:
            raise ProviderError("Set the configured credential locally before starting a live conversation.")
        if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) for m in messages):
            raise ProviderError("A chat request needs a nonempty list of message objects.")
        if tools is not None and (not isinstance(tools, list) or any(not isinstance(t, dict) for t in tools)):
            raise ProviderError("Tools must be a list of tool definitions.")
        if tool_choice is not None and tool_choice not in ("auto", "none", "required"):
            raise ProviderError("tool_choice must be auto, none, or required.")
        if timeout is not None and (type(timeout) not in (int,float) or not 0<timeout<=self.config.timeout):
            raise ProviderError('Invalid bounded request timeout.')
        if max_tokens is not None and (type(max_tokens)!=int or not 1<=max_tokens<=self.config.max_tokens):
            raise ProviderError('Invalid bounded token limit.')
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
            "store": False,
        }
        if self.policy == DEEPSEEK_POLICY:
            if temperature is not None or top_p is not None:
                raise ProviderError("DeepSeek official policy uses default sampling; overrides are refused.", code="configuration")
            if response_format is not None and response_format != {"type": "json_object"}:
                raise ProviderError("DeepSeek official structured calls require json_object; no format fallback.", code="configuration")
            del body["store"]
            body["thinking"] = {"type": "disabled"}
        # Opt-in only: existing callers keep their exact wire contract.
        for name, value, upper in (("temperature", temperature, 2), ("top_p", top_p, 1)):
            if value is not None:
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= upper:
                    raise ProviderError("Invalid explicit sampling configuration.", code="configuration")
                body[name] = value
        if response_format is not None:
            from .response_formats import validate_format
            validate_format(response_format)
            body["response_format"] = response_format
        if tools is not None:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        try:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            raise ProviderError("The chat request could not be encoded as JSON.") from None
        request = urllib.request.Request(
            self._url,
            data=encoded,
            headers={
                "Authorization": "Bearer " + self.config.api_key,
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "steady-companion-agent/0.1",
            },
            method="POST",
        )
        try:
            with self._open(request, timeout=timeout if timeout is not None else self.config.timeout) as response:
                status = getattr(response, "status", 200)
                if not isinstance(status, int) or not 200 <= status < 300:
                    raise _request_status_error(status if isinstance(status, int) else 0, response_format)
                # Defense in depth for an injected transport with redirect support.
                final_url = response.geturl() if hasattr(response, "geturl") else self._url
                if final_url != self._url:
                    raise ProviderError("The response URL changed unexpectedly. Check your configured transport.")
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Do not read or report the provider's raw error body or reason.
            code = exc.code
            exc.close()
            error = _request_status_error(code, response_format)
            raise error from None
        except (TimeoutError, socket.timeout):
            raise ProviderError("The Inference service request timed out. No automatic retry was made; the provider may have processed the request.",code="timeout") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason,(TimeoutError,socket.timeout)):
                raise ProviderError("The Inference service request timed out. No automatic retry was made.",code="timeout") from None
            raise ProviderError("Could not securely connect to Inference service. Check your network and HTTPS base URL. No automatic retry was made.",code="transport") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise ProviderError("The Inference service connection failed. No automatic retry was made; the provider may have processed the request.",code="transport") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ProviderError("The Inference service response exceeded the supported size. No automatic retry was made.",code="response_size")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError, TypeError, RecursionError):
            raise ProviderError("Inference service returned invalid JSON. No automatic retry was made.",code="json_parse") from None
        try:
            return _validated_response(payload)
        except ProviderError as exc:
            exc.response_metadata = response_metadata(payload, (self.config.api_key,))
            usage=payload.get('usage') if isinstance(payload,dict) else None
            if isinstance(usage,dict):
                exc.reported_usage={k:v for k,v in usage.items() if k in ('prompt_tokens','completion_tokens','total_tokens') and type(v)==int and v>=0}
            raise

    def list_models(self) -> set[str]:
        """One read-only GET on this configured endpoint; no pagination or retry."""
        if not self.config.api_key:
            raise ProviderError("A local credential is required.", code="configuration")
        url = self._url.removesuffix("chat/completions") + "models"
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.config.api_key,
            "Accept": "application/json", "User-Agent": "steady-companion-agent/0.1"}, method="GET")
        try:
            with self._open(request, timeout=self.config.timeout) as response:
                status = getattr(response, "status", 200)
                if type(status) is not int or not 200 <= status < 300:
                    raise _status_error(status if type(status) is int else 0)
                if hasattr(response, "geturl") and response.geturl() != url:
                    raise ProviderError("Model list redirect refused.", code="configuration")
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise _status_error(status) from None
        except (TimeoutError, socket.timeout):
            raise ProviderError("Model list timed out; no retry.", code="timeout") from None
        except urllib.error.URLError as exc:
            code = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "transport"
            raise ProviderError("Model list unavailable; no retry.", code=code) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise ProviderError("Model list transport failed; no retry.", code="transport") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ProviderError("Model list too large.", code="response_size")
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
            data = payload["data"]
            if payload.get("object") != "list" or not isinstance(data, list) or payload.get("has_more"):
                raise ValueError
            ids = [row["id"] for row in data]
            if any(not isinstance(x, str) or not x or len(x) > 256 for x in ids) or len(ids) != len(set(ids)):
                raise ValueError
            return set(ids)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise ProviderError("Model availability could not be verified.", code="response_shape") from None


def response_metadata(payload, secrets=()):
    """Only bounded routing identity and finish enum; never raw body or reasoning."""
    import re
    result = {"reported_model": None, "finish_reason": None, "visible_content_chars": None}
    if not isinstance(payload, dict):
        return result
    model = payload.get("model")
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_./:@+-]{1,256}", model) and not any(s and s in model for s in secrets):
        result["reported_model"] = model
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            result["visible_content_chars"] = len(message["content"])
    finish = choices[0].get("finish_reason") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    if finish in ("stop", "length", "tool_calls", "content_filter", "function_call"):
        result["finish_reason"] = finish
    return result
