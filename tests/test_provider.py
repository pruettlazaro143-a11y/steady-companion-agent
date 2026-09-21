import io
import http.client
import json
import os
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from steady_companion.provider import Client, Config, ProviderError, _NoRedirect


ANSWER = {"choices": [{"message": {"role": "assistant", "content": "聊那个游戏吧。"}}], "usage": {"total_tokens": 15}}


class Response(io.BytesIO):
    status = 200

    def __init__(self, data, url=None, status=200):
        super().__init__(data if isinstance(data, bytes) else json.dumps(data).encode())
        self.url = url
        self.status = status

    def geturl(self):
        return self.url or "https://api.tokenfactory.us-central1.nebius.com/v1/chat/completions"


class ProviderTests(unittest.TestCase):
    def test_request_uses_console_endpoint_and_bearer_without_retry(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return Response(ANSWER)

        config = Config(api_key="test-secret", timeout=12, max_tokens=100)
        result = Client(config, opener=opener).complete([{"role": "user", "content": "你好"}])
        self.assertEqual(result["choices"][0]["message"]["content"], "聊那个游戏吧。")
        self.assertEqual(len(requests), 1)
        request, timeout = requests[0]
        self.assertEqual(request.full_url, config.base_url + "chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(timeout, 12)
        body = json.loads(request.data)
        self.assertEqual(body["model"], config.model)
        self.assertEqual(body["max_tokens"], 100)
        self.assertIs(body["store"], False)
        self.assertIn("你好".encode(), request.data)
        self.assertNotIn("test-secret", repr(config))

    def test_tools_only_reply_accepted_and_reasoning_removed(self):
        call = {"id": "call_1", "type": "function", "function": {"name": "memory_search", "arguments": "{}"}}
        response = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call], "reasoning_content": "private-reasoning"}}]}
        captured = []

        def opener(request, timeout):
            captured.append(json.loads(request.data))
            return Response(response)

        result = Client(Config(api_key="secret"), opener).complete(
            [{"role": "user", "content": "记得吗"}],
            tools=[{"type": "function", "function": {"name": "memory_search"}}],
            tool_choice="auto",
        )
        self.assertEqual(result["choices"][0]["message"]["tool_calls"], [call])
        self.assertNotIn("reasoning_content", result["choices"][0]["message"])
        self.assertEqual(captured[0]["tool_choice"], "auto")

    def test_provider_errors_never_expose_body_key_or_url(self):
        for status in (301, 302, 307, 308, 400, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                calls = []

                def opener(request, timeout):
                    calls.append(request)
                    raise urllib.error.HTTPError(
                        "https://sensitive-url.invalid/secret", status, "secret-reason", {}, io.BytesIO(b"private-message test-secret")
                    )

                with self.assertRaises(ProviderError) as caught:
                    Client(Config(api_key="test-secret"), opener).complete([{"role": "user", "content": "private-message"}])
                for sensitive in ("private-message", "test-secret", "secret-reason", "sensitive-url"):
                    self.assertNotIn(sensitive, str(caught.exception))
                self.assertEqual(len(calls), 1)

    def test_redirect_handler_refuses_forwarding_credentials(self):
        request = urllib.request.Request("https://example.com/v1/chat/completions", headers={"Authorization": "Bearer private"})
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(_NoRedirect().redirect_request(request, None, code, "redirect", {}, "https://other.invalid/"))

    def test_unexpected_final_url_is_rejected(self):
        with self.assertRaises(ProviderError):
            Client(Config(api_key="secret"), lambda request, timeout: Response(ANSWER, "https://other.invalid/")).complete(
                [{"role": "user", "content": "hello"}]
            )

    def test_unsafe_base_urls_rejected(self):
        for url in (
            "http://example.com/v1", "https://user:secret@example.com/v1", "https://example.com/v1?key=secret",
            "https://example.com/v1#secret", "https://example.com/v1?", "https://example.com/v1#", "https:///v1",
            "https://example.com:99999/v1", "https://example.com\\@other.invalid/v1", "https://example.com/\nsecret",
        ):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                Config(base_url=url)

    def test_configuration_numbers_missing_key_and_header_injection(self):
        for kwargs in ({"timeout": 0}, {"timeout": float("nan")}, {"timeout": 601}, {"max_tokens": 0}, {"max_tokens": True}, {"api_key": "x\nAuthorization: bad"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ProviderError):
                Config(**kwargs)
        called = []
        with self.assertRaises(ProviderError):
            Client(Config(), lambda *args, **kwargs: called.append(True)).complete([{"role": "user", "content": "hello"}])
        self.assertEqual(called, [])
        with patch.dict(os.environ, {"NEBIUS_TIMEOUT": "secret-bad-value"}, clear=True):
            with self.assertRaises(ProviderError) as caught:
                Config.from_env()
            self.assertNotIn("secret-bad-value", str(caught.exception))

    def test_environment_config_and_custom_https_endpoint(self):
        env = {"NEBIUS_API_KEY": "test-key", "NEBIUS_BASE_URL": "https://api.tokenfactory.nebius.com/v1", "NEBIUS_MODEL": "test/model", "NEBIUS_TIMEOUT": "15.5", "NEBIUS_MAX_TOKENS": "2048"}
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        captured = []

        def opener(request, timeout):
            captured.append(request.full_url)
            return Response(ANSWER, request.full_url)

        Client(config, opener).complete([{"role": "user", "content": "hi"}])
        self.assertEqual(captured, [env["NEBIUS_BASE_URL"] + "/chat/completions"])
        self.assertEqual(config.timeout, 15.5)
        self.assertEqual(config.max_tokens, 2048)

    def test_invalid_json_and_schema_do_not_expose_response(self):
        invalid = [b"private-server-body", {}, {"choices": []}, {"choices": [{"message": {"role": "assistant", "reasoning_content": "private-reasoning"}}]}, {"choices": [{"message": {"role": "assistant", "content": ["unexpected"]}}]}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ProviderError) as caught:
                Client(Config(api_key="secret"), lambda request, timeout: Response(payload)).complete([{"role": "user", "content": "hi"}])
            self.assertNotIn("private-", str(caught.exception))

    def test_timeout_and_network_error_have_no_raw_exception(self):
        for error in (TimeoutError("secret-host"), urllib.error.URLError("secret-key")):
            calls = []

            def opener(request, timeout):
                calls.append(True)
                raise error

            with self.assertRaises(ProviderError) as caught:
                Client(Config(api_key="secret"), opener).complete([{"role": "user", "content": "hello"}])
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(len(calls), 1)

    def test_interrupted_response_body_becomes_safe_provider_error(self):
        class BrokenResponse(Response):
            def read(self, size=-1):
                raise http.client.IncompleteRead(b"private-partial-answer")

        with self.assertRaises(ProviderError) as caught:
            Client(Config(api_key="secret"), lambda request, timeout: BrokenResponse(ANSWER)).complete(
                [{"role": "user", "content": "hello"}]
            )
        self.assertNotIn("private-partial-answer", str(caught.exception))
        self.assertIn("No automatic retry", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
