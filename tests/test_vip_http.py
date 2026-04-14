import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytz
from requests.cookies import RequestsCookieJar

MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "frontline-pass.py"
SPEC = importlib.util.spec_from_file_location("frontline_pass_module", MODULE_PATH)
frontline_pass = importlib.util.module_from_spec(SPEC)
import sys

sys.modules[SPEC.name] = frontline_pass
SPEC.loader.exec_module(frontline_pass)  # type: ignore[union-attr]

AppConfig = frontline_pass.AppConfig
HttpCredentials = frontline_pass.HttpCredentials
VipHttpClient = frontline_pass.VipHttpClient
VipHTTPError = frontline_pass.VipHTTPError
VipService = frontline_pass.VipService
RollingWindowLimiter = frontline_pass.RollingWindowLimiter
FrontlinePassBot = frontline_pass.FrontlinePassBot
QuickVipView = frontline_pass.QuickVipView
SwitchMeView = frontline_pass.SwitchMeView
PlayerVipStatus = frontline_pass.PlayerVipStatus
TeamSwitchResult = frontline_pass.TeamSwitchResult
QUICK_VIP_GIVER_LIMIT_WINDOW_HOURS = frontline_pass.QUICK_VIP_GIVER_LIMIT_WINDOW_HOURS
LEGACY_QUICK_VIP_GIVER_ROLE_NAMES = frontline_pass.LEGACY_QUICK_VIP_GIVER_ROLE_NAMES
LEGACY_QUICK_VIP_GIVER_LIMIT_PER_WINDOW = frontline_pass.LEGACY_QUICK_VIP_GIVER_LIMIT_PER_WINDOW


class DummyResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict,
        *,
        text: str | None = None,
        cookies: RequestsCookieJar | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else "response-text"
        self.cookies = cookies or RequestsCookieJar()

    def json(self) -> dict:
        return self._payload


class DummySession:
    def __init__(self, responses) -> None:
        if isinstance(responses, DummyResponse):
            responses = [responses]
        self.responses = list(responses)
        self.calls = []
        self.verify = True
        self.cookies = RequestsCookieJar()

    def _next_response(self) -> DummyResponse:
        if self.responses:
            response = self.responses.pop(0)
        else:
            raise AssertionError("Unexpected HTTP request in DummySession")
        if response.cookies:
            for key, value in response.cookies.items():
                self.cookies.set(key, value)
        return response

    def request(self, method, url, json=None, headers=None, timeout=None, params=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
                "params": params,
            }
        )
        return self._next_response()

    def post(self, url, json=None, headers=None, timeout=None, params=None):
        return self.request("POST", url, json=json, headers=headers, timeout=timeout, params=params)


class DummyGuildPermissions:
    def __init__(self, *, administrator: bool = False) -> None:
        self.administrator = administrator


class DummyRole:
    def __init__(self, role_id: int, name: str) -> None:
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"


class DummyMember:
    def __init__(
        self,
        user_id: int,
        display_name: str,
        *,
        roles: list | None = None,
        administrator: bool = False,
    ) -> None:
        self.id = user_id
        self.display_name = display_name
        self.roles = roles or []
        self.guild_permissions = DummyGuildPermissions(administrator=administrator)
        self.mention = f"<@{user_id}>"
        self.added_roles = []
        self.removed_roles = []
        self.sent_messages = []

    async def add_roles(self, role, *, reason=None) -> None:
        self.added_roles.append((role, reason))

    async def remove_roles(self, role, *, reason=None) -> None:
        self.removed_roles.append((role, reason))

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)


class DummyGuild:
    def __init__(self, roles: list | None = None) -> None:
        self._roles = {role.id: role for role in (roles or [])}

    def get_role(self, role_id: int):
        return self._roles.get(role_id)


class DummyResponseController:
    def __init__(self) -> None:
        self.messages = []
        self.deferred = []
        self._done = False

    async def send_message(self, content: str, **kwargs) -> None:
        self.messages.append((content, kwargs))
        self._done = True

    async def defer(self, **kwargs) -> None:
        self.deferred.append(kwargs)
        self._done = True

    def is_done(self) -> bool:
        return self._done


class DummyFollowupController:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, content: str, **kwargs):
        self.messages.append((content, kwargs))
        return {"content": content, "kwargs": kwargs}


class DummyChannel:
    def __init__(self) -> None:
        self.messages = []
        self.id = 999

    async def send(self, content: str) -> None:
        self.messages.append(content)


class DummyInteraction:
    def __init__(self, *, user, guild=None, channel=None) -> None:
        self.user = user
        self.guild = guild
        self.channel = channel or DummyChannel()
        self.response = DummyResponseController()
        self.followup = DummyFollowupController()

    async def original_response(self):
        return mock.AsyncMock()


