"""Offline checks for chat SSO links; no .env file or live Omni calls are used."""

import base64
import hashlib
import hmac
import json
import os
import unittest
import zlib
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

with patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv"):
    import app as demo


SECRET = "synthetic-test-secret"
EMAIL = "demo+unicode@example.test"
NAME = "Zoë 李"
NOW = 1_800_000_000


def unpack(url):
    query = parse_qs(urlsplit(url).query)
    if set(query) != {"payload", "signature"}:
        raise AssertionError("Expected only v1 payload and signature parameters")
    payload = query["payload"][0]
    signature = query["signature"][0]
    expected = base64.urlsafe_b64encode(
        hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    if not hmac.compare_digest(signature, expected):
        raise AssertionError("Signature does not cover the encoded payload")
    compressed = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    return json.loads(zlib.decompress(compressed, -zlib.MAX_WBITS))


class EmbedSignatureTests(unittest.TestCase):
    def setUp(self):
        patches = [
            patch.multiple(
                demo,
                MOCK_MODE=False,
                OMNI_BASE_URL="https://example.omniapp.co",
                OMNI_EMBED_LOGIN_URL="",
                OMNI_EMBED_SECRET=SECRET,
                OMNI_EMBED_CONNECTION_ROLES="",
                OMNI_CHAT_CONTENT_PATH="/chat?chat={conversation_id}",
            ),
            patch.object(demo.time, "time", return_value=NOW + 0.75),
            patch.object(demo, "log"),
            patch("requests.sessions.Session.request", side_effect=AssertionError("Live HTTP disabled")),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_signed_payload_contains_identity_destination_and_expiry(self):
        url = demo.signed_embed_url("/chat?chat=chat-123", EMAIL, NAME)
        params = unpack(url)
        self.assertEqual(params["loginUrl"], "https://example.embed-omniapp.co/embed/login")
        self.assertEqual(params["loginUrl"], url.split("?")[0])
        self.assertEqual(params["contentPath"], "/chat?chat=chat-123")
        self.assertEqual(params["externalId"], EMAIL)
        self.assertEqual(params["name"], NAME)
        self.assertIs(type(params["exp"]), int)
        self.assertEqual(params["exp"], NOW + 24 * 60 * 60)
        self.assertRegex(params["nonce"], r"^[A-Za-z0-9_-]{32}$")
        for value in parse_qs(urlsplit(url).query).values():
            self.assertRegex(value[0], r"^[A-Za-z0-9_-]+$")

    def test_nonce_is_fresh_for_each_url(self):
        first = unpack(demo.signed_embed_url("/chat?chat=1", EMAIL, NAME))
        second = unpack(demo.signed_embed_url("/chat?chat=1", EMAIL, NAME))
        self.assertNotEqual(first["nonce"], second["nonce"])

    def test_optional_values_cannot_override_generated_claims(self):
        params = unpack(demo.signed_embed_url(
            "/chat", EMAIL, NAME,
            loginUrl="https://other.example.test/embed/login", externalId="other-user",
            nonce="reused-nonce", exp=0,
        ))
        self.assertEqual(params["loginUrl"], "https://example.embed-omniapp.co/embed/login")
        self.assertEqual(params["externalId"], EMAIL)
        self.assertEqual(params["exp"], NOW + 24 * 60 * 60)
        self.assertRegex(params["nonce"], r"^[A-Za-z0-9_-]{32}$")

    def test_optional_values_keep_json_types(self):
        optional = {
            "connectionRoles": {"connection-1": "RESTRICTED_QUERIER"},
            "userAttributes": {"region": "日本"},
            "groups": ["support", "demo"],
            "accessBoost": False,
            "entityAiCreditLimit": 0,
        }
        params = unpack(demo.signed_embed_url("/chat", EMAIL, NAME, **optional, theme="", branch=None))
        for key, value in optional.items():
            self.assertEqual(params[key], value)
        self.assertNotIn("theme", params)
        self.assertNotIn("branch", params)

    def test_api_success_uses_returned_url_without_local_signing(self):
        api_url = "https://example.embed-omniapp.co/embed/login?from-api=1"
        for response_body in (api_url, {"url": api_url}, {"embedUrl": api_url}, {"signedUrl": api_url}):
            with self.subTest(response_type=type(response_body).__name__):
                response = Mock(ok=True)
                response.json.return_value = response_body
                with patch.object(demo.requests, "post", return_value=response) as post, patch.object(demo, "signed_embed_url") as local:
                    self.assertEqual(demo.sso_url_for_chat("chat-123", EMAIL, NAME), api_url)
                    local.assert_not_called()
                    post.assert_called_once_with(
                        "https://example.omniapp.co/embed/sso/generate-url",
                        headers={"Content-Type": "application/json"},
                        json={"contentPath": "/chat?chat=chat-123", "externalId": EMAIL, "name": NAME, "secret": SECRET},
                        timeout=15,
                    )

    def test_api_errors_and_missing_url_fall_back_to_v1(self):
        http_error = Mock(ok=False, status_code=503, text="Unavailable")
        no_url = Mock(ok=True)
        no_url.json.return_value = {}
        bad_json = Mock(ok=True)
        bad_json.json.side_effect = ValueError("Invalid JSON")
        bad_shape = Mock(ok=True)
        bad_shape.json.return_value = []
        cases = [http_error, no_url, bad_json, bad_shape, demo.requests.Timeout("Timed out")]
        for response in cases:
            with self.subTest(response_type=type(response).__name__):
                post = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
                with patch.object(demo.requests, "post", post):
                    params = unpack(demo.sso_url_for_chat("chat-123", EMAIL, NAME))
                    self.assertEqual(params["contentPath"], "/chat?chat=chat-123")
                    self.assertEqual(params["externalId"], EMAIL)
                    self.assertEqual(params["name"], NAME)
                    post.assert_called_once()

    def test_fallback_preserves_custom_host_path_and_parses_environment_roles(self):
        for raw_roles in ("connection-1", '{"connection-1":"RESTRICTED_QUERIER"}'):
            with self.subTest(roles_format=raw_roles):
                roles = demo._connection_roles_json(raw_roles)
                with patch.multiple(
                    demo,
                    OMNI_EMBED_LOGIN_URL="https://analytics.example.test/embed/login/",
                    OMNI_CHAT_CONTENT_PATH="/chat?chat={conversation_id}&view=compact",
                    OMNI_EMBED_CONNECTION_ROLES=roles,
                ), patch.object(demo.requests, "post", side_effect=demo.requests.Timeout) as post:
                    url = demo.sso_url_for_chat("chat-123", EMAIL, NAME)
                    params = unpack(url)
                    self.assertEqual(params["loginUrl"], "https://analytics.example.test/embed/login")
                    self.assertEqual(params["loginUrl"], url.split("?")[0])
                    self.assertEqual(params["contentPath"], "/chat?chat=chat-123&view=compact")
                    self.assertEqual(params["connectionRoles"], {"connection-1": "RESTRICTED_QUERIER"})
                    self.assertEqual(post.call_args.kwargs["json"]["connectionRoles"], roles)

    def test_no_secret_or_conversation_skips_signing_and_api(self):
        with patch.object(demo.requests, "post") as post, patch.object(demo, "signed_embed_url") as local:
            with patch.object(demo, "OMNI_EMBED_SECRET", ""):
                self.assertIsNone(demo.sso_url_for_chat("chat-123", EMAIL, NAME))
            self.assertIsNone(demo.sso_url_for_chat(None, EMAIL, NAME))
            post.assert_not_called()
            local.assert_not_called()

    def test_generate_session_keeps_existing_api_body(self):
        response = Mock(ok=True)
        response.json.return_value = {"sessionId": "synthetic-session"}
        roles = '{"connection-1":"RESTRICTED_QUERIER"}'
        with patch.object(demo.requests, "post", return_value=response) as post:
            self.assertEqual(demo.upsert_embed_user(EMAIL, NAME, connection_roles=roles), response.json.return_value)
            post.assert_called_once_with(
                "https://example.omniapp.co/api/v1/embed/sso/generate-session",
                headers=demo.HEADERS,
                json={"contentPath": "/my", "externalId": EMAIL, "name": NAME, "connectionRoles": json.loads(roles)},
                timeout=15,
            )


if __name__ == "__main__":
    unittest.main()
