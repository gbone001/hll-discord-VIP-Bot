# Frontline Pass

Frontline Pass is a Discord bot that lets Hell Let Loose players enter their `player_id` from hllrecords.com on demand and self-grant temporary VIP status via the CRCON HTTP API using a bearer token or login credentials.

## Highlights

- **Self-service VIPs** - players press **Get VIP**, paste their Player-ID, and receive VIP without moderator intervention.
- **Quick VIP panel** - members with persisted Quick VIP role allocations can press **Quick VIP**, paste any player_id, and grant a fixed 10-minute VIP window.
- **Switch Me panel** - players can press **Switch Me**, paste the same `player_id` from hllrecords.com used by the VIP form, and request a move to the opposite team.
- **Moderator assist: /assignvip** - moderators can grant a temporary Discord role to a member so they can access the VIP channel to claim; the role is removed automatically after claiming.
- **Team messaging: /game_server_message** - moderators can send a custom in-game message to Axis, Allies, or Both and get delivery confirmation.
- **HTTP transport** - all VIP grants are issued through the CRCON HTTP API (bearer token preferred, login fallback optional).
- **Always-on control panel** - the Discord message survives restarts and can be reposted via `/repost_frontline_controls`.
- **No local database** - no SQLite or JSON persistence; everything is handled through the modal and CRCON.
- **Timezone-aware** - expiry timestamps are rendered in your configured locale.

## Install & Run

```bash
git clone https://github.com/yourusername/hall-frontline-pass.git
cd hall-frontline-pass
pip install -r requirements.txt

copy config.example.jsonc config.jsonc   # Windows
# or
cp config.example.jsonc config.jsonc     # macOS / Linux
```

Environment-specific overrides are optional. Create an `.env` only if you need to override values defined in `config.jsonc`.

If you want to run entirely from environment variables, copy `.env.dist` to `.env` and fill in the required sections from top to bottom. The minimum working set is `DISCORD_TOKEN`, `CHANNEL_ID`, `VIP_DURATION_HOURS`, `LOCAL_TIMEZONE`, `CRCON_HTTP_BASE_URL`, and either `CRCON_HTTP_BEARER_TOKEN` or both `CRCON_HTTP_USERNAME` and `CRCON_HTTP_PASSWORD`.

Configure `config.jsonc`, then start the bot:

```bash
python frontline-pass.py
```

## Persistent systemd service

The repo ships with a systemd template and helper script so the bot restarts automatically after crashes or reboots.

1. Create the virtual environment and install dependencies (`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`).
2. Adjust `config.jsonc` for your deployment (set `FRONTLINE_CONFIG_PATH` if you keep it outside the repository). Optional runtime overrides are still read from `/opt/hall-frontline-pass/.env` when present.
3. Install the unit and enable it at boot:
   ```bash
   ./manage_frontline_pass.sh install        # copies hall-frontline-pass.service.dist -> /etc/systemd/system/hall-frontline-pass@.service
   ```
   By default the script enables `hall-frontline-pass@$(whoami).service`. Override `BOT_SERVICE_USER` if you run under a different account.
4. Control the bot with the helper script (wraps `systemctl`):
   ```bash
   ./manage_frontline_pass.sh start
   ./manage_frontline_pass.sh status
   ./manage_frontline_pass.sh restart
   ```
   Use `stop` to take it offline, or `run` to execute the bot in the foreground for development (`./manage_frontline_pass.sh run`).

The systemd unit uses `Restart=on-failure`, so systemd automatically relaunches the bot if it exits unexpectedly. `WantedBy=multi-user.target` ensures it starts on every boot.

## Configuration Cheat Sheet

All primary settings live in `config.jsonc` (JSON5 syntax). Environment variables are optional overrides for deployments where you can’t use files (CI, containers, etc.).