class VipHttpClientTests(unittest.TestCase):
    def test_add_vip_uses_bearer_token(self) -> None:
        session = DummySession(DummyResponse(200, {"result": "ok"}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            timeout=5.0,
            session=session,
        )

        result = client.add_vip("player-id", "desc", "2025-10-31T12:00:00Z")

        self.assertEqual(result["result"], "ok")
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["url"], "https://example/api/add_vip")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["timeout"], 5.0)
        self.assertEqual(
            call["json"],
            {
                "player_id": "player-id",
                "description": "desc",
                "expiration": "2025-10-31T12:00:00Z",
            },
        )
        self.assertIn("Authorization", call["headers"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer abc123")

    def test_add_vip_includes_player_name(self) -> None:
        session = DummySession(DummyResponse(200, {"result": "ok"}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        client.add_vip("player-id", "desc", None, player_name="GBONE001")

        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(
            call["json"],
            {
                "player_id": "player-id",
                "description": "desc",
                "player_name": "GBONE001",
            },
        )

    def test_add_vip_rejects_non_200(self) -> None:
        session = DummySession(DummyResponse(401, {"error": "unauthorized"}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        with self.assertRaises(VipHTTPError):
            client.add_vip("player-id", "desc", None)

    def test_bearer_token_preferred_over_login(self) -> None:
        session = DummySession(DummyResponse(200, {"result": "ok"}))
        credentials = HttpCredentials(
            base_url="https://example/api",
            bearer_token="abc123",
            username="user",
            password="pass",
        )
        client = VipHttpClient(credentials, session=session)

        client.add_vip("player-id", "desc", None)

        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("login", session.calls[0]["url"])

    def test_logs_in_and_uses_session_cookie(self) -> None:
        cookie_jar = RequestsCookieJar()
        cookie_jar.set("sessionid", "session-cookie")
        cookie_jar.set("csrftoken", "csrf-token")
        login_response = DummyResponse(
            200,
            {"result": True, "failed": False},
            text="login-success",
            cookies=cookie_jar,
        )
        api_response = DummyResponse(200, {"result": "ok"})
        session = DummySession([login_response, api_response])
        credentials = HttpCredentials(
            base_url="https://example",
            username="user",
            password="pass",
        )
        client = VipHttpClient(credentials, session=session)

        result = client.add_vip("player-id", "desc", None)

        self.assertEqual(result["result"], "ok")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["url"], "https://example/api/login")
        self.assertEqual(session.calls[1]["url"], "https://example/api/add_vip")
        self.assertEqual(session.calls[1]["headers"]["Referer"], "https://example")

    def test_get_player_profile_fetches_profile(self) -> None:
        session = DummySession(DummyResponse(200, {"result": {"player_id": "player-id"}}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        profile = client.get_player_profile("player-id")

        self.assertEqual(profile["player_id"], "player-id")
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://example/api/get_player_profile")
        self.assertEqual(
            call["params"],
            {
                "player_id": "player-id",
                "num_sessions": 10,
            },
        )

    def test_get_player_profile_raises_on_failure(self) -> None:
        session = DummySession(DummyResponse(403, {"result": None, "error": "nope"}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        with self.assertRaises(VipHTTPError):
            client.get_player_profile("player-id")

    def test_get_players_fetches_players(self) -> None:
        session = DummySession(
            DummyResponse(
                200,
                {
                    "result": [
                        {"player_id": "p1", "team": "Axis"},
                        {"player_id": "p2", "team": "Allies"},
                    ]
                },
            )
        )
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        players = client.get_players()

        self.assertEqual(len(players), 2)
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(session.calls[0]["url"], "https://example/api/get_players")

    def test_message_player_posts_payload(self) -> None:
        session = DummySession(DummyResponse(200, {"result": True}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        sent = client.message_player(
            player_id="p1",
            message="Hello team",
            by="BotAdmin",
            save_message=False,
            player_name="PlayerOne",
        )

        self.assertTrue(sent)
        self.assertEqual(session.calls[0]["url"], "https://example/api/message_player")
        self.assertEqual(
            session.calls[0]["json"],
            {
                "player_id": "p1",
                "message": "Hello team",
                "by": "BotAdmin",
                "save_message": False,
                "player_name": "PlayerOne",
            },
        )

    def test_get_gamestate_fetches_result(self) -> None:
        session = DummySession(
            DummyResponse(
                200,
                {"result": {"num_axis_players": 48, "num_allied_players": 46}},
            )
        )
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        gamestate = client.get_gamestate()

        self.assertEqual(gamestate["num_axis_players"], 48)
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(session.calls[0]["url"], "https://example/api/get_gamestate")

    def test_switch_player_now_posts_payload(self) -> None:
        session = DummySession(DummyResponse(200, {"result": True}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        result = client.switch_player_now("player-id")

        self.assertTrue(result["result"])
        self.assertEqual(session.calls[0]["url"], "https://example/api/switch_player_now")
        self.assertEqual(session.calls[0]["json"], {"player_id": "player-id"})

    def test_switch_player_now_raises_on_failure(self) -> None:
        session = DummySession(DummyResponse(403, {"failed": True, "error": "forbidden"}))
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        with self.assertRaises(VipHTTPError):
            client.switch_player_now("player-id")

    def test_set_broadcast_posts_message(self) -> None:
        session = DummySession(
            [
                DummyResponse(200, {"result": "previous-message"}),
                DummyResponse(200, {"result": "Server notice"}),
            ]
        )
        client = VipHttpClient(
            HttpCredentials(base_url="https://example/api", bearer_token="abc123"),
            session=session,
        )

        result = client.set_broadcast("Server notice")

        self.assertEqual(
            result,
            {
                "previous": "previous-message",
                "current": "Server notice",
            },
        )
        self.assertEqual(session.calls[0]["url"], "https://example/api/set_broadcast")
        self.assertEqual(session.calls[0]["json"], {"message": "Server notice"})
        self.assertEqual(session.calls[1]["method"], "GET")
        self.assertEqual(session.calls[1]["url"], "https://example/api/get_broadcast_message")


class VipServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(
            discord_token="token",
            vip_duration_hours=4,
            channel_id=1,
            timezone=pytz.UTC,
            timezone_name="UTC",
            state_directory=pathlib.Path("."),
            http_credentials=HttpCredentials(
                base_url="https://example",
                bearer_token="abc123",
            ),
        )

    def test_grant_vip_uses_http_client(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_player_profile.return_value = {}
        fake_http_client.add_vip.return_value = {"result": "ok"}
        service._http_client = fake_http_client  # type: ignore[attr-defined]
        fixed_now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        service._now_utc = mock.Mock(return_value=fixed_now)  # type: ignore[attr-defined]

        result = service.grant_vip(
            "steam123",
            duration_hours=4,
            local_timezone=pytz.UTC,
            requester_display_name="GBONE",
        )

        fake_http_client.add_vip.assert_called_once()
        args, kwargs = fake_http_client.add_vip.call_args
        fake_http_client.get_player_profile.assert_called_once_with("steam123", num_sessions=10)
        expected_expiration = fixed_now + timedelta(hours=4)
        self.assertEqual(args[0], "steam123")
        self.assertIn("Discord VIP for GBONE", args[1])
        self.assertEqual(args[2], expected_expiration.isoformat())
        self.assertIsNone(kwargs.get("player_name"))
        self.assertIn("HTTP API", result.status_lines[0])
        self.assertEqual(result.expiration_utc, expected_expiration)
        self.assertEqual(result.expiration_local, expected_expiration)

    def test_grant_vip_forwards_player_name(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_player_profile.return_value = {}
        fake_http_client.add_vip.return_value = {"result": "ok"}
        service._http_client = fake_http_client  # type: ignore[attr-defined]
        service._now_utc = mock.Mock(return_value=datetime(2030, 1, 1, tzinfo=timezone.utc))  # type: ignore[attr-defined]

        service.grant_vip(
            "steam123",
            duration_hours=1,
            local_timezone=pytz.UTC,
            requester_display_name="GBONE",
            player_name="GBONE001",
        )

        fake_http_client.add_vip.assert_called_once()
        _, kwargs = fake_http_client.add_vip.call_args
        self.assertEqual(kwargs.get("player_name"), "GBONE001")

    def test_grant_fixed_vip_does_not_fetch_or_extend_existing_expiration(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.add_vip.return_value = {"result": "ok"}
        service._http_client = fake_http_client  # type: ignore[attr-defined]
        fixed_now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        service._now_utc = mock.Mock(return_value=fixed_now)  # type: ignore[attr-defined]

        result = service.grant_fixed_vip(
            "steam123",
            duration_minutes=10,
            local_timezone=pytz.UTC,
            requester_display_name="GBONE",
        )

        fake_http_client.get_player_profile.assert_not_called()
        fake_http_client.add_vip.assert_called_once()
        args, kwargs = fake_http_client.add_vip.call_args
        expected_expiration = fixed_now + timedelta(minutes=10)
        self.assertEqual(args[0], "steam123")
        self.assertIn("Quick VIP from Discord by GBONE", args[1])
        self.assertEqual(args[2], expected_expiration.isoformat())
        self.assertIsNone(kwargs.get("player_name"))
        self.assertEqual(result.expiration_utc, expected_expiration)
        self.assertEqual(result.expiration_local, expected_expiration)

    def test_grant_vip_extends_existing_expiration(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_player_profile.return_value = {
            "vips": [
                {"expiration": "2031-01-01T00:00:00+00:00"},
                {"expiration": "2030-12-01T00:00:00+00:00"},
            ]
        }
        fake_http_client.add_vip.return_value = {"result": "ok"}
        service._http_client = fake_http_client  # type: ignore[attr-defined]
        service._now_utc = mock.Mock(return_value=datetime(2030, 6, 1, tzinfo=timezone.utc))  # type: ignore[attr-defined]
        local_tz = pytz.timezone("Australia/Sydney")

        result = service.grant_vip(
            "steam123",
            duration_hours=2,
            local_timezone=local_tz,
            requester_display_name="GBONE",
        )

        base = datetime.fromisoformat("2031-01-01T00:00:00+00:00")
        expected_expiration = base + timedelta(hours=2)
        self.assertEqual(result.expiration_utc, expected_expiration)
        self.assertEqual(result.expiration_local, expected_expiration.astimezone(local_tz))
        self.assertEqual(
            fake_http_client.add_vip.call_args[0][2],
            expected_expiration.isoformat(),
        )

    def test_grant_vip_extends_nested_player_vip_expiration(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_player_profile.return_value = {
            "player": {
                "player_vip": {
                    "expiration": "2031-01-01T00:00:00+00:00",
                }
            }
        }
        fake_http_client.add_vip.return_value = {"result": "ok"}
        service._http_client = fake_http_client  # type: ignore[attr-defined]
        service._now_utc = mock.Mock(return_value=datetime(2030, 6, 1, tzinfo=timezone.utc))  # type: ignore[attr-defined]

        result = service.grant_vip(
            "steam123",
            duration_hours=72,
            local_timezone=pytz.UTC,
            requester_display_name="GBONE",
        )

        expected_expiration = datetime.fromisoformat("2031-01-01T00:00:00+00:00") + timedelta(hours=72)
        self.assertEqual(result.expiration_utc, expected_expiration)
        self.assertEqual(
            fake_http_client.add_vip.call_args[0][2],
            expected_expiration.isoformat(),
        )

    def test_get_player_vip_status_returns_expiration(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_player_profile.return_value = {
            "vips": [
                {"expiration": "2032-05-01T10:00:00+00:00"},
            ]
        }
        service._http_client = fake_http_client  # type: ignore[attr-defined]

        status = service.get_player_vip_status("steam123")

        self.assertEqual(status.player_id, "steam123")
        self.assertEqual(status.expiration_utc, datetime.fromisoformat("2032-05-01T10:00:00+00:00"))
        fake_http_client.get_player_profile.assert_called_once_with("steam123", num_sessions=10)

    def test_get_player_vip_status_reads_nested_vip_shape(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_player_profile.return_value = {
            "profile": {
                "current_vip": {
                    "expiration": "2032-05-01T10:00:00+00:00",
                }
            }
        }
        service._http_client = fake_http_client  # type: ignore[attr-defined]

        status = service.get_player_vip_status("steam123")

        self.assertEqual(status.player_id, "steam123")
        self.assertEqual(status.expiration_utc, datetime.fromisoformat("2032-05-01T10:00:00+00:00"))

    def test_get_player_vip_status_handles_missing_entries(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_player_profile.return_value = {}
        service._http_client = fake_http_client  # type: ignore[attr-defined]

        status = service.get_player_vip_status("steam123")

        self.assertEqual(status.player_id, "steam123")
        self.assertIsNone(status.expiration_utc)

    def test_message_team_axis(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_players.return_value = [
            {"player_id": "a1", "team": "Axis", "player_name": "AxisOne"},
            {"player_id": "a2", "team": "Axis", "player_name": "AxisTwo"},
            {"player_id": "l1", "team": "Allies", "player_name": "AllyOne"},
        ]
        fake_http_client.message_player.return_value = True
        service._http_client = fake_http_client  # type: ignore[attr-defined]

        result = service.message_team("axis", "Push now", "Moderator")

        self.assertEqual(result.recipient, "axis")
        self.assertEqual(result.attempted, 2)
        self.assertEqual(result.sent, 2)
        self.assertEqual(result.failed, 0)
        self.assertEqual(fake_http_client.message_player.call_count, 2)
        fake_http_client.set_broadcast.assert_not_called()

    def test_message_team_both_ignores_unknown_entries(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_players.return_value = [
            {"player_id": "a1", "team": "Axis"},
            {"team": "Axis"},
            {"player_id": "l1", "team": "Allies"},
            "invalid",
        ]
        fake_http_client.message_player.return_value = True
        service._http_client = fake_http_client  # type: ignore[attr-defined]

        result = service.message_team("both", "All players", "Moderator")

        self.assertEqual(result.attempted, 3)
        self.assertEqual(result.sent, 2)
        self.assertEqual(result.failed, 1)
        self.assertEqual(fake_http_client.message_player.call_count, 2)
        fake_http_client.set_broadcast.assert_not_called()

    def test_message_team_rejects_invalid_recipient(self) -> None:
        service = VipService(self.config)

        with self.assertRaises(VipHTTPError):
            service.message_team("spectators", "Hello", "Moderator")

    def test_switch_player_to_opposite_team_axis_to_allies(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_players.return_value = [
            {"player_id": "steam123", "team": "Axis"},
        ]
        fake_http_client.get_gamestate.return_value = {
            "num_axis_players": 49,
            "num_allied_players": 48,
        }
        fake_http_client.switch_player_now.return_value = {"result": True}
        service._http_client = fake_http_client  # type: ignore[attr-defined]

        result = service.switch_player_to_opposite_team("steam123", "GBONE")

        self.assertTrue(result.switched)
        self.assertEqual(result.current_team, "axis")
        self.assertEqual(result.target_team, "allies")
        fake_http_client.switch_player_now.assert_called_once_with("steam123")

    def test_switch_player_to_opposite_team_rejects_missing_player(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_players.return_value = []
        service._http_client = fake_http_client  # type: ignore[attr-defined]

        with self.assertRaises(VipHTTPError):
            service.switch_player_to_opposite_team("steam123", "GBONE")

    def test_switch_player_to_opposite_team_rejects_full_team(self) -> None:
        service = VipService(self.config)
        fake_http_client = mock.Mock()
        fake_http_client.get_players.return_value = [
            {"player_id": "steam123", "team": "Allies"},
        ]
        fake_http_client.get_gamestate.return_value = {
            "num_axis_players": 50,
            "num_allied_players": 47,
        }
        service._http_client = fake_http_client  # type: ignore[attr-defined]

        with self.assertRaises(VipHTTPError):
            service.switch_player_to_opposite_team("steam123", "GBONE")


class RollingWindowLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_try_consume_blocks_after_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_path = pathlib.Path(tmpdir) / "quick_limit.json"
            limiter = RollingWindowLimiter(
                window=timedelta(hours=24),
                default_limit=2,
                storage_path=storage_path,
            )

            first = await limiter.try_consume(42)
            second = await limiter.try_consume(42)
            blocked = await limiter.try_consume(42)

            self.assertTrue(first.allowed)
            self.assertTrue(second.allowed)
            self.assertFalse(blocked.allowed)
            self.assertEqual(blocked.used, 2)
            self.assertEqual(blocked.limit, 2)
            self.assertIsNotNone(blocked.next_available_at)

    async def test_try_consume_prunes_old_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_path = pathlib.Path(tmpdir) / "quick_limit.json"
            old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
            state = {
                "limit": 1,
                "usage": {
                    "42": [old_time],
                },
            }
            storage_path.write_text(json.dumps(state), encoding="utf-8")

            limiter = RollingWindowLimiter(
                window=timedelta(hours=24),
                default_limit=1,
                storage_path=storage_path,
            )
            result = await limiter.try_consume(42)

            self.assertTrue(result.allowed)
            self.assertEqual(result.used, 1)
            self.assertEqual(result.limit, 1)

    async def test_get_usage_reports_limit_without_consuming(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_path = pathlib.Path(tmpdir) / "quick_limit.json"
            limiter = RollingWindowLimiter(
                window=timedelta(hours=24),
                default_limit=1,
                storage_path=storage_path,
            )

            usage_before = await limiter.get_usage(42)
            usage_after_consume = await limiter.try_consume(42)
            usage_after = await limiter.get_usage(42)

            self.assertTrue(usage_before.allowed)
            self.assertEqual(usage_before.used, 0)
            self.assertTrue(usage_after_consume.allowed)
            self.assertFalse(usage_after.allowed)
            self.assertEqual(usage_after.used, 1)
            self.assertEqual(usage_after.limit, 1)
            self.assertIsNotNone(usage_after.next_available_at)


class BotCommandRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_tempdir)
        self.base_config = AppConfig(
            discord_token="token",
            vip_duration_hours=4,
            channel_id=1,
            timezone=pytz.UTC,
            timezone_name="UTC",
            state_directory=pathlib.Path(self.tempdir.name),
            http_credentials=HttpCredentials(
                base_url="https://example",
                bearer_token="abc123",
            ),
            moderator_role_id=555,
            vip_assign_limit=2,
            quick_vip_role_ids=(99,),
        )

    async def _cleanup_tempdir(self) -> None:
        self.tempdir.cleanup()

    async def _build_bot(self, *, vip_temp_role_id=None) -> FrontlinePassBot:
        config = AppConfig(
            discord_token=self.base_config.discord_token,
            vip_duration_hours=self.base_config.vip_duration_hours,
            channel_id=self.base_config.channel_id,
            timezone=self.base_config.timezone,
            timezone_name=self.base_config.timezone_name,
            state_directory=self.base_config.state_directory,
            http_credentials=self.base_config.http_credentials,
            vip_assign_limit=self.base_config.vip_assign_limit,
            moderator_role_id=self.base_config.moderator_role_id,
            vip_temp_role_id=vip_temp_role_id,
            quick_vip_role_ids=self.base_config.quick_vip_role_ids,
        )
        bot = FrontlinePassBot(config, mock.Mock())
        bot.vip_assign_limiter = frontline_pass.VipAssignLimiter(
            config.timezone,
            default_limit=config.vip_assign_limit,
            storage_path=pathlib.Path(self.tempdir.name) / "vip_assign_usage.json",
        )
        bot.quick_vip_giver_limiter = RollingWindowLimiter(
            window=timedelta(hours=QUICK_VIP_GIVER_LIMIT_WINDOW_HOURS),
            default_limit=1,
            storage_path=pathlib.Path(self.tempdir.name) / "quick_vip_role_usage.json",
        )
        await bot._register_commands()
        self.addAsyncCleanup(bot.close)
        return bot

    async def test_show_player_vip_uses_to_thread(self) -> None:
        bot = await self._build_bot()
        bot.vip_service.get_player_vip_status = mock.Mock(  # type: ignore[assignment]
            return_value=PlayerVipStatus(
                player_id="player-id",
                expiration_utc=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
        )
        interaction = DummyInteraction(user=DummyMember(1, "Admin", administrator=True))
        command = bot.tree.get_command("show_player_vip")
        self.assertIsNotNone(command)

        with (
            mock.patch.object(frontline_pass, "schedule_ephemeral_cleanup"),
            mock.patch.object(frontline_pass.asyncio, "to_thread", new=mock.AsyncMock()) as to_thread_mock,
        ):
            to_thread_mock.return_value = PlayerVipStatus(
                player_id="player-id",
                expiration_utc=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
            await command.callback(interaction, "player-id")  # type: ignore[union-attr]

        to_thread_mock.assert_awaited_once()
        args = to_thread_mock.await_args.args
        self.assertEqual(args[0], bot.vip_service.get_player_vip_status)
        self.assertEqual(args[1], "player-id")

    async def test_assignvip_does_not_consume_limit_when_role_config_missing(self) -> None:
        bot = await self._build_bot(vip_temp_role_id=None)
        moderator = DummyMember(7, "Moderator", administrator=True)
        member = DummyMember(8, "Target")
        interaction = DummyInteraction(user=moderator, guild=DummyGuild())
        command = bot.tree.get_command("assignvip")
        self.assertIsNotNone(command)

        with mock.patch.object(frontline_pass, "schedule_ephemeral_cleanup"):
            await command.callback(interaction, member)  # type: ignore[union-attr]

        used, limit = await bot.vip_assign_limiter.get_usage(moderator.id)
        self.assertEqual(used, 0)
        self.assertEqual(limit, 2)
        self.assertEqual(len(member.added_roles), 0)

    async def test_quick_vip_failure_does_not_consume_usage(self) -> None:
        bot = await self._build_bot()
        quick_vip_user = DummyMember(
            9,
            "QuickVIP",
            roles=[DummyRole(99, "Quick VIP")],
        )
        interaction = DummyInteraction(user=quick_vip_user, guild=DummyGuild())
        vip_service = mock.Mock()
        vip_service.grant_fixed_vip.side_effect = VipHTTPError("CRCON offline")
        view = QuickVipView(bot, bot.config, vip_service)

        with mock.patch.object(frontline_pass, "schedule_ephemeral_cleanup"):
            await view.handle_modal_submission(interaction, "2805d5bbe14b6ec432f82e5cb859d012")

        usage = await bot.quick_vip_giver_limiter.get_usage(quick_vip_user.id)
        self.assertTrue(usage.allowed)
        self.assertEqual(usage.used, 0)

    async def test_quick_vip_rejects_user_without_nominated_role(self) -> None:
        bot = await self._build_bot()
        interaction = DummyInteraction(user=DummyMember(10, "NotAllowed"), guild=DummyGuild())
        vip_service = mock.Mock()
        view = QuickVipView(bot, bot.config, vip_service)

        with mock.patch.object(frontline_pass, "schedule_ephemeral_cleanup"):
            await view.handle_modal_submission(interaction, "2805d5bbe14b6ec432f82e5cb859d012")

        self.assertEqual(len(interaction.response.messages), 1)
        self.assertIn("approved Quick VIP role", interaction.response.messages[0][0])
        self.assertFalse(vip_service.grant_fixed_vip.called)

    async def test_quick_vip_blocks_second_use_within_48_hours(self) -> None:
        bot = await self._build_bot()
        quick_vip_user = DummyMember(
            11,
            "QuickVIP",
            roles=[DummyRole(99, "Quick VIP")],
        )
        interaction = DummyInteraction(user=quick_vip_user, guild=DummyGuild())
        vip_service = mock.Mock()
        vip_service.grant_fixed_vip.return_value = mock.Mock(
            expiration_local=datetime(2030, 1, 1, tzinfo=timezone.utc),
            expiration_utc=datetime(2030, 1, 1, tzinfo=timezone.utc),
            status_lines=["VIP set"],
        )
        view = QuickVipView(bot, bot.config, vip_service)

        with (
            mock.patch.object(frontline_pass, "schedule_ephemeral_cleanup"),
            mock.patch.object(bot, "refresh_quick_vip_announcement_message", new=mock.AsyncMock()),
            mock.patch.object(bot, "record_vip_grant"),
            mock.patch.object(frontline_pass.asyncio, "to_thread", new=mock.AsyncMock(side_effect=vip_service.grant_fixed_vip)),
        ):
            await view.handle_modal_submission(interaction, "2805d5bbe14b6ec432f82e5cb859d012")

        second_interaction = DummyInteraction(user=quick_vip_user, guild=DummyGuild())
        with mock.patch.object(frontline_pass, "schedule_ephemeral_cleanup"):
            await view.handle_modal_submission(second_interaction, "2805d5bbe14b6ec432f82e5cb859d012")

        self.assertEqual(len(second_interaction.response.messages), 1)
        self.assertIn("48 hours", second_interaction.response.messages[0][0])

    async def test_legacy_quick_vip_role_keeps_five_per_24_hours(self) -> None:
        bot = await self._build_bot()
        legacy_role_name = next(iter(LEGACY_QUICK_VIP_GIVER_ROLE_NAMES))
        quick_vip_user = DummyMember(
            12,
            "LegacyQuickVIP",
            roles=[DummyRole(100, legacy_role_name)],
        )
        vip_service = mock.Mock()
        vip_service.grant_fixed_vip.return_value = mock.Mock(
            expiration_local=datetime(2030, 1, 1, tzinfo=timezone.utc),
            expiration_utc=datetime(2030, 1, 1, tzinfo=timezone.utc),
            status_lines=["VIP set"],
        )
        view = QuickVipView(bot, bot.config, vip_service)

        with (
            mock.patch.object(frontline_pass, "schedule_ephemeral_cleanup"),
            mock.patch.object(bot, "refresh_quick_vip_announcement_message", new=mock.AsyncMock()),
            mock.patch.object(bot, "record_vip_grant"),
            mock.patch.object(frontline_pass.asyncio, "to_thread", new=mock.AsyncMock(side_effect=vip_service.grant_fixed_vip)),
        ):
            for _ in range(LEGACY_QUICK_VIP_GIVER_LIMIT_PER_WINDOW):
                interaction = DummyInteraction(user=quick_vip_user, guild=DummyGuild())
                await view.handle_modal_submission(interaction, "2805d5bbe14b6ec432f82e5cb859d012")

        blocked_interaction = DummyInteraction(user=quick_vip_user, guild=DummyGuild())
        with mock.patch.object(frontline_pass, "schedule_ephemeral_cleanup"):
            await view.handle_modal_submission(blocked_interaction, "2805d5bbe14b6ec432f82e5cb859d012")

        self.assertEqual(len(blocked_interaction.response.messages), 1)
        self.assertIn("5 grants per 24 hours", blocked_interaction.response.messages[0][0])

    async def test_moderator_role_has_unlimited_quick_vip(self) -> None:
        bot = await self._build_bot()
        moderator_user = DummyMember(
            13,
            "ModeratorQuickVIP",
            roles=[DummyRole(555, "Moderator")],
        )

        eligibility = bot.get_quick_vip_eligibility(moderator_user)

        self.assertTrue(eligibility.allowed)
        self.assertTrue(eligibility.unlimited)
        self.assertIsNone(eligibility.limiter)
        self.assertEqual(eligibility.policy_name, "moderator role")

    async def test_moderator_role_quick_vip_does_not_consume_limiter_usage(self) -> None:
        bot = await self._build_bot()
        moderator_user = DummyMember(
            14,
            "ModeratorQuickVIP",
            roles=[DummyRole(555, "Moderator")],
        )
        interaction = DummyInteraction(user=moderator_user, guild=DummyGuild())
        vip_service = mock.Mock()
        vip_service.grant_fixed_vip.return_value = mock.Mock(
            expiration_local=datetime(2030, 1, 1, tzinfo=timezone.utc),
            expiration_utc=datetime(2030, 1, 1, tzinfo=timezone.utc),
            status_lines=["VIP set"],
        )
        view = QuickVipView(bot, bot.config, vip_service)

        with (
            mock.patch.object(frontline_pass, "schedule_ephemeral_cleanup"),
            mock.patch.object(bot, "refresh_quick_vip_announcement_message", new=mock.AsyncMock()),
            mock.patch.object(bot, "record_vip_grant"),
            mock.patch.object(frontline_pass.asyncio, "to_thread", new=mock.AsyncMock(side_effect=vip_service.grant_fixed_vip)),
        ):
            await view.handle_modal_submission(interaction, "2805d5bbe14b6ec432f82e5cb859d012")

        nominated_usage = await bot.quick_vip_giver_limiter.get_usage(moderator_user.id)
        legacy_usage = await bot.legacy_quick_vip_giver_limiter.get_usage(moderator_user.id)
        self.assertEqual(nominated_usage.used, 0)
        self.assertEqual(legacy_usage.used, 0)

    async def test_switch_me_rejects_invalid_player_id(self) -> None:
        bot = await self._build_bot()
        interaction = DummyInteraction(user=DummyMember(15, "Switcher"), guild=DummyGuild())
        vip_service = mock.Mock()
        view = SwitchMeView(bot, bot.config, vip_service)

        with mock.patch.object(frontline_pass, "schedule_ephemeral_cleanup"):
            await view.handle_modal_submission(interaction, "short-id")

        self.assertEqual(len(interaction.response.messages), 1)
        self.assertIn("32-character string", interaction.response.messages[0][0])
        self.assertFalse(vip_service.switch_player_to_opposite_team.called)

    async def test_switch_me_returns_success_message(self) -> None:
        bot = await self._build_bot()
        interaction = DummyInteraction(user=DummyMember(16, "Switcher"), guild=DummyGuild())
        vip_service = mock.Mock()
        vip_service.switch_player_to_opposite_team.return_value = TeamSwitchResult(
            player_id="2805d5bbe14b6ec432f82e5cb859d012",
            current_team="axis",
            target_team="allies",
            switched=True,
            detail="True",
            status_lines=[
                "Requested by Switcher",
                "Current team: Axis",
                "Target team: Allies",
                "HTTP API: True",
            ],
        )
        view = SwitchMeView(bot, bot.config, vip_service)

        with (
            mock.patch.object(frontline_pass, "schedule_ephemeral_cleanup"),
            mock.patch.object(frontline_pass.asyncio, "to_thread", new=mock.AsyncMock(side_effect=vip_service.switch_player_to_opposite_team)),
        ):
            await view.handle_modal_submission(interaction, "2805d5bbe14b6ec432f82e5cb859d012")

        self.assertEqual(len(interaction.followup.messages), 1)
        self.assertIn("Team switch requested successfully.", interaction.followup.messages[0][0])


class LoadConfigTests(unittest.TestCase):
    def test_load_config_uses_app_directory_when_no_state_dir_is_configured(self) -> None:
        original_file = frontline_pass.__file__
        temp_root = pathlib.Path(tempfile.mkdtemp())
        fake_module_path = temp_root / "frontline-pass.py"
        fake_module_path.write_text("# test module marker\n", encoding="utf-8")

        required_env = {
            "DISCORD_TOKEN": "token",
            "CHANNEL_ID": "123",
            "VIP_DURATION_HOURS": "72",
            "LOCAL_TIMEZONE": "Australia/Sydney",
            "CRCON_HTTP_BASE_URL": "https://example.com",
            "CRCON_HTTP_BEARER_TOKEN": "bearer-token",
        }
        removed_env = {
            "FRONTLINE_STATE_DIR": os.environ.get("FRONTLINE_STATE_DIR"),
            "FRONTLINE_CONFIG_PATH": os.environ.get("FRONTLINE_CONFIG_PATH"),
        }

        try:
            frontline_pass.__file__ = str(fake_module_path)
            for key, value in required_env.items():
                os.environ[key] = value
            for key in removed_env:
                os.environ.pop(key, None)

            config = frontline_pass.load_config()

            self.assertEqual(config.state_directory, temp_root)
        finally:
            frontline_pass.__file__ = original_file
            for key in required_env:
                os.environ.pop(key, None)
            for key, value in removed_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
