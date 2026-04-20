from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import discord
import json5
import pytz
import requests
from discord import ButtonStyle, app_commands

try:
    from discord.abc import MessageableChannel
except ImportError:  # discord.py>=2.4 renamed MessageableChannel -> Messageable
    from discord.abc import Messageable as MessageableChannel

from discord.ext import commands
from discord.ui import Button, Modal, TextInput, View
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)

ANNOUNCEMENT_TITLE = "VIP Control Center"
QUICK_VIP_ANNOUNCEMENT_TITLE = "Quick VIP Control Center"
SWITCH_ME_ANNOUNCEMENT_TITLE = "Switch Me Control Center"
QUICK_VIP_DURATION_MINUTES = 10
DEFAULT_QUICK_VIP_ROLE_IDS = (
    1322175167685988403,  # MSU
    1440534025054720020,  # Randoms
)
LEGACY_QUICK_VIP_GIVER_ROLE_NAMES = {
    "MSU-Quick-VIP-Giver",
    "ROFS-Quick-VIP-Giver",
    "SCH-Quick-VIP-Giver",
    "6th-Quick-VIP-Giver",
    "TFMC-Quick-VIP-Giver",
    "LGN-Quick-VIP-Giver",
}
LEGACY_QUICK_VIP_GIVER_LIMIT_PER_WINDOW = 5
LEGACY_QUICK_VIP_GIVER_LIMIT_WINDOW_HOURS = 24
QUICK_VIP_GIVER_LIMIT_PER_WINDOW = 1
QUICK_VIP_GIVER_LIMIT_WINDOW_HOURS = 48
PLAYER_ID_PLACEHOLDER = (
    "Go to https://hllrecords.com/, get your player_id (e.g. 2805d5bbe14b6ec432f82e5cb859d012)."
)


def schedule_ephemeral_cleanup(
    interaction: discord.Interaction,
    *,
    delay: float = 10.0,
    message: Optional[discord.Message] = None,
) -> None:
    async def _cleanup() -> None:
        await asyncio.sleep(delay)
        with contextlib.suppress(discord.HTTPException, discord.NotFound):
            if message is None:
                await interaction.delete_original_response()
            else:
                await message.delete()

    asyncio.create_task(_cleanup())