| Key | Required | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | Yes | Discord bot token. |
| `VIP_DURATION_HOURS` | Yes | Duration of each VIP grant. |
| `CHANNEL_ID` | Yes | Channel hosting the control panel buttons. |
| `QUICK_VIP_CHANNEL_ID` | Optional | Separate channel hosting the persistent Quick VIP panel. Members with a persisted Quick VIP role allocation can use it to grant a fixed 10-minute VIP. |
| `SWITCH_ME_CHANNEL_ID` | Optional | Separate channel hosting the persistent Switch Me panel. Players paste the same `player_id` from hllrecords.com used by the VIP form to request a move to the opposite team. |
| `QUICK_VIP_ROLE_IDS` | Optional | Backward-compatible seed list for Quick VIP allocations. On startup, these role IDs are persisted to `quick_vip_allocations.json` as `1 use / 48 hours`. Defaults include `MSU` (`1322175167685988403`) and `Randoms` (`1440534025054720020`). Use `/create_quick_vip_allocation` for ongoing changes. |
| `LOCAL_TIMEZONE` | Yes | Timezone for human-readable expiry timestamps (e.g. `Australia/Sydney`). |
| `ANNOUNCEMENT_MESSAGE_ID` | Optional | Reuse an existing Discord message for the control panel. |
| `QUICK_VIP_ANNOUNCEMENT_MESSAGE_ID` | Optional | Reuse an existing Discord message for the Quick VIP control panel. |
| `SWITCH_ME_ANNOUNCEMENT_MESSAGE_ID` | Optional | Reuse an existing Discord message for the Switch Me control panel. |
| `MODERATOR_ROLE_ID` | Optional | Discord role ID treated as moderator for privileged commands such as `/assignvip`, `/set_vip_duration`, and `/game_server_message`. |
| `VIP_TEMP_ROLE_ID`, `VIP_CLAIM_CHANNEL_ID` | Optional | Used by `/assignvip`. `VIP_TEMP_ROLE_ID` is a temporary Discord role that grants access to your VIP claim channel. `VIP_CLAIM_CHANNEL_ID` is the channel ID where the control panel lives (falls back to `CHANNEL_ID` if unset). |
| `VIP_ASSIGN_LIMIT` | Optional | Weekly per-moderator cap for `/assignvip`. Defaults to `5` uses and resets every Monday at 01:00 in `LOCAL_TIMEZONE`. |
| `FRONTLINE_STATE_DIR` | Optional | Directory used for runtime state such as limiter JSON files and Quick VIP allocations. Defaults to the app directory locally; set this to `/data` on Railway or in containers so allocation policy and cooldown state survive restarts. |
| `COMMAND_GUILD_IDS` / `COMMAND_GUILD_ID` | Optional | Comma-separated guild IDs (or a single ID) to sync slash commands instantly to those servers. If unset, commands are synced globally (may take up to ~1 hour to propagate). |
| `CRCON_HTTP_BASE_URL` | Yes | CRCON host (omit `/api`; the bot appends it automatically). |
| `CRCON_HTTP_BEARER_TOKEN` | Yes\* | Pre-generated CRCON token. Required unless you supply username/password. |
| `CRCON_HTTP_USERNAME`, `CRCON_HTTP_PASSWORD` | Conditional | CRCON login credentials. Provide both instead of a bearer token if you want automatic logins and token refreshes. |
| `CRCON_HTTP_VERIFY` | Optional | `true` by default. Set to `false` when using self-signed certificates. |
| `CRCON_HTTP_TIMEOUT` | Optional | CRCON HTTP timeout in seconds (default `20`). |

The bot validates required settings on startup and exits with a clear error when something is missing or malformed.

### Sample `config.jsonc`

```json5
{
  DISCORD_TOKEN: "your-discord-token",
  CHANNEL_ID: 123456789012345678,
  QUICK_VIP_CHANNEL_ID: 234567890123456789,
  SWITCH_ME_CHANNEL_ID: 345678901234567890,
  QUICK_VIP_ROLE_IDS: [1322175167685988403, 1440534025054720020], // initial seed only; manage later with slash commands
  VIP_DURATION_HOURS: 24,
  LOCAL_TIMEZONE: "Australia/Sydney",
  FRONTLINE_STATE_DIR: "/data",

  CRCON_HTTP_BASE_URL: "https://crcon.example.com:8010",
  CRCON_HTTP_BEARER_TOKEN: "your-pre-generated-token",
  # Alternatively provide username/password instead of a bearer token:
  # CRCON_HTTP_USERNAME: "vipbot",
  # CRCON_HTTP_PASSWORD: "supersecret",
  CRCON_HTTP_VERIFY: true,
  CRCON_HTTP_TIMEOUT: 20,

  MODERATOR_ROLE_ID: null,
  ANNOUNCEMENT_MESSAGE_ID: null,
  QUICK_VIP_ANNOUNCEMENT_MESSAGE_ID: null,
  SWITCH_ME_ANNOUNCEMENT_MESSAGE_ID: null
}
```

## Bot Experience

1. **Get VIP** - clicking **Get VIP** opens a modal that collects the player's `player_id`. Enter the 32-character string (for example `2805d5bbe14b6ec432f82e5cb859d012` from https://hllrecords.com) and the bot will call the CRCON HTTP API to grant VIP, then report the expiry time back to you. The `player_id` is not persisted; users paste it each time they request access.

Admins can refresh the message at any time with `/repost_frontline_controls`.

### Quick VIP panel

1. Users open the dedicated `QUICK_VIP_CHANNEL_ID` channel and press **Quick VIP (10 min)**.
2. The user must hold either the configured `MODERATOR_ROLE_ID` or a Discord role with a persisted Quick VIP allocation.
3. They paste the target player's `player_id` from [https://hllrecords.com](https://hllrecords.com).
4. The bot grants a fixed 10-minute VIP window starting from the current time.
5. Usage limits are controlled per Discord role with `/create_quick_vip_allocation role uses hours`.
6. The configured `MODERATOR_ROLE_ID` can use Quick VIP without a cooldown cap.
7. This does not add 10 minutes to an existing VIP total; it sets the expiration to 10 minutes from the moment the button is used.

Quick VIP allocation policy is stored in `quick_vip_allocations.json` under `FRONTLINE_STATE_DIR`. In Railway/container deployments, set `FRONTLINE_STATE_DIR=/data` so the file is written to `/data/quick_vip_allocations.json`.

Admin commands:

1. `/create_quick_vip_allocation role uses hours` creates or updates a role allocation.
2. `/delete_quick_vip_allocation role` removes a role allocation and its tracked usage.
3. `/quick_vip_allocations` lists active role allocations.

Startup migration runs only when `quick_vip_allocations.json` does not already exist:

1. `QUICK_VIP_ROLE_IDS` are seeded as `1 use / 48 hours`. The built-in defaults are `MSU` (`1322175167685988403`) and `Randoms` (`1440534025054720020`).
2. Legacy clan Quick VIP role names are resolved from connected Discord guilds and seeded as `5 uses / 24 hours` when matching roles are found.
3. Existing old usage files are not converted because they did not record which role allocation consumed each use.

Admins can refresh the Quick VIP panel at any time with `/repost_quick_vip_controls`.

### Switch Me panel