def build_announcement_embed(
    config: AppConfig,
    vip_duration_hours: float,
    _last_grant_at: Optional[datetime],
) -> discord.Embed:
    description_lines = [
        "Use the button below to activate your VIP access.",
        'When registering you need to paste your player_id string, for example "2805d5bbe14b6ec432f82e5cb859d012", from https://hllrecords.com.',
        f"VIP duration: **{vip_duration_hours:g} hours**.",
    ]
    embed = discord.Embed(
        title=ANNOUNCEMENT_TITLE,
        description="\n".join(description_lines),
        color=0x2F3136,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Local Timezone", value=config.timezone_name, inline=True)
    embed.set_footer(text="Buttons stay active across restarts.")
    return embed


def build_quick_vip_announcement_embed(
    config: AppConfig,
    _last_grant_at: Optional[datetime],
) -> discord.Embed:
    description_lines = [
        "Use the button below to grant a fixed 10-minute VIP window.",
        "Eligible users need either a legacy clan Quick VIP role, the configured moderator role, or a nominated Quick VIP role.",
        "Paste the target player's player_id from https://hllrecords.com when prompted.",
        "This does not extend existing VIP. It sets the target to 10 minutes from now.",
    ]
    embed = discord.Embed(
        title=QUICK_VIP_ANNOUNCEMENT_TITLE,
        description="\n".join(description_lines),
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Duration", value=f"{QUICK_VIP_DURATION_MINUTES} minutes", inline=True)
    embed.add_field(
        name="Nominated Roles",
        value=f"{QUICK_VIP_GIVER_LIMIT_PER_WINDOW} use per {QUICK_VIP_GIVER_LIMIT_WINDOW_HOURS} hours",
        inline=True,
    )
    embed.add_field(
        name="Legacy Clan / Moderator Role",
        value="Legacy clan roles: 5 uses per 24 hours\nModerator role: unlimited",
        inline=True,
    )
    embed.add_field(name="Local Timezone", value=config.timezone_name, inline=True)
    embed.set_footer(text="Eligibility is controlled by legacy clan roles, the moderator role, or nominated Discord roles.")
    return embed


def build_switch_me_announcement_embed(
    config: AppConfig,
    _last_switch_at: Optional[datetime],
) -> discord.Embed:
    description_lines = [
        "Use the button below to request a move to the opposite team.",
        'When registering you need to paste your player_id string, for example "2805d5bbe14b6ec432f82e5cb859d012", from https://hllrecords.com.',
        "The switch only succeeds if you are currently in-game and the opposite team has space available.",
    ]
    embed = discord.Embed(
        title=SWITCH_ME_ANNOUNCEMENT_TITLE,
        description="\n".join(description_lines),
        color=0xF1C40F,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Local Timezone", value=config.timezone_name, inline=True)
    embed.set_footer(text="Buttons stay active across restarts.")
    return embed


class AnnouncementManager:
    def __init__(
        self,
        config: AppConfig,
        *,
        channel_id: int,
        announcement_message_id: Optional[int],
        title: str,
    ) -> None:
        self._config = config
        self._channel_id = channel_id
        self._announcement_message_id = announcement_message_id
        self._title = title
        self._message_id: Optional[int] = None

    async def ensure(
        self,
        bot: commands.Bot,
        view: View,
        embed: discord.Embed,
        *,
        force_new: bool = False,
    ) -> Optional[discord.Message]:
        destination = await self._resolve_destination(bot)
        if destination is None:
            return None

        if force_new:
            await self._delete_existing(destination, bot)
            message = None
        else:
            message = await self._locate_message(destination, bot)

        if message:
            await message.edit(embed=embed, view=view)
            self._message_id = message.id
            logging.info("Reattached control view to existing message %s", message.id)
            return message

        sent_message = await destination.send(embed=embed, view=view)
        self._message_id = sent_message.id
        logging.info(
            "Posted announcement message with id %s. Set for future updates within this session.",
            sent_message.id,
        )
        return sent_message

    async def _resolve_destination(self, bot: commands.Bot) -> Optional[MessageableChannel]:
        destination = bot.get_channel(self._channel_id)
        if destination is None:
            try:
                destination = await bot.fetch_channel(self._channel_id)
            except discord.DiscordException:
                logging.exception("Failed to access channel with id %s", self._channel_id)
                return None

        if not isinstance(destination, (discord.TextChannel, discord.Thread, discord.DMChannel)):
            logging.error("Channel %s is not a text-based destination.", self._channel_id)
            return None

        return destination

    async def _locate_message(
        self,
        destination: MessageableChannel,
        bot: commands.Bot,
    ) -> Optional[discord.Message]:
        for message_id in self._candidate_message_ids():
            try:
                message = await destination.fetch_message(message_id)
                return message
            except discord.NotFound:
                continue
            except discord.DiscordException:
                logging.exception("Failed to fetch announcement message %s", message_id)

        async for message in destination.history(limit=50):
            if message.author == bot.user and message.embeds:
                if message.embeds[0].title == self._title:
                    self._message_id = message.id
                    return message
        return None

    async def _delete_existing(self, destination: MessageableChannel, bot: commands.Bot) -> None:
        for message_id in self._candidate_message_ids():
            try:
                message = await destination.fetch_message(message_id)
            except discord.NotFound:
                continue
            except discord.DiscordException:
                logging.exception("Failed to fetch announcement message %s for deletion", message_id)
                continue
            await self._delete_message(message)

        async for message in destination.history(limit=50):
            if message.author == bot.user and message.embeds:
                if message.embeds[0].title == self._title:
                    await self._delete_message(message)

        self._message_id = None

    async def _delete_message(self, message: discord.Message) -> None:
        try:
            await message.delete()
        except discord.DiscordException:
            logging.exception("Failed to delete announcement message %s", message.id)

    def _candidate_message_ids(self) -> List[int]:
        candidate_ids: List[int] = []
        if self._message_id:
            candidate_ids.append(self._message_id)
        if self._announcement_message_id:
            candidate_ids.append(self._announcement_message_id)
        return candidate_ids


def _parse_bool_env(name: str, raw: Optional[str], errors: List[str]) -> Optional[bool]:
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    errors.append(f"{name} must be a boolean (true/false); got {raw!r}")
    return None


def _load_raw_config() -> Tuple[Dict[str, Any], Optional[Path]]:
    candidate_paths: List[Path] = []
    env_path = os.getenv("FRONTLINE_CONFIG_PATH")
    if env_path:
        candidate_paths.append(Path(env_path))
    base_dir = Path(__file__).resolve().parent
    for name in ("config.jsonc", "config.json"):
        candidate_paths.append(base_dir / name)
        candidate_paths.append(Path.cwd() / name)

    for path in candidate_paths:
        if not path:
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json5.load(handle)
        except FileNotFoundError:
            continue
        except Exception:
            logging.exception("Failed to load configuration from %s", path)
            continue
        if isinstance(data, dict):
            logging.info("Loaded configuration from %s", path)
            return data, path
    logging.warning("No configuration file found; relying on environment variables.")
    return {}, None


@dataclass(frozen=True)
class HttpCredentials:
    base_url: str
    bearer_token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    verify: bool = True
    timeout: float = 20.0


@dataclass(frozen=True)
class AppConfig:
    discord_token: str
    vip_duration_hours: float
    channel_id: int
    timezone: pytz.BaseTzInfo
    timezone_name: str
    state_directory: Path
    announcement_message_id: Optional[int] = None
    quick_vip_channel_id: Optional[int] = None
    quick_vip_announcement_message_id: Optional[int] = None
    switch_me_channel_id: Optional[int] = None
    switch_me_announcement_message_id: Optional[int] = None
    quick_vip_role_ids: Tuple[int, ...] = DEFAULT_QUICK_VIP_ROLE_IDS
    http_credentials: Optional[HttpCredentials] = None
    moderator_role_id: Optional[int] = None
    vip_temp_role_id: Optional[int] = None
    vip_claim_channel_id: Optional[int] = None
    vip_assign_limit: int = 5

    @property
    def vip_duration_label(self) -> str:
        return f"{self.vip_duration_hours:g}"


@dataclass(frozen=True)
class VipAssignUsageResult:
    allowed: bool
    used: int
    limit: int


@dataclass(frozen=True)
class RollingWindowUsageResult:
    allowed: bool
    used: int
    limit: int
    next_available_at: Optional[datetime] = None


@dataclass(frozen=True)
class QuickVipEligibility:
    allowed: bool
    limiter: Optional["RollingWindowLimiter"] = None
    policy_name: str = ""
    limit: int = 0
    window_hours: int = 0
    unlimited: bool = False


class RollingWindowLimiter:
    def __init__(self, *, window: timedelta, default_limit: int, storage_path: Path) -> None:
        self._window = window
        self._storage_path = storage_path
        self._lock = asyncio.Lock()
        self._state: Dict[str, Any] = {
            "limit": max(int(default_limit), 1),
            "usage": {},
        }
        self._load_state()

    async def try_consume(self, user_id: int) -> RollingWindowUsageResult:
        async with self._lock:
            now = datetime.now(timezone.utc)
            changed = self._prune(now)
            limit = max(int(self._state.get("limit", 1)), 1)
            usage_map = self._state.setdefault("usage", {})
            key = str(user_id)
            entries = usage_map.setdefault(key, [])
            if not isinstance(entries, list):
                entries = []
                usage_map[key] = entries
                changed = True

            used = len(entries)
            if used >= limit:
                oldest = self._parse_datetime(entries[0]) if entries else None
                next_available = oldest + self._window if oldest else None
                if changed:
                    self._save_state()
                return RollingWindowUsageResult(False, used, limit, next_available)

            entries.append(now.isoformat())
            self._save_state()
            return RollingWindowUsageResult(True, used + 1, limit)

    async def get_usage(self, user_id: int) -> RollingWindowUsageResult:
        async with self._lock:
            now = datetime.now(timezone.utc)
            changed = self._prune(now)
            limit = max(int(self._state.get("limit", 1)), 1)
            usage_map = self._state.setdefault("usage", {})
            key = str(user_id)
            entries = usage_map.setdefault(key, [])
            if not isinstance(entries, list):
                entries = []
                usage_map[key] = entries
                changed = True

            used = len(entries)
            next_available = None
            if used >= limit and entries:
                oldest = self._parse_datetime(entries[0])
                next_available = oldest + self._window if oldest else None

            if changed:
                self._save_state()

            return RollingWindowUsageResult(used < limit, used, limit, next_available)

    def _load_state(self) -> None:
        try:
            with self._storage_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except Exception:
            logging.exception("Failed to load rolling limiter state from %s", self._storage_path)
            return

        limit = data.get("limit")
        if not isinstance(limit, int) or limit <= 0:
            limit = self._state["limit"]
        usage_raw = data.get("usage") or {}
        usage: Dict[str, List[str]] = {}
        if isinstance(usage_raw, dict):
            for key, timestamps in usage_raw.items():
                if not isinstance(timestamps, list):
                    continue
                cleaned: List[str] = []
                for value in timestamps:
                    if isinstance(value, str) and self._parse_datetime(value):
                        cleaned.append(value)
                if cleaned:
                    usage[str(key)] = cleaned

        self._state = {
            "limit": limit,
            "usage": usage,
        }

    def _save_state(self) -> None:
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            with self._storage_path.open("w", encoding="utf-8") as handle:
                json.dump(self._state, handle)
        except Exception:
            logging.exception("Failed to persist rolling limiter state to %s", self._storage_path)

    def _prune(self, now: datetime) -> bool:
        cutoff = now - self._window
        usage_map = self._state.get("usage", {})
        if not isinstance(usage_map, dict):
            self._state["usage"] = {}
            return True
        changed = False
        for key in list(usage_map.keys()):
            timestamps = usage_map.get(key, [])
            if not isinstance(timestamps, list):
                usage_map.pop(key, None)
                changed = True
                continue
            filtered: List[str] = []
            for value in timestamps:
                parsed = self._parse_datetime(value)
                if not parsed:
                    changed = True
                    continue
                if parsed > cutoff:
                    filtered.append(value)
                else:
                    changed = True
            if filtered:
                if len(filtered) != len(timestamps):
                    usage_map[key] = filtered
            else:
                usage_map.pop(key, None)
                changed = True
        return changed

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed


class VipAssignLimiter:
    def __init__(self, timezone: pytz.BaseTzInfo, *, default_limit: int, storage_path: Path) -> None:
        self._timezone = timezone
        self._storage_path = storage_path
        self._lock = asyncio.Lock()
        self._state: Dict[str, Any] = {
            "limit": max(int(default_limit), 1),
            "usage": {},
            "window_start": None,
        }
        self._load_state()

    async def get_limit(self) -> int:
        async with self._lock:
            return max(int(self._state.get("limit", 1)), 1)

    async def set_limit(self, new_limit: int) -> int:
        normalized = max(int(new_limit), 1)
        async with self._lock:
            self._state["limit"] = normalized
            self._save_state()
            return normalized

    async def try_consume(self, user_id: int) -> VipAssignUsageResult:
        async with self._lock:
            now = datetime.now(timezone.utc)
            if self._ensure_current_window(now):
                self._save_state()
            limit = max(int(self._state.get("limit", 1)), 1)
            usage_map = self._state.setdefault("usage", {})
            key = str(user_id)
            current = int(usage_map.get(key, 0))
            if current >= limit:
                return VipAssignUsageResult(False, current, limit)
            usage_map[key] = current + 1
            self._save_state()
            return VipAssignUsageResult(True, current + 1, limit)

    async def get_usage(self, user_id: int) -> Tuple[int, int]:
        async with self._lock:
            now = datetime.now(timezone.utc)
            if self._ensure_current_window(now):
                self._save_state()
            limit = max(int(self._state.get("limit", 1)), 1)
            current = int(self._state.get("usage", {}).get(str(user_id), 0))
            return current, limit

    def _load_state(self) -> None:
        try:
            with self._storage_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except Exception:
            logging.exception("Failed to load VIP assign limiter state from %s", self._storage_path)
            return

        limit = data.get("limit")
        if not isinstance(limit, int) or limit <= 0:
            limit = self._state["limit"]
        usage_raw = data.get("usage") or {}
        usage: Dict[str, int] = {}
        if isinstance(usage_raw, dict):
            for key, value in usage_raw.items():
                try:
                    usage[str(key)] = max(int(value), 0)
                except (TypeError, ValueError):
                    continue
        window_start = data.get("window_start")
        if not isinstance(window_start, str):
            window_start = None

        self._state = {
            "limit": limit,
            "usage": usage,
            "window_start": window_start,
        }

    def _save_state(self) -> None:
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            with self._storage_path.open("w", encoding="utf-8") as handle:
                json.dump(self._state, handle)
        except Exception:
            logging.exception("Failed to persist VIP assign limiter state to %s", self._storage_path)

    def _ensure_current_window(self, now: datetime) -> bool:
        current_start = self._current_window_start(now)
        stored = self._parse_datetime(self._state.get("window_start"))
        if not stored or stored.astimezone(self._timezone) != current_start:
            self._state["window_start"] = current_start.isoformat()
            self._state["usage"] = {}
            return True
        return False

    def _current_window_start(self, now: datetime) -> datetime:
        localized = now.astimezone(self._timezone)
        start_of_week = localized - timedelta(days=localized.weekday())
        start_of_week = start_of_week.replace(hour=1, minute=0, second=0, microsecond=0)
        if localized < start_of_week:
            start_of_week -= timedelta(days=7)
        return start_of_week

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed


def load_config() -> AppConfig:
    load_dotenv()
    app_directory = Path(__file__).resolve().parent
    raw_config, _ = _load_raw_config()
    config_values = {str(key).upper(): value for key, value in raw_config.items()}
    errors: List[str] = []

    config_aliases: Dict[str, Tuple[str, ...]] = {
        "CRCON_HTTP_BASE_URL": ("API_BASE_URL",),
        "CRCON_HTTP_BEARER_TOKEN": ("API_BEARER_TOKEN",),
        "CRCON_HTTP_USERNAME": ("API_USERNAME",),
        "CRCON_HTTP_PASSWORD": ("API_PASSWORD",),
        "CRCON_HTTP_VERIFY": ("API_VERIFY",),
        "CRCON_HTTP_TIMEOUT": ("API_TIMEOUT",),
    }

    def config_lookup(name: str) -> Any:
        upper = name.upper()
        if upper in config_values and config_values[upper] not in (None, ""):
            return config_values[upper]
        for alias in config_aliases.get(upper, ()):
            alias_upper = alias.upper()
            if alias_upper in config_values and config_values[alias_upper] not in (None, ""):
                return config_values[alias_upper]
        return None

    def get_value(name: str, default: Any = None) -> Any:
        env_value = os.getenv(name)
        if env_value is not None and env_value.strip() != "":
            return env_value.strip()
        lookup = config_lookup(name)
        if lookup is not None:
            return lookup
        return default

    def require_str(name: str) -> str:
        value = get_value(name)
        if value is None or str(value).strip() == "":
            errors.append(f"{name} is required")
            return ""
        return str(value).strip()

    def require_float(name: str) -> Optional[float]:
        value = get_value(name)
        if value is None or str(value).strip() == "":
            errors.append(f"{name} is required")
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            errors.append(f"{name} must be a number (got {value!r})")
            return None

    def require_int(name: str) -> Optional[int]:
        value = get_value(name)
        if value is None or str(value).strip() == "":
            errors.append(f"{name} is required")
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            errors.append(f"{name} must be an integer (got {value!r})")
            return None

    def optional_int(name: str) -> Optional[int]:
        value = get_value(name)
        if value is None or str(value).strip() == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            errors.append(f"{name} must be an integer (got {value!r})")
            return None

    def optional_int_list(name: str) -> Tuple[int, ...]:
        value = get_value(name)
        if value is None:
            return ()

        items: List[Any]
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return ()
            items = [part.strip() for part in stripped.split(",")]
        elif isinstance(value, (list, tuple, set)):
            items = list(value)
        else:
            errors.append(f"{name} must be a comma-separated string or list of integers")
            return ()

        parsed_values: List[int] = []
        for item in items:
            if item is None or str(item).strip() == "":
                continue
            try:
                parsed_values.append(int(str(item).strip()))
            except (TypeError, ValueError):
                errors.append(f"{name} contains a non-integer value ({item!r})")
                return ()
        return tuple(parsed_values)

    def optional_float(name: str, default: Optional[float] = None) -> Optional[float]:
        value = get_value(name)
        if value is None or str(value).strip() == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            errors.append(f"{name} must be a number (got {value!r})")
            return default

    def optional_bool(name: str, default: Optional[bool] = None) -> Optional[bool]:
        value = get_value(name)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        parsed = _parse_bool_env(name, str(value), errors)
        if parsed is not None:
            return parsed
        return default

    discord_token = require_str("DISCORD_TOKEN")
    vip_duration_hours = require_float("VIP_DURATION_HOURS")
    channel_id = require_int("CHANNEL_ID")
    timezone_name = require_str("LOCAL_TIMEZONE")
    state_directory_raw = get_value("FRONTLINE_STATE_DIR")

    if vip_duration_hours is not None and vip_duration_hours <= 0:
        errors.append("VIP_DURATION_HOURS must be greater than zero")

    try:
        timezone = pytz.timezone(timezone_name)
    except pytz.UnknownTimeZoneError:
        errors.append(f"LOCAL_TIMEZONE must be a valid IANA timezone (got {timezone_name!r})")
        timezone = pytz.UTC

    if state_directory_raw is None or str(state_directory_raw).strip() == "":
        state_directory = app_directory
    else:
        state_directory = Path(str(state_directory_raw)).expanduser()
        if not state_directory.is_absolute():
            state_directory = (app_directory / state_directory).resolve()
    try:
        state_directory.mkdir(parents=True, exist_ok=True)
        probe_path = state_directory / ".frontline-state-write-test"
        probe_path.write_text("ok", encoding="utf-8")
        probe_path.unlink()
    except Exception as exc:
        errors.append(f"FRONTLINE_STATE_DIR must be writable (resolved to {state_directory}): {exc}")
    announcement_message_id = optional_int("ANNOUNCEMENT_MESSAGE_ID")
    quick_vip_channel_id = optional_int("QUICK_VIP_CHANNEL_ID")
    quick_vip_announcement_message_id = optional_int("QUICK_VIP_ANNOUNCEMENT_MESSAGE_ID")
    switch_me_channel_id = optional_int("SWITCH_ME_CHANNEL_ID")
    switch_me_announcement_message_id = optional_int("SWITCH_ME_ANNOUNCEMENT_MESSAGE_ID")
    quick_vip_role_ids = optional_int_list("QUICK_VIP_ROLE_IDS")
    moderator_role_id = optional_int("MODERATOR_ROLE_ID")
    vip_temp_role_id = optional_int("VIP_TEMP_ROLE_ID")
    vip_claim_channel_id = optional_int("VIP_CLAIM_CHANNEL_ID")
    vip_assign_limit_raw = optional_int("VIP_ASSIGN_LIMIT")
    if vip_assign_limit_raw is None:
        vip_assign_limit = 5
    else:
        vip_assign_limit = vip_assign_limit_raw
    if vip_assign_limit <= 0:
        errors.append("VIP_ASSIGN_LIMIT must be greater than zero")
    http_base_url_raw = get_value("CRCON_HTTP_BASE_URL")
    http_bearer_token = get_value("CRCON_HTTP_BEARER_TOKEN")
    http_username = get_value("CRCON_HTTP_USERNAME")
    http_password = get_value("CRCON_HTTP_PASSWORD")
    http_verify = optional_bool("CRCON_HTTP_VERIFY", default=True)
    http_timeout = optional_float("CRCON_HTTP_TIMEOUT", default=20.0) or 20.0

    http_credentials: Optional[HttpCredentials] = None
    trimmed_base = str(http_base_url_raw).strip() if http_base_url_raw else ""
    trimmed_token = str(http_bearer_token).strip() if http_bearer_token else ""
    trimmed_username = str(http_username).strip() if http_username else ""
    trimmed_password = str(http_password).strip() if http_password else ""

    if any((trimmed_base, trimmed_token, trimmed_username, trimmed_password)):
        normalized_base = trimmed_base.rstrip("/")
        if not normalized_base:
            errors.append("CRCON_HTTP_BASE_URL is required when using the HTTP API integration")
        else:
            lowered_base = normalized_base.lower()
            if lowered_base.endswith("/api") or "/api/" in lowered_base:
                errors.append(
                    "CRCON_HTTP_BASE_URL should not include '/api'. Provide the host only (e.g. https://example.com:8010)."
                )
        if not trimmed_token and not (trimmed_username and trimmed_password):
            errors.append(
                "Provide either CRCON_HTTP_BEARER_TOKEN or both CRCON_HTTP_USERNAME and CRCON_HTTP_PASSWORD when enabling the HTTP API integration"
            )
        if trimmed_username and not trimmed_password:
            errors.append("CRCON_HTTP_PASSWORD is required when CRCON_HTTP_USERNAME is provided")
        if normalized_base and (trimmed_token or (trimmed_username and trimmed_password)):
            http_credentials = HttpCredentials(
                base_url=normalized_base,
                bearer_token=trimmed_token or None,
                username=trimmed_username or None,
                password=trimmed_password or None,
                verify=http_verify if http_verify is not None else True,
                timeout=http_timeout,
            )

    if http_credentials is None:
        errors.append("CRCON_HTTP_BASE_URL and authentication details are required to grant VIP via HTTP API")

    if errors:
        raise RuntimeError("Configuration error(s): " + "; ".join(errors))

    assert vip_duration_hours is not None
    assert channel_id is not None
    return AppConfig(
        discord_token=discord_token,
        vip_duration_hours=vip_duration_hours,
        channel_id=channel_id,
        timezone=timezone,
        timezone_name=timezone_name,
        state_directory=state_directory,
        announcement_message_id=announcement_message_id,
        quick_vip_channel_id=quick_vip_channel_id,
        quick_vip_announcement_message_id=quick_vip_announcement_message_id,
        switch_me_channel_id=switch_me_channel_id,
        switch_me_announcement_message_id=switch_me_announcement_message_id,
        quick_vip_role_ids=quick_vip_role_ids or DEFAULT_QUICK_VIP_ROLE_IDS,
        http_credentials=http_credentials,
        moderator_role_id=moderator_role_id,
        vip_temp_role_id=vip_temp_role_id,
        vip_claim_channel_id=vip_claim_channel_id,
        vip_assign_limit=vip_assign_limit,
    )


class VipHTTPError(Exception):
    """Raised when the HTTP API integration fails."""


class VipHttpClient:
    def __init__(
        self,
        credentials: HttpCredentials,
        timeout: Optional[float] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.credentials = credentials
        if timeout is None:
            timeout = credentials.timeout
        if timeout is None:
            timeout = 10.0
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.verify = credentials.verify
        self._token: Optional[str] = None
        self._authenticated = False
        self._bearer_failed = False

    def _endpoint(self, name: str) -> str:
        base = self.credentials.base_url.rstrip("/")
        if not base.lower().endswith("/api"):
            base = f"{base}/api"
        return f"{base}/{name.lstrip('/')}"

    def _headers(self, *, include_auth: bool = True) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        token: Optional[str] = None
        if include_auth:
            token = self._authorization_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        if include_auth and not token:
            headers["Referer"] = self.credentials.base_url
            csrf_token = self.session.cookies.get("csrftoken")
            if csrf_token:
                headers["X-CSRFToken"] = csrf_token
        return headers

    def _authorization_token(self) -> Optional[str]:
        if self.credentials.bearer_token and not self._bearer_failed:
            return self.credentials.bearer_token
        return self._token

    def _has_login_credentials(self) -> bool:
        return bool(self.credentials.username and self.credentials.password)

    def _ensure_authenticated(self) -> None:
        if self._authorization_token():
            return
        if self._authenticated:
            return
        if not self._has_login_credentials():
            return
        self._login()

    def _login(self) -> None:
        if not self._has_login_credentials():
            raise VipHTTPError("CRCON HTTP username/password are not configured.")

        response = self.session.post(
            self._endpoint("login"),
            json={
                "username": self.credentials.username,
                "password": self.credentials.password,
            },
            headers=self._headers(include_auth=False),
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise VipHTTPError(f"Login failed with status {response.status_code}: {response.text}")

        data = self._parse_json(response)
        if data.get("failed"):
            raise VipHTTPError(f"Login failed: {data.get('error') or data}")

        token = self._extract_token(data)
        self._token = token
        has_session_cookie = bool(self.session.cookies.get("sessionid"))
        if not has_session_cookie and not token:
            raise VipHTTPError("Login succeeded but no session cookie or auth token was provided.")
        self._authenticated = True

    def _refresh_token_if_possible(self) -> bool:
        if not self._has_login_credentials():
            return False
        self._token = None
        self._authenticated = False
        try:
            self._login()
        except VipHTTPError:
            return False
        return True

    def _request_with_reauth(
        self,
        method: str,
        endpoint: str,
        *,
        json_payload: Optional[Dict[str, Any]] = None,
        query_params: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        url = self._endpoint(endpoint)
        self._ensure_authenticated()
        headers = self._headers()
        request_kwargs: Dict[str, Any] = {
            "headers": headers,
            "timeout": self.timeout,
        }
        if json_payload is not None:
            request_kwargs["json"] = json_payload
        if query_params is not None:
            request_kwargs["params"] = query_params
        response = self.session.request(method, url, **request_kwargs)
        if response.status_code == 401:
            if self.credentials.bearer_token:
                self._bearer_failed = True
            logging.warning(
                "HTTP %s %s returned 401: %s",
                method,
                endpoint,
                response.text,
            )
            if self._refresh_token_if_possible():
                headers = self._headers()
                request_kwargs = {
                    "headers": headers,
                    "timeout": self.timeout,
                }
                if json_payload is not None:
                    request_kwargs["json"] = json_payload
                if query_params is not None:
                    request_kwargs["params"] = query_params
                response = self.session.request(method, url, **request_kwargs)
        return response

    @staticmethod
    def _extract_token(payload: Any) -> Optional[str]:
        if isinstance(payload, str):
            return payload or None
        if not isinstance(payload, dict):
            return None

        for key in ("token", "jwt", "access_token", "accessToken"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value

        for key in ("result", "data"):
            nested = payload.get(key)
            token = VipHttpClient._extract_token(nested)
            if token:
                return token
        return None

    def add_vip(
        self,
        player_id: str,
        description: str,
        expiration_iso: Optional[str],
        *,
        player_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "player_id": player_id,
            "description": description,
        }
        if expiration_iso:
            payload["expiration"] = expiration_iso
        if player_name:
            payload["player_name"] = player_name

        try:
            response = self._request_with_reauth("POST", "add_vip", json_payload=payload)
        except requests.exceptions.RequestException as exc:
            raise VipHTTPError(f"HTTP API request failed: {exc}") from exc

        if response.status_code != 200:
            raise VipHTTPError(f"add_vip failed with status {response.status_code}: {response.text}")

        data = self._parse_json(response)
        if data.get("failed"):
            raise VipHTTPError(f"add_vip reported failure: {data.get('error') or data}")
        return data

    def get_player_profile(
        self,
        player_id: str,
        *,
        num_sessions: int = 10,
    ) -> Dict[str, Any]:
        query_params = {
            "player_id": player_id,
            "num_sessions": num_sessions,
        }
        try:
            response = self._request_with_reauth(
                "GET",
                "get_player_profile",
                query_params=query_params,
            )
        except requests.exceptions.RequestException as exc:
            raise VipHTTPError(f"HTTP API request failed: {exc}") from exc

        if response.status_code != 200:
            raise VipHTTPError(
                f"get_player_profile failed with status {response.status_code}: {response.text}"
            )

        data = self._parse_json(response)
        if data.get("failed"):
            raise VipHTTPError(f"get_player_profile reported failure: {data.get('error') or data}")
        result = data.get("result") or {}
        if not isinstance(result, dict):
            raise VipHTTPError("get_player_profile returned an unexpected result format.")
        return result

    def get_players(self) -> List[Dict[str, Any]]:
        try:
            response = self._request_with_reauth("GET", "get_players")
        except requests.exceptions.RequestException as exc:
            raise VipHTTPError(f"HTTP API request failed: {exc}") from exc

        if response.status_code != 200:
            raise VipHTTPError(f"get_players failed with status {response.status_code}: {response.text}")

        data = self._parse_json(response)
        if data.get("failed"):
            raise VipHTTPError(f"get_players reported failure: {data.get('error') or data}")
        result = data.get("result")
        if result is None:
            return []
        if not isinstance(result, list):
            raise VipHTTPError("get_players returned an unexpected result format.")
        players: List[Dict[str, Any]] = []
        for player in result:
            if isinstance(player, dict):
                players.append(player)
        return players

    def get_detailed_players(self) -> List[Dict[str, Any]]:
        try:
            response = self._request_with_reauth("GET", "get_detailed_players")
        except requests.exceptions.RequestException as exc:
            raise VipHTTPError(f"HTTP API request failed: {exc}") from exc

        if response.status_code != 200:
            raise VipHTTPError(
                f"get_detailed_players failed with status {response.status_code}: {response.text}"
            )

        data = self._parse_json(response)
        if data.get("failed"):
            raise VipHTTPError(f"get_detailed_players reported failure: {data.get('error') or data}")

        result = data.get("result") or {}
        if not isinstance(result, dict):
            raise VipHTTPError("get_detailed_players returned an unexpected result format.")

        players_raw = result.get("players") or {}
        if not isinstance(players_raw, dict):
            raise VipHTTPError("get_detailed_players returned an unexpected players format.")

        players: List[Dict[str, Any]] = []
        for player in players_raw.values():
            if isinstance(player, dict):
                players.append(player)
        return players

    def get_gamestate(self) -> Dict[str, Any]:
        try:
            response = self._request_with_reauth("GET", "get_gamestate")
        except requests.exceptions.RequestException as exc:
            raise VipHTTPError(f"HTTP API request failed: {exc}") from exc

        if response.status_code != 200:
            raise VipHTTPError(f"get_gamestate failed with status {response.status_code}: {response.text}")

        data = self._parse_json(response)
        if data.get("failed"):
            raise VipHTTPError(f"get_gamestate reported failure: {data.get('error') or data}")
        result = data.get("result")
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise VipHTTPError("get_gamestate returned an unexpected result format.")
        return result

    def switch_player_now(self, player_id: str) -> Dict[str, Any]:
        payload = {"player_id": player_id}

        try:
            response = self._request_with_reauth("POST", "switch_player_now", json_payload=payload)
        except requests.exceptions.RequestException as exc:
            raise VipHTTPError(f"HTTP API request failed: {exc}") from exc

        if response.status_code != 200:
            raise VipHTTPError(
                f"switch_player_now failed with status {response.status_code}: {response.text}"
            )

        data = self._parse_json(response)
        if data.get("failed"):
            raise VipHTTPError(f"switch_player_now reported failure: {data.get('error') or data}")
        return data

    def message_player(
        self,
        player_id: str,
        message: str,
        by: str,
        *,
        save_message: bool = False,
        player_name: Optional[str] = None,
    ) -> bool:
        payload: Dict[str, Any] = {
            "player_id": player_id,
            "message": message,
            "by": by,
            "save_message": save_message,
        }
        if player_name:
            payload["player_name"] = player_name

        try:
            response = self._request_with_reauth("POST", "message_player", json_payload=payload)
        except requests.exceptions.RequestException as exc:
            raise VipHTTPError(f"HTTP API request failed: {exc}") from exc

        if response.status_code != 200:
            raise VipHTTPError(f"message_player failed with status {response.status_code}: {response.text}")

        data = self._parse_json(response)
        if data.get("failed"):
            raise VipHTTPError(f"message_player reported failure: {data.get('error') or data}")
        result = data.get("result")
        return bool(result)

    def set_broadcast(self, message: str) -> Dict[str, str]:
        payload = {"message": message}
        try:
            response = self._request_with_reauth("POST", "set_broadcast", json_payload=payload)
        except requests.exceptions.RequestException as exc:
            raise VipHTTPError(f"HTTP API request failed: {exc}") from exc

        if response.status_code != 200:
            raise VipHTTPError(f"set_broadcast failed with status {response.status_code}: {response.text}")

        data = self._parse_json(response)
        if data.get("failed"):
            raise VipHTTPError(f"set_broadcast reported failure: {data.get('error') or data}")

        previous = data.get("result")
        previous_text = ""
        if previous is not None:
            previous_text = str(previous)

        current_text = message
        try:
            current_response = self._request_with_reauth("GET", "get_broadcast_message")
            if current_response.status_code == 200:
                current_data = self._parse_json(current_response)
                if not current_data.get("failed"):
                    current_result = current_data.get("result")
                    if isinstance(current_result, str) and current_result.strip():
                        current_text = current_result.strip()
        except (VipHTTPError, requests.exceptions.RequestException):
            logging.warning("Unable to read current broadcast after set_broadcast.")

        return {
            "previous": previous_text,
            "current": current_text,
        }

    @staticmethod
    def _parse_json(response: requests.Response) -> Dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise VipHTTPError(f"Failed to parse JSON response: {response.text}") from exc
        if isinstance(data, dict):
            return data
        raise VipHTTPError("Unexpected response format; expected JSON object.")


@dataclass
class VipGrantResult:
    status_lines: List[str]
    detail: str
    expiration_local: datetime
    expiration_utc: datetime


@dataclass(frozen=True)
class TeamMessageDispatchResult:
    recipient: str
    attempted: int
    sent: int
    failed: int


@dataclass(frozen=True)
class TeamSwitchResult:
    player_id: str
    current_team: str
    target_team: str
    switched: bool
    detail: str
    status_lines: List[str]


@dataclass(frozen=True)
class PlayerVipStatus:
    player_id: str
    expiration_utc: Optional[datetime]

    def is_active(self, reference: datetime) -> bool:
        return bool(self.expiration_utc and self.expiration_utc > reference)


class VipService:
    def __init__(self, config: AppConfig) -> None:
        if not config.http_credentials:
            raise RuntimeError("HTTP credentials are required for VIP service.")
        self._http_client = VipHttpClient(config.http_credentials)

    def grant_vip(
        self,
        player_id: str,
        duration_hours: float,
        local_timezone: pytz.BaseTzInfo,
        requester_display_name: str,
        *,
        player_name: Optional[str] = None,
    ) -> VipGrantResult:
        expiration_utc = self._determine_extended_expiration(player_id, duration_hours)
        expiration_local = expiration_utc.astimezone(local_timezone)
        expiration_iso = expiration_utc.isoformat()
        comment = (
            f"Discord VIP for {requester_display_name} until {expiration_utc:%Y-%m-%d %H:%M:%S} UTC"
        )
        response = self._http_client.add_vip(
            player_id,
            comment,
            expiration_iso,
            player_name=player_name,
        )
        message: Any = response.get("result")
        if isinstance(message, dict):
            message = message.get("result") or message
        if message is None:
            message = "HTTP API add_vip succeeded."
        detail = str(message)
        return VipGrantResult(
            status_lines=[f"HTTP API: {detail}"],
            detail=detail,
            expiration_local=expiration_local,
            expiration_utc=expiration_utc,
        )

    def grant_fixed_vip(
        self,
        player_id: str,
        duration_minutes: float,
        local_timezone: pytz.BaseTzInfo,
        requester_display_name: str,
        *,
        player_name: Optional[str] = None,
    ) -> VipGrantResult:
        expiration_utc = self._now_utc() + timedelta(minutes=duration_minutes)
        expiration_local = expiration_utc.astimezone(local_timezone)
        expiration_iso = expiration_utc.isoformat()
        comment = (
            f"Quick VIP from Discord by {requester_display_name} until {expiration_utc:%Y-%m-%d %H:%M:%S} UTC"
        )
        response = self._http_client.add_vip(
            player_id,
            comment,
            expiration_iso,
            player_name=player_name,
        )
        message: Any = response.get("result")
        if isinstance(message, dict):
            message = message.get("result") or message
        if message is None:
            message = "HTTP API add_vip succeeded."
        detail = str(message)
        return VipGrantResult(
            status_lines=[f"HTTP API: {detail}"],
            detail=detail,
            expiration_local=expiration_local,
            expiration_utc=expiration_utc,
        )

    def get_player_vip_status(self, player_id: str) -> PlayerVipStatus:
        profile = self._http_client.get_player_profile(player_id, num_sessions=10)
        expiration_utc = self._extract_latest_vip_expiration(profile)
        return PlayerVipStatus(player_id=player_id, expiration_utc=expiration_utc)

    def message_team(
        self,
        recipient: str,
        message: str,
        requester_display_name: str,
    ) -> TeamMessageDispatchResult:
        recipient_key = recipient.strip().lower()
        if recipient_key not in {"axis", "allies", "both"}:
            raise VipHTTPError("Recipient must be one of: axis, allies, both.")

        text = message.strip()
        if not text:
            raise VipHTTPError("Message cannot be empty.")

        players = self._http_client.get_players()
        targets = self._filter_players_by_team(players, recipient_key)
        attempted = len(targets)
        sent = 0
        failed = 0

        for player in targets:
            player_id = self._extract_player_id(player)
            if not player_id:
                failed += 1
                continue

            player_name = self._extract_player_name(player)
            try:
                self._http_client.message_player(
                    player_id=player_id,
                    message=text,
                    by=requester_display_name,
                    save_message=False,
                    player_name=player_name,
                )
                sent += 1
            except VipHTTPError:
                failed += 1

        return TeamMessageDispatchResult(
            recipient=recipient_key,
            attempted=attempted,
            sent=sent,
            failed=failed,
        )

    def switch_player_to_opposite_team(
        self,
        player_id: str,
        requester_display_name: str,
    ) -> TeamSwitchResult:
        players = self._http_client.get_detailed_players()
        player = self._find_player(players, player_id)
        if player is None:
            raise VipHTTPError("Player was not found in the current server player list.")

        current_team = self._extract_team_name(player)
        if current_team not in {"axis", "allies"}:
            raise VipHTTPError("Player is not currently assigned to Axis or Allies.")

        target_team = "allies" if current_team == "axis" else "axis"
        gamestate = self._http_client.get_gamestate()
        target_count = self._get_team_player_count(gamestate, target_team)
        if target_count >= 50:
            raise VipHTTPError(
                f"Cannot switch player because the {target_team.capitalize()} team is full ({target_count}/50)."
            )

        response = self._http_client.switch_player_now(player_id)
        message: Any = response.get("result")
        if isinstance(message, dict):
            message = message.get("result") or message
        if message is None:
            message = "HTTP API switch_player_now succeeded."
        detail = str(message)
        return TeamSwitchResult(
            player_id=player_id,
            current_team=current_team,
            target_team=target_team,
            switched=True,
            detail=detail,
            status_lines=[
                f"Requested by {requester_display_name}",
                f"Current team: {current_team.capitalize()}",
                f"Target team: {target_team.capitalize()}",
                f"HTTP API: {detail}",
            ],
        )

    def _determine_extended_expiration(
        self,
        player_id: str,
        duration_hours: float,
    ) -> datetime:
        profile = self._http_client.get_player_profile(player_id, num_sessions=10)
        latest_expiration = self._extract_latest_vip_expiration(profile)
        base = self._now_utc()
        if latest_expiration and latest_expiration > base:
            base = latest_expiration
        return base + timedelta(hours=duration_hours)

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _extract_latest_vip_expiration(profile: Dict[str, Any]) -> Optional[datetime]:
        latest: Optional[datetime] = None
        for entry in VipService._iter_vip_entries(profile):
            if not isinstance(entry, dict):
                continue
            expiration_str = entry.get("expiration")
            expiration_dt = VipService._parse_iso_datetime(expiration_str)
            if expiration_dt is None:
                continue
            expiration_utc = expiration_dt.astimezone(timezone.utc)
            if latest is None or expiration_utc > latest:
                latest = expiration_utc
        return latest

    @staticmethod
    def _iter_vip_entries(profile: Any) -> List[Dict[str, Any]]:
        if not isinstance(profile, dict):
            return []

        vip_entries: List[Dict[str, Any]] = []
        candidate_keys = (
            "vips",
            "vip",
            "player_vip",
            "active_vip",
            "current_vip",
        )
        nested_container_keys = (
            "player",
            "profile",
            "player_profile",
            "result",
            "data",
        )

        for key in candidate_keys:
            vip_entries.extend(VipService._normalize_vip_entries(profile.get(key)))

        for key in nested_container_keys:
            nested_value = profile.get(key)
            if isinstance(nested_value, dict):
                vip_entries.extend(VipService._iter_vip_entries(nested_value))

        return vip_entries

    @staticmethod
    def _normalize_vip_entries(value: Any) -> List[Dict[str, Any]]:
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict)]
        return []

    @staticmethod
    def _parse_iso_datetime(value: Any) -> Optional[datetime]:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _extract_player_id(player: Any) -> Optional[str]:
        if not isinstance(player, dict):
            return None
        for key in ("player_id", "steam_id_64", "steam_id", "id"):
            value = player.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_player_name(player: Any) -> Optional[str]:
        if not isinstance(player, dict):
            return None
        for key in ("player_name", "name"):
            value = player.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_team_name(player: Any) -> Optional[str]:
        if not isinstance(player, dict):
            return None
        for key in ("team", "team_name", "team_side"):
            value = player.get(key)
            normalized = VipService._normalize_team_value(value)
            if normalized:
                return normalized
        return None

    @staticmethod
    def _normalize_team_value(value: Any) -> Optional[str]:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"axis"}:
                return "axis"
            if lowered in {"allies", "allied"}:
                return "allies"
        return None

    @staticmethod
    def _filter_players_by_team(players: Any, recipient: str) -> List[Dict[str, Any]]:
        if not isinstance(players, list):
            return []
        if recipient == "both":
            return [player for player in players if isinstance(player, dict)]
        filtered: List[Dict[str, Any]] = []
        for player in players:
            team_name = VipService._extract_team_name(player)
            if team_name == recipient and isinstance(player, dict):
                filtered.append(player)
        return filtered

    @staticmethod
    def _find_player(players: Any, player_id: str) -> Optional[Dict[str, Any]]:
        if not isinstance(players, list):
            return None
        normalized_player_id = player_id.strip()
        for player in players:
            if not isinstance(player, dict):
                continue
            candidate_id = VipService._extract_player_id(player)
            if candidate_id == normalized_player_id:
                return player
        return None

    @staticmethod
    def _get_team_player_count(gamestate: Any, team_name: str) -> int:
        if not isinstance(gamestate, dict):
            raise VipHTTPError("get_gamestate returned an unexpected result format.")

        if team_name == "axis":
            candidate_keys = ("num_axis_players", "axis_players", "axis_count")
        elif team_name == "allies":
            candidate_keys = ("num_allied_players", "num_allies_players", "allied_players", "allies_count")
        else:
            raise VipHTTPError(f"Unsupported team name {team_name!r}.")

        for key in candidate_keys:
            value = gamestate.get(key)
            try:
                return int(value)
            except (TypeError, ValueError):
                continue

        raise VipHTTPError(f"Could not determine current player count for the {team_name} team.")


class VipRequestModal(Modal):
    def __init__(self, parent_view: "CombinedView") -> None:
        super().__init__(title="Request VIP Access", custom_id="frontline-pass-vip-modal")
        self._parent_view = parent_view
        self.player_id = TextInput(
            label="HLL player_id",
            placeholder=PLAYER_ID_PLACEHOLDER,
            custom_id="frontline-pass-vip-player-id-input",
            min_length=32,
            max_length=32,
            style=discord.TextStyle.short,
        )
        self.add_item(self.player_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._parent_view.handle_vip_modal_submission(interaction, self.player_id.value)


class PersistentView(View):
    def __init__(self) -> None:
        super().__init__(timeout=None)


class CombinedView(PersistentView):
    def __init__(
        self,
        bot: "FrontlinePassBot",
        config: AppConfig,
        vip_service: VipService,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.config = config
        self.vip_service = vip_service
        self._refresh_button_label()

    @discord.ui.button(label="Get VIP", style=ButtonStyle.green, custom_id="frontline-pass-get-vip")
    async def give_vip_button(self, interaction: discord.Interaction, _: Button) -> None:
        modal = VipRequestModal(self)
        try:
            await interaction.response.send_modal(modal)
        except discord.HTTPException:
            logging.exception("Failed to open VIP request modal for %s", interaction.user.id)
            error_message = "I couldn't open the VIP request form. Please try again shortly."
            if interaction.response.is_done():
                followup = await interaction.followup.send(error_message, ephemeral=True, wait=True)
                schedule_ephemeral_cleanup(interaction, message=followup)
            else:
                await interaction.response.send_message(error_message, ephemeral=True)
                schedule_ephemeral_cleanup(interaction)

    def _refresh_button_label(self) -> None:
        self.give_vip_button.label = f"Get VIP ({self.bot.vip_duration_hours:g} hours)"

    def refresh_vip_label(self) -> None:
        self._refresh_button_label()

    async def handle_vip_modal_submission(self, interaction: discord.Interaction, player_id: str) -> None:
        player_id = player_id.strip()
        if not player_id:
            await interaction.response.send_message("player_id cannot be empty.", ephemeral=True)
            schedule_ephemeral_cleanup(interaction)
            return

        if len(player_id) != 32:
            await interaction.response.send_message(
                "player_id must be a 32-character string copied from https://hllrecords.com.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)
            return

        await interaction.response.defer(ephemeral=True)
        await self._grant_vip_for_player(
            interaction,
            player_id,
            player_display_name=interaction.user.display_name,
        )

    async def _grant_vip_for_player(
        self,
        interaction: discord.Interaction,
        player_id: str,
        *,
        player_display_name: Optional[str] = None,
    ) -> None:
        duration_hours = self.bot.vip_duration_hours

        try:
            result = await asyncio.to_thread(
                self.vip_service.grant_vip,
                player_id,
                duration_hours,
                self.config.timezone,
                interaction.user.display_name,
                player_name=player_display_name,
            )
        except VipHTTPError as exc:
            logging.exception("Failed to grant VIP for player %s", player_id)
            followup_message = await interaction.followup.send(
                f"Error: VIP status could not be set: {exc}",
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)
            return
        except Exception as exc:  # pragma: no cover
            logging.exception("Unexpected error while granting VIP for player %s: %s", player_id, exc)
            followup_message = await interaction.followup.send(
                "An unexpected error occurred while setting VIP status.",
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)
            return

        readable_expiration = result.expiration_local.strftime("%Y-%m-%d %H:%M:%S %Z")
        logging.info(
            "Granted VIP for player %s until %s UTC (%s)",
            player_id,
            result.expiration_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "; ".join(result.status_lines),
        )
        self.bot.record_vip_grant(datetime.now(timezone.utc))
        await self.bot.refresh_announcement_message()

        header_lines = [
            f"You now have VIP for {self.config.vip_duration_label} hours!",
            f"Linked player_id: {player_id}",
            f"Expiration: {readable_expiration}",
        ]
        status_summary = "\n".join(f"- {line}" for line in result.status_lines)
        message_body = "\n".join(header_lines) + "\n\n**Status**:\n" + status_summary
        followup_message = await interaction.followup.send(
            message_body,
            ephemeral=True,
            wait=True,
        )
        schedule_ephemeral_cleanup(interaction, message=followup_message)
        await self._maybe_remove_temp_vip_role(interaction)

    async def _maybe_remove_temp_vip_role(self, interaction: discord.Interaction) -> None:
        role_id = getattr(self.config, "vip_temp_role_id", None)
        if not role_id:
            return
        guild = interaction.guild
        if guild is None:
            return
        user = interaction.user
        if not isinstance(user, discord.Member):
            try:
                user = await guild.fetch_member(interaction.user.id)
            except discord.DiscordException:
                return
        role = guild.get_role(role_id)
        if role is None:
            return
        if role in getattr(user, "roles", []):
            try:
                await user.remove_roles(role, reason="Frontline Pass: remove temporary VIP role after claim")
                logging.info("Removed temporary VIP role %s from %s after claim", role_id, user.id)
            except discord.DiscordException:
                logging.exception("Failed to remove temporary VIP role %s from %s", role_id, user.id)


class QuickVipRequestModal(Modal):
    def __init__(self, parent_view: "QuickVipView") -> None:
        super().__init__(title="Grant Quick VIP", custom_id="frontline-pass-quick-vip-modal")
        self._parent_view = parent_view
        self.player_id = TextInput(
            label="Target player_id",
            placeholder=PLAYER_ID_PLACEHOLDER,
            custom_id="frontline-pass-quick-vip-player-id-input",
            min_length=32,
            max_length=32,
            style=discord.TextStyle.short,
        )
        self.add_item(self.player_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._parent_view.handle_modal_submission(interaction, self.player_id.value)


class QuickVipView(PersistentView):
    def __init__(
        self,
        bot: "FrontlinePassBot",
        config: AppConfig,
        vip_service: VipService,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.config = config
        self.vip_service = vip_service

    @discord.ui.button(
        label="Quick VIP (10 min)",
        style=ButtonStyle.blurple,
        custom_id="frontline-pass-quick-vip",
    )
    async def quick_vip_button(self, interaction: discord.Interaction, _: Button) -> None:
        modal = QuickVipRequestModal(self)
        try:
            await interaction.response.send_modal(modal)
        except discord.HTTPException:
            logging.exception("Failed to open Quick VIP request modal for %s", interaction.user.id)
            error_message = "I couldn't open the Quick VIP form. Please try again shortly."
            if interaction.response.is_done():
                followup = await interaction.followup.send(error_message, ephemeral=True, wait=True)
                schedule_ephemeral_cleanup(interaction, message=followup)
            else:
                await interaction.response.send_message(error_message, ephemeral=True)
                schedule_ephemeral_cleanup(interaction)

    async def handle_modal_submission(self, interaction: discord.Interaction, player_id: str) -> None:
        player_id = player_id.strip()
        if not player_id:
            await interaction.response.send_message("Player-ID cannot be empty.", ephemeral=True)
            schedule_ephemeral_cleanup(interaction)
            return

        if len(player_id) != 32:
            await interaction.response.send_message(
                "Player-ID must be a 32-character string copied from https://hllrecords.com.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)
            return

        member = interaction.user if hasattr(interaction.user, "roles") else None
        if member is None:
            await interaction.response.send_message(
                "Quick VIP can only be used by a server member with an approved Quick VIP role.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)
            return
        eligibility = self.bot.get_quick_vip_eligibility(member)
        if not eligibility.allowed:
            await interaction.response.send_message(
                "You do not have an approved Quick VIP role, so this control is unavailable to you.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)
            return

        if eligibility.unlimited:
            usage = None
        elif eligibility.limiter is not None:
            usage = await eligibility.limiter.get_usage(member.id)
        else:
            await interaction.response.send_message(
                "Quick VIP is misconfigured for your role policy. Ask an admin to check the bot configuration.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)
            return

        if usage is not None and not usage.allowed:
            next_at = usage.next_available_at
            if next_at is not None:
                next_at_unix = int(next_at.timestamp())
                message = (
                    f"Quick VIP limit reached: {eligibility.limit} "
                    f"{'grant' if eligibility.limit == 1 else 'grants'} per {eligibility.window_hours} "
                    f"hours for {eligibility.policy_name}.\n"
                    f"Try again <t:{next_at_unix}:R>."
                )
            else:
                message = (
                    f"Quick VIP limit reached: {eligibility.limit} "
                    f"{'grant' if eligibility.limit == 1 else 'grants'} per {eligibility.window_hours} "
                    f"hours for {eligibility.policy_name}."
                )
            await interaction.response.send_message(message, ephemeral=True)
            schedule_ephemeral_cleanup(interaction)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(
                self.vip_service.grant_fixed_vip,
                player_id,
                QUICK_VIP_DURATION_MINUTES,
                self.config.timezone,
                interaction.user.display_name,
            )
        except VipHTTPError as exc:
            logging.exception("Failed to grant Quick VIP for player %s", player_id)
            followup_message = await interaction.followup.send(
                f"Error: Quick VIP could not be set: {exc}",
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)
            return
        except Exception as exc:  # pragma: no cover
            logging.exception("Unexpected error while granting Quick VIP for player %s: %s", player_id, exc)
            followup_message = await interaction.followup.send(
                "An unexpected error occurred while setting Quick VIP.",
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)
            return

        readable_expiration = result.expiration_local.strftime("%Y-%m-%d %H:%M:%S %Z")
        logging.info(
            "Granted Quick VIP for player %s until %s UTC (%s)",
            player_id,
            result.expiration_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "; ".join(result.status_lines),
        )
        if not eligibility.unlimited and eligibility.limiter is not None:
            usage = await eligibility.limiter.try_consume(member.id)
            if not usage.allowed:
                logging.warning(
                    "Quick VIP grant for %s succeeded but usage recording was rejected due to a concurrent limit check for user %s under policy %s.",
                    player_id,
                    member.id,
                    eligibility.policy_name,
                )
        self.bot.record_vip_grant(datetime.now(timezone.utc))
        await self.bot.refresh_quick_vip_announcement_message()

        status_summary = "\n".join(f"- {line}" for line in result.status_lines)
        message_body = (
            f"Quick VIP granted for **{QUICK_VIP_DURATION_MINUTES} minutes**.\n"
            f"Target ID: {player_id}\n"
            f"Expiration: {readable_expiration}\n\n"
            f"**Status**:\n{status_summary}"
        )
        followup_message = await interaction.followup.send(
            message_body,
            ephemeral=True,
            wait=True,
        )
        schedule_ephemeral_cleanup(interaction, message=followup_message)


class SwitchMeRequestModal(Modal):
    def __init__(self, parent_view: "SwitchMeView") -> None:
        super().__init__(title="Request Team Switch", custom_id="frontline-pass-switch-me-modal")
        self._parent_view = parent_view
        self.player_id = TextInput(
            label="HLL player_id",
            placeholder=PLAYER_ID_PLACEHOLDER,
            custom_id="frontline-pass-switch-me-player-id-input",
            min_length=32,
            max_length=32,
            style=discord.TextStyle.short,
        )
        self.add_item(self.player_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._parent_view.handle_modal_submission(interaction, self.player_id.value)


class SwitchMeView(PersistentView):
    def __init__(
        self,
        bot: "FrontlinePassBot",
        config: AppConfig,
        vip_service: VipService,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.config = config
        self.vip_service = vip_service

    @discord.ui.button(
        label="Switch Me",
        style=ButtonStyle.secondary,
        custom_id="frontline-pass-switch-me",
    )
    async def switch_me_button(self, interaction: discord.Interaction, _: Button) -> None:
        modal = SwitchMeRequestModal(self)
        try:
            await interaction.response.send_modal(modal)
        except discord.HTTPException:
            logging.exception("Failed to open Switch Me modal for %s", interaction.user.id)
            error_message = "I couldn't open the Switch Me form. Please try again shortly."
            if interaction.response.is_done():
                followup = await interaction.followup.send(error_message, ephemeral=True, wait=True)
                schedule_ephemeral_cleanup(interaction, message=followup)
            else:
                await interaction.response.send_message(error_message, ephemeral=True)
                schedule_ephemeral_cleanup(interaction)

    async def handle_modal_submission(self, interaction: discord.Interaction, player_id: str) -> None:
        player_id = player_id.strip()
        if not player_id:
            await interaction.response.send_message("player_id cannot be empty.", ephemeral=True)
            schedule_ephemeral_cleanup(interaction)
            return

        if len(player_id) != 32:
            await interaction.response.send_message(
                "player_id must be a 32-character string copied from https://hllrecords.com.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(
                self.vip_service.switch_player_to_opposite_team,
                player_id,
                interaction.user.display_name,
            )
        except VipHTTPError as exc:
            logging.exception("Failed to switch player %s to the opposite team", player_id)
            followup_message = await interaction.followup.send(
                f"Error: team switch could not be completed: {exc}",
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)
            return
        except Exception as exc:  # pragma: no cover
            logging.exception("Unexpected error while switching player %s: %s", player_id, exc)
            followup_message = await interaction.followup.send(
                "An unexpected error occurred while switching teams.",
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)
            return

        logging.info(
            "Switched player %s from %s to %s (%s)",
            result.player_id,
            result.current_team,
            result.target_team,
            "; ".join(result.status_lines),
        )
        status_summary = "\n".join(f"- {line}" for line in result.status_lines)
        message_body = (
            "Team switch requested successfully.\n"
            f"Linked player_id: {result.player_id}\n"
            f"From: {result.current_team.capitalize()}\n"
            f"To: {result.target_team.capitalize()}\n\n"
            f"**Status**:\n{status_summary}"
        )
        followup_message = await interaction.followup.send(
            message_body,
            ephemeral=True,
            wait=True,
        )
        schedule_ephemeral_cleanup(interaction, message=followup_message)


class FrontlinePassBot(commands.Bot):
    def __init__(self, config: AppConfig, vip_service: VipService) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.vip_service = vip_service
        self.announcement_manager = AnnouncementManager(
            config,
            channel_id=config.channel_id,
            announcement_message_id=config.announcement_message_id,
            title=ANNOUNCEMENT_TITLE,
        )
        self.quick_vip_announcement_manager: Optional[AnnouncementManager] = None
        self.switch_me_announcement_manager: Optional[AnnouncementManager] = None
        if config.quick_vip_channel_id:
            self.quick_vip_announcement_manager = AnnouncementManager(
                config,
                channel_id=config.quick_vip_channel_id,
                announcement_message_id=config.quick_vip_announcement_message_id,
                title=QUICK_VIP_ANNOUNCEMENT_TITLE,
            )
        if config.switch_me_channel_id:
            self.switch_me_announcement_manager = AnnouncementManager(
                config,
                channel_id=config.switch_me_channel_id,
                announcement_message_id=config.switch_me_announcement_message_id,
                title=SWITCH_ME_ANNOUNCEMENT_TITLE,
            )
        self.persistent_view: Optional[CombinedView] = None
        self.quick_vip_view: Optional[QuickVipView] = None
        self.switch_me_view: Optional[SwitchMeView] = None
        self._vip_duration_hours = config.vip_duration_hours
        self._last_grant_utc: Optional[datetime] = None
        limiter_state_path = config.state_directory / "vip_assign_usage.json"
        self.vip_assign_limiter = VipAssignLimiter(
            config.timezone,
            default_limit=config.vip_assign_limit,
            storage_path=limiter_state_path,
        )
        quick_vip_limiter_path = config.state_directory / "quick_vip_role_usage.json"
        self.quick_vip_giver_limiter = RollingWindowLimiter(
            window=timedelta(hours=QUICK_VIP_GIVER_LIMIT_WINDOW_HOURS),
            default_limit=QUICK_VIP_GIVER_LIMIT_PER_WINDOW,
            storage_path=quick_vip_limiter_path,
        )
        legacy_quick_vip_limiter_path = config.state_directory / "quick_vip_legacy_role_usage.json"
        self.legacy_quick_vip_giver_limiter = RollingWindowLimiter(
            window=timedelta(hours=LEGACY_QUICK_VIP_GIVER_LIMIT_WINDOW_HOURS),
            default_limit=LEGACY_QUICK_VIP_GIVER_LIMIT_PER_WINDOW,
            storage_path=legacy_quick_vip_limiter_path,
        )

    @property
    def vip_duration_hours(self) -> float:
        return self._vip_duration_hours

    @property
    def last_grant_time(self) -> Optional[datetime]:
        return self._last_grant_utc

    def record_vip_grant(self, when: datetime) -> None:
        self._last_grant_utc = when

    async def setup_hook(self) -> None:
        self.persistent_view = CombinedView(self, self.config, self.vip_service)
        self.add_view(self.persistent_view)
        if self.quick_vip_announcement_manager:
            self.quick_vip_view = QuickVipView(self, self.config, self.vip_service)
            self.add_view(self.quick_vip_view)
        if self.switch_me_announcement_manager:
            self.switch_me_view = SwitchMeView(self, self.config, self.vip_service)
            self.add_view(self.switch_me_view)
        await self._register_commands()
        guild_ids_raw = os.getenv("COMMAND_GUILD_IDS") or os.getenv("COMMAND_GUILD_ID")
        synced_any_guild = False
        if guild_ids_raw:
            try:
                guild_ids = [int(x.strip()) for x in guild_ids_raw.split(",") if x.strip()]
            except ValueError:
                logging.warning("Invalid COMMAND_GUILD_IDS value %r; falling back to global sync.", guild_ids_raw)
                guild_ids = []
            for gid in guild_ids:
                try:
                    guild_obj = discord.Object(id=gid)
                    self.tree.copy_global_to(guild=guild_obj)
                    await self.tree.sync(guild=guild_obj)
                    synced_any_guild = True
                    logging.info("Slash commands synced to guild %s", gid)
                except discord.DiscordException:
                    logging.exception("Failed to sync slash commands to guild %s", gid)
        if not synced_any_guild:
            await self.tree.sync()
            logging.info("Slash commands globally synced (may take up to 1 hour to appear).")
        try:
            cmd_names = ", ".join(sorted(cmd.name for cmd in self.tree.get_commands()))
            logging.info("Registered slash commands: %s", cmd_names)
        except Exception:
            logging.exception("Unable to list registered slash commands")

    async def on_ready(self) -> None:
        logging.info("Bot is ready: %s", self.user)
        http_base = self.config.http_credentials.base_url if self.config.http_credentials else "unset"
        logging.info("HTTP API base=%s; current VIP duration=%.2f hours", http_base, self.vip_duration_hours)
        await self.refresh_announcement_message()
        await self.refresh_quick_vip_announcement_message()
        await self.refresh_switch_me_announcement_message()

    async def refresh_announcement_message(self) -> None:
        if not self.persistent_view:
            logging.error("Persistent view not initialised; cannot refresh announcement message.")
            return
        await self.announcement_manager.ensure(
            self,
            self.persistent_view,
            build_announcement_embed(self.config, self.vip_duration_hours, self.last_grant_time),
        )

    async def refresh_quick_vip_announcement_message(self) -> None:
        if not self.quick_vip_announcement_manager or not self.quick_vip_view:
            return
        await self.quick_vip_announcement_manager.ensure(
            self,
            self.quick_vip_view,
            build_quick_vip_announcement_embed(self.config, self.last_grant_time),
        )

    async def refresh_switch_me_announcement_message(self) -> None:
        if not self.switch_me_announcement_manager or not self.switch_me_view:
            return
        await self.switch_me_announcement_manager.ensure(
            self,
            self.switch_me_view,
            build_switch_me_announcement_embed(self.config, self.last_grant_time),
        )

    def _user_has_moderator_privileges(self, user: discord.abc.User) -> bool:
        permissions = getattr(user, "guild_permissions", None)  # type: ignore[attr-defined]
        if permissions and permissions.administrator:
            return True
        role_id = self.config.moderator_role_id
        if role_id and hasattr(user, "roles"):
            for role in getattr(user, "roles", []):  # type: ignore[assignment]
                if getattr(role, "id", None) == role_id:
                    return True
        return False

    @staticmethod
    def user_has_any_role_id(user: discord.abc.User, role_ids: Tuple[int, ...]) -> bool:
        if not hasattr(user, "roles"):
            return False
        targets = {int(role_id) for role_id in role_ids}
        if not targets:
            return False
        for role in getattr(user, "roles", []):
            role_id = getattr(role, "id", None)
            if isinstance(role_id, int) and role_id in targets:
                return True
        return False

    @staticmethod
    def user_has_any_role_named(user: discord.abc.User, role_names: set[str]) -> bool:
        if not hasattr(user, "roles"):
            return False
        targets = {name.strip().lower() for name in role_names if isinstance(name, str)}
        if not targets:
            return False
        for role in getattr(user, "roles", []):
            name = getattr(role, "name", "")
            if isinstance(name, str) and name.strip().lower() in targets:
                return True
        return False

    def user_has_legacy_quick_vip_role(self, user: discord.abc.User) -> bool:
        if self.user_has_any_role_named(user, LEGACY_QUICK_VIP_GIVER_ROLE_NAMES):
            return True
        return False

    def user_has_moderator_role(self, user: discord.abc.User) -> bool:
        moderator_role_id = self.config.moderator_role_id
        if moderator_role_id is None:
            return False
        return self.user_has_any_role_id(user, (moderator_role_id,))

    def get_quick_vip_eligibility(self, user: discord.abc.User) -> QuickVipEligibility:
        if self.user_has_moderator_role(user):
            return QuickVipEligibility(
                allowed=True,
                policy_name="moderator role",
                unlimited=True,
            )
        if self.user_has_legacy_quick_vip_role(user):
            return QuickVipEligibility(
                allowed=True,
                limiter=self.legacy_quick_vip_giver_limiter,
                policy_name="legacy clan Quick VIP roles",
                limit=LEGACY_QUICK_VIP_GIVER_LIMIT_PER_WINDOW,
                window_hours=LEGACY_QUICK_VIP_GIVER_LIMIT_WINDOW_HOURS,
            )
        if self.user_has_any_role_id(user, self.config.quick_vip_role_ids):
            return QuickVipEligibility(
                allowed=True,
                limiter=self.quick_vip_giver_limiter,
                policy_name="nominated Quick VIP roles",
                limit=QUICK_VIP_GIVER_LIMIT_PER_WINDOW,
                window_hours=QUICK_VIP_GIVER_LIMIT_WINDOW_HOURS,
            )
        return QuickVipEligibility(allowed=False)

    async def set_vip_duration_hours(self, hours: float) -> None:
        self._vip_duration_hours = hours
        if self.persistent_view:
            self.persistent_view.refresh_vip_label()
        await self.refresh_announcement_message()

    async def _register_commands(self) -> None:
        @self.tree.command(
            name="repost_frontline_controls",
            description="Repost the Frontline VIP control panel.",
        )
        async def repost_frontline_controls(interaction: discord.Interaction) -> None:
            permissions = getattr(interaction.user, "guild_permissions", None)  # type: ignore[attr-defined]
            if not permissions or not permissions.administrator:
                await interaction.response.send_message(
                    "You need administrator permissions to use this command.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            await interaction.response.defer(ephemeral=True)
            if not self.persistent_view:
                followup_message = await interaction.followup.send(
                    "The persistent view is not initialised yet. Try again shortly.",
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)
                return

            message = await self.announcement_manager.ensure(
                self,
                self.persistent_view,
                build_announcement_embed(self.config, self.vip_duration_hours, self.last_grant_time),
                force_new=True,
            )
            if message:
                followup_message = await interaction.followup.send(
                    f"Frontline VIP controls reposted successfully (message ID {message.id}).",
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)
            else:
                followup_message = await interaction.followup.send(
                    "Unable to repost the VIP controls. Check the bot logs for details.",
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)

        @self.tree.command(
            name="repost_quick_vip_controls",
            description="Repost the Quick VIP control panel.",
        )
        async def repost_quick_vip_controls(interaction: discord.Interaction) -> None:
            permissions = getattr(interaction.user, "guild_permissions", None)  # type: ignore[attr-defined]
            if not permissions or not permissions.administrator:
                await interaction.response.send_message(
                    "You need administrator permissions to use this command.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            if not self.quick_vip_announcement_manager or not self.quick_vip_view:
                await interaction.response.send_message(
                    "Quick VIP controls are not configured. Set QUICK_VIP_CHANNEL_ID first.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            await interaction.response.defer(ephemeral=True)
            message = await self.quick_vip_announcement_manager.ensure(
                self,
                self.quick_vip_view,
                build_quick_vip_announcement_embed(self.config, self.last_grant_time),
                force_new=True,
            )
            if message:
                followup_message = await interaction.followup.send(
                    f"Quick VIP controls reposted successfully (message ID {message.id}).",
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)
            else:
                followup_message = await interaction.followup.send(
                    "Unable to repost the Quick VIP controls. Check the bot logs for details.",
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)

        @self.tree.command(
            name="repost_switch_me_controls",
            description="Repost the Switch Me control panel.",
        )
        async def repost_switch_me_controls(interaction: discord.Interaction) -> None:
            permissions = getattr(interaction.user, "guild_permissions", None)  # type: ignore[attr-defined]
            if not permissions or not permissions.administrator:
                await interaction.response.send_message(
                    "You need administrator permissions to use this command.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            if not self.switch_me_announcement_manager or not self.switch_me_view:
                await interaction.response.send_message(
                    "Switch Me controls are not configured. Set SWITCH_ME_CHANNEL_ID first.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            await interaction.response.defer(ephemeral=True)
            message = await self.switch_me_announcement_manager.ensure(
                self,
                self.switch_me_view,
                build_switch_me_announcement_embed(self.config, self.last_grant_time),
                force_new=True,
            )
            if message:
                followup_message = await interaction.followup.send(
                    f"Switch Me controls reposted successfully (message ID {message.id}).",
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)
            else:
                followup_message = await interaction.followup.send(
                    "Unable to repost the Switch Me controls. Check the bot logs for details.",
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)

        @self.tree.command(
            name="set_vip_duration",
            description="Set the VIP duration in hours.",
        )
        @app_commands.describe(hours="Number of hours that VIP access should last")
        async def set_vip_duration(interaction: discord.Interaction, hours: float) -> None:
            if not self._user_has_moderator_privileges(interaction.user):
                await interaction.response.send_message(
                    "You need moderator permissions to use this command.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            if hours <= 0:
                await interaction.response.send_message(
                    "VIP duration must be greater than zero hours.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            await interaction.response.defer(ephemeral=True)
            await self.set_vip_duration_hours(hours)
            followup_message = await interaction.followup.send(
                f"VIP duration updated to {hours:g} hours.",
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)

        @self.tree.command(
            name="setvipduration",
            description="Set the VIP duration in hours (alias).",
        )
        @app_commands.describe(hours="Number of hours that VIP access should last")
        async def setvipduration(interaction: discord.Interaction, hours: float) -> None:
            if not self._user_has_moderator_privileges(interaction.user):
                await interaction.response.send_message(
                    "You need moderator permissions to use this command.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            if hours <= 0:
                await interaction.response.send_message(
                    "VIP duration must be greater than zero hours.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            await interaction.response.defer(ephemeral=True)
            await self.set_vip_duration_hours(hours)
            followup_message = await interaction.followup.send(
                f"VIP duration updated to {hours:g} hours.",
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)

        @self.tree.command(
            name="getvipduration",
            description="Show the current VIP duration in hours.",
        )
        async def getvipduration(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                f"Current VIP duration: {self.vip_duration_hours:g} hours.",
                ephemeral=True,
            )
            schedule_ephemeral_cleanup(interaction)

        @self.tree.command(
            name="show_player_vip",
            description="Show a player's current VIP status and expiration via the HTTP API.",
        )
        @app_commands.describe(player_id="Player ID string from https://hllrecords.com")
        async def show_player_vip(interaction: discord.Interaction, player_id: str) -> None:
            await interaction.response.defer(ephemeral=True)
            now = datetime.now(timezone.utc)
            try:
                status = await asyncio.to_thread(
                    self.vip_service.get_player_vip_status,
                    player_id,
                )
            except VipHTTPError as exc:
                followup_message = await interaction.followup.send(
                    f"Unable to fetch VIP status for {player_id}: {exc}",
                    ephemeral=True,
                    wait=True,
                )
                schedule_ephemeral_cleanup(interaction, message=followup_message)
                return

            if status.expiration_utc:
                expiration_local = status.expiration_utc.astimezone(self.config.timezone)
                formatted = expiration_local.strftime("%Y-%m-%d %H:%M:%S %Z")
                if status.expiration_utc > now:
                    body = (
                        f"{player_id} currently has VIP access until {formatted} "
                        f"({self.config.timezone_name})."
                    )
                else:
                    body = (
                        f"{player_id} last had VIP access until {formatted} "
                        f"({self.config.timezone_name}); it has expired."
                    )
            else:
                body = f"No VIP records found for {player_id}."

            followup_message = await interaction.followup.send(
                body,
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)

        @self.tree.command(
            name="game_server_message",
            description="Send a custom message to Axis, Allies, or Both.",
        )
        @app_commands.describe(
            recipient="Who should receive the in-game direct message",
            message="Message text to send in-game",
        )
        @app_commands.choices(
            recipient=[
                app_commands.Choice(name="Axis", value="axis"),
                app_commands.Choice(name="Allies", value="allies"),
                app_commands.Choice(name="Both", value="both"),
            ]
        )
        async def game_server_message(
            interaction: discord.Interaction,
            recipient: app_commands.Choice[str],
            message: str,
        ) -> None:
            if not self._user_has_moderator_privileges(interaction.user):
                await interaction.response.send_message(
                    "You need moderator permissions to use this command.",
                    ephemeral=False,
                )
                return

            cleaned_message = message.strip()
            if not cleaned_message:
                await interaction.response.send_message(
                    "Message cannot be empty.",
                    ephemeral=False,
                )
                return

            await interaction.response.defer(ephemeral=False)
            try:
                result = await asyncio.to_thread(
                    self.vip_service.message_team,
                    recipient.value,
                    cleaned_message,
                    interaction.user.display_name,
                )
            except VipHTTPError as exc:
                await interaction.followup.send(
                    f"Unable to deliver message: {exc}",
                    ephemeral=False,
                    wait=True,
                )
                return
            except Exception as exc:
                logging.exception("Unexpected error while sending server message: %s", exc)
                await interaction.followup.send(
                    "Unexpected error while sending the in-game message.",
                    ephemeral=False,
                    wait=True,
                )
                return

            await interaction.followup.send(
                (
                    f"Recipient: {result.recipient}\n"
                    f"Message: {cleaned_message}\n"
                    f"Direct messages attempted: {result.attempted}\n"
                    f"Direct messages sent: {result.sent}\n"
                    f"Direct message failures: {result.failed}"
                ),
                ephemeral=False,
                wait=True,
            )

        @self.tree.command(
            name="vipassignlimit",
            description="View or update the weekly /assignvip usage limit.",
        )
        @app_commands.describe(limit="Optional new weekly limit per moderator (>=1)")
        async def vipassignlimit(interaction: discord.Interaction, limit: Optional[int] = None) -> None:
            if not self._user_has_moderator_privileges(interaction.user):
                await interaction.response.send_message(
                    "You need moderator permissions to use this command.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            if limit is None:
                used, current_limit = await self.vip_assign_limiter.get_usage(interaction.user.id)
                await interaction.response.send_message(
                    (
                        f"VIPAssignLimit is currently {current_limit} uses per moderator each week.\n"
                        f"You have used {used} time(s) this week. Resets Monday 01:00 {self.config.timezone_name}."
                    ),
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            if limit <= 0:
                await interaction.response.send_message(
                    "VIPAssignLimit must be at least 1 use per week.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            await interaction.response.defer(ephemeral=True)
            updated_limit = await self.vip_assign_limiter.set_limit(limit)
            followup_message = await interaction.followup.send(
                (
                    f"VIPAssignLimit updated to {updated_limit} uses per moderator each week. "
                    f"Resets Monday 01:00 {self.config.timezone_name}."
                ),
                ephemeral=True,
                wait=True,
            )
            schedule_ephemeral_cleanup(interaction, message=followup_message)

        @self.tree.command(
            name="health",
            description="Show bot health information.",
        )
        async def health(interaction: discord.Interaction) -> None:
            last_grant = self.last_grant_time
            if last_grant:
                local_dt = last_grant.astimezone(self.config.timezone)
                last_grant_text = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            else:
                last_grant_text = "None yet"
            http_base = self.config.http_credentials.base_url if self.config.http_credentials else "unset"
            msg = (
                f"VIP duration: {self.vip_duration_hours:g} hours\n"
                f"Last VIP grant: {last_grant_text}\n"
                f"HTTP API base: {http_base}"
            )
            await interaction.response.send_message(msg, ephemeral=True)
            schedule_ephemeral_cleanup(interaction)

        @self.tree.command(
            name="assignvip",
            description="Assign a temporary VIP Discord role to a member so they can claim VIP.",
        )
        @app_commands.describe(member="Select the server member to grant temporary VIP role to")
        async def assignvip(interaction: discord.Interaction, member: discord.Member) -> None:
            if not self._user_has_moderator_privileges(interaction.user):
                await interaction.response.send_message(
                    "You need moderator permissions to use this command.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            current_usage, current_limit = await self.vip_assign_limiter.get_usage(interaction.user.id)
            if current_usage >= current_limit:
                await interaction.response.send_message(
                    "Weekly limit reached - reset each Monday",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            role_id = self.config.vip_temp_role_id
            if not role_id:
                await interaction.response.send_message(
                    "VIP_TEMP_ROLE_ID is not configured. Set it in your environment or config.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            guild = interaction.guild
            if guild is None:
                await interaction.response.send_message(
                    "This command can only be used inside a server (guild).",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            role = guild.get_role(role_id)
            if role is None:
                await interaction.response.send_message(
                    f"Could not find role with ID {role_id} in this server.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            try:
                await member.add_roles(role, reason="Frontline Pass: temporary VIP role for claim")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "I don't have permission to assign that role. Ensure my role is above the VIP role.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return
            except discord.DiscordException:
                logging.exception("Failed to add temporary VIP role %s to %s", role_id, member.id)
                await interaction.response.send_message(
                    "Failed to assign the temporary VIP role due to an unexpected error.",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            usage_result = await self.vip_assign_limiter.try_consume(interaction.user.id)
            if not usage_result.allowed:
                try:
                    await member.remove_roles(
                        role,
                        reason="Frontline Pass: revert temporary VIP role after limit race",
                    )
                except discord.DiscordException:
                    logging.exception(
                        "Assigned VIP role %s to %s but failed to roll it back after usage limit rejection.",
                        role_id,
                        member.id,
                    )
                await interaction.response.send_message(
                    "Weekly limit reached - reset each Monday",
                    ephemeral=True,
                )
                schedule_ephemeral_cleanup(interaction)
                return

            claim_channel_id = self.config.vip_claim_channel_id or self.config.channel_id
            channel_mention = f"<#{claim_channel_id}>" if claim_channel_id else "the VIP channel"
            announcement = (
                f"Assigned {role.mention} to {member.mention} by {interaction.user.mention}.\n"
                f"Please go to {channel_mention} and press Get VIP, then enter the Player-ID when prompted.\n"
                "After you claim VIP, your temporary Discord role will be removed automatically."
            )

            try:
                await interaction.response.defer(ephemeral=True, thinking=False)
                deferred = True
            except discord.InteractionResponded:
                deferred = False

            sent_in_channel = False
            destination = interaction.channel
            if isinstance(destination, MessageableChannel):
                try:
                    await destination.send(announcement)
                    sent_in_channel = True
                except discord.Forbidden:
                    logging.info(
                        "Missing permission to post assignvip announcement in channel %s",
                        getattr(destination, "id", "unknown"),
                    )
                except discord.DiscordException:
                    logging.exception(
                        "Failed to post assignvip announcement in channel %s",
                        getattr(destination, "id", "unknown"),
                    )

            if deferred:
                followup_text = (
                    f"Assigned {role.mention} to {member.mention}. Posted instructions here and notified the player."
                    if sent_in_channel
                    else (
                        "Assigned the role, but I couldn't post instructions in this channel. "
                        "Please confirm permissions."
                    )
                )
                with contextlib.suppress(discord.DiscordException):
                    await interaction.followup.send(followup_text, ephemeral=True, wait=False)
            elif not sent_in_channel:
                # As a last resort, attempt to deliver the announcement via a follow-up.
                with contextlib.suppress(discord.DiscordException):
                    await interaction.followup.send(announcement, ephemeral=False, wait=False)

            dm_message = (
                f"Hi {member.display_name}, {interaction.user.display_name} assigned you the {role.name} role so you can claim VIP.\n"
                f"Head over to {channel_mention}, press Get VIP, and paste your player_id from hllrecords.com.\n"
                "Once you claim VIP, your temporary Discord role will be removed automatically."
            )
            try:
                await member.send(dm_message)
            except discord.Forbidden:
                logging.info("Cannot DM user %s; DMs disabled or blocked.", member.id)
            except discord.HTTPException:
                logging.exception("Failed to send assignvip DM to user %s", member.id)


def create_bot(config: AppConfig, vip_service: VipService) -> commands.Bot:
    return FrontlinePassBot(config, vip_service)


def main() -> None:
    config = load_config()
    vip_service = VipService(config)
    bot = create_bot(config, vip_service)
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