1. Users open the dedicated `SWITCH_ME_CHANNEL_ID` channel and press **Switch Me**.
2. They paste the same 32-character `player_id` from [https://hllrecords.com](https://hllrecords.com) used by the VIP form.
3. The bot looks up the player on the configured CRCON server, detects the current team, and checks whether the opposite team has room.
4. If the opposite team has space available, the bot calls the CRCON switch endpoint immediately.
5. If the player is not in-game, has no supported team assignment, or the opposite team is full, the bot fails explicitly and shows the reason.
6. This MVP is single-CRCON and does not queue full-team requests.

Admins can refresh the Switch Me panel at any time with `/repost_switch_me_controls`.

### New: Moderator flow with `/assignvip`

1. A moderator runs `/assignvip` and selects a member from the server-wide autocomplete picker.
2. The bot assigns the `VIP_TEMP_ROLE_ID` role to that member and points them to `VIP_CLAIM_CHANNEL_ID` (or `CHANNEL_ID`).
3. The member presses **Get VIP**, pastes their Player-ID, and submits the modal.
4. After a successful claim, the bot automatically removes the temporary Discord role so access reverts to normal.
5. Each moderator can only run `/assignvip` up to `VIP_ASSIGN_LIMIT` times per week (resets Mondays at 01:00 local time). Use `/vipassignlimit` to check your usage or update the cap.

### Check a player's VIP status

Use `/show_player_vip` and paste the Player ID string that you can copy from [https://hllrecords.com](https://hllrecords.com). The bot calls the CRCON HTTP API and replies with the current VIP status and the expiration time rendered in your configured local timezone.

### Send team message

Use `/game_server_message` with:

1. `recipient`: `Axis`, `Allies`, or `Both`
2. `message`: the text to deliver in-game

The bot will:

1. Query current players from CRCON.
2. Send direct in-game messages to players matching the selected team scope.
3. Reply with delivery stats (`attempted`, `sent`, `failed`) so you can confirm it sent.

Required CRCON API permissions for the bot account:

- `api.can_view_get_players`
- `api.can_message_players`

## Deployment Notes

- **Local / bare metal** - run `python frontline-pass.py` under your favorite supervisor (systemd, pm2, tmux).
- **Docker / Compose** - use `docker compose up --build -d` with a populated `.env`. The compose stack persists runtime state in a named Docker volume mounted at `/data`.
- **Railway** - the repo ships with `railway.toml` and a Dockerfile. Railway builds from the Dockerfile, so the image `CMD` is the runtime entrypoint. Set the required variables (see `.env.dist`) in the Railway dashboard/CLI before deploying—at minimum `DISCORD_TOKEN`, `VIP_DURATION_HOURS`, `CHANNEL_ID`, `LOCAL_TIMEZONE`, `CRCON_HTTP_BASE_URL`, and either `CRCON_HTTP_BEARER_TOKEN` or (`CRCON_HTTP_USERNAME` + `CRCON_HTTP_PASSWORD`). If you attach a Railway volume at `/data`, set `FRONTLINE_STATE_DIR=/data`; startup now validates that the directory is writable so Quick VIP allocation policy and cooldown state cannot silently fall back to ephemeral storage.

## Quick Troubleshooting Checklist

Run these commands from a trusted host to confirm CRCON HTTP connectivity:

1. **Login endpoint**
   ```bash
   curl -sS -X POST https://<crcon-host>:8010/api/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"<user>","password":"<pass>"}'
   ```
   Expect a JSON payload containing a `token`, `jwt`, or `access_token` field.

2. **Authorized VIP grant**
   ```bash
   curl -sS -X POST https://<crcon-host>:8010/api/add_vip \
     -H 'Authorization: Bearer <token>' \
     -H 'Content-Type: application/json' \
     -d '{"player_id":"76561198000000000","description":"frontline-pass","expiration":"2025-11-01T12:00:00Z"}'
   ```
   A 200 response confirms the account has the `api.can_add_vip` permission.

3. **TLS issues?** If either request fails with certificate errors and you trust the endpoint, repeat with `curl -k` and set `CRCON_HTTP_VERIFY=false` in `.env` so the bot also skips certificate validation.

## Requirements

- Python 3.8+
- `discord.py`
- `pytz`
- `python-dotenv`
- `requests`

## License

Frontline Pass is released under the MIT License. See [LICENSE](LICENSE) for details.
