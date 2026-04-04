# AGENTS.md

## Rule
Every project in this workspace must include a root-level `AGENTS.md`.

This repository uses `AGENTS.md` as the canonical place for agent-facing project instructions. If a new project is created from this repository or alongside it, create `AGENTS.md` at the project root before adding implementation work.

## Working expectations
- Prefer correctness over speed.
- Never guess missing requirements. Ask or state assumptions explicitly.
- No silent failures or hidden fallbacks.
- Keep solutions minimal, production-ready, and readable.
- Use clear naming and straightforward control flow.
- Add comments only where the logic is not obvious.
- Avoid unnecessary dependencies.
- Design changes for logging, observability, and explicit error handling.

## Repository notes
- Runtime entrypoint: `frontline-pass.py`
- Primary configuration: `config.jsonc`
- Optional environment overrides: `.env`
- Tests: `tests/`

## Project
`HLL CRCON Discord Automation`

This repository is a Hell Let Loose Discord automation bot that grants temporary VIP access, supports moderator-assisted VIP flows, and sends in-game team messages through the CRCON HTTP API.

## Current architecture
- The current runtime is a single-entrypoint implementation in `frontline-pass.py`.
- Internal responsibilities are already separated by class even though the repository has not yet been split into `commands/`, `events/`, `services/`, and `utils/` packages.
- When making substantive backend changes, prefer extracting or extending service-style components instead of adding more Discord or CRCON logic inline.

## Services
- `VipHttpClient` in `frontline-pass.py` handles CRCON HTTP authentication, reauthentication, and API calls such as `add_vip`, `get_player_profile`, `get_players`, `message_player`, and broadcast actions.
- `VipService` in `frontline-pass.py` handles VIP grant orchestration, expiration calculation, player VIP status lookups, and team message dispatch.
- `AnnouncementManager` in `frontline-pass.py` handles persistent Discord control-panel message creation, refresh, repost, and cleanup.
- `VipAssignLimiter` in `frontline-pass.py` handles weekly moderator assignment limits for `/assignvip`.
- `RollingWindowLimiter` in `frontline-pass.py` handles rolling-window cooldown and quota behavior for Quick VIP usage.

## Discord surfaces
- Persistent button views: standard VIP claim and Quick VIP claim.
- Slash/admin flows: `/assignvip`, `/vipassignlimit`, `/show_player_vip`, `/game_server_message`, repost commands, and VIP duration management.
- Startup flow: command sync, persistent view registration, and announcement recovery on `on_ready`.

## Rules
- No direct CRCON HTTP calls in Discord button handlers, modal submit handlers, or slash command bodies. Route game-server actions through `VipService` or a dedicated service extracted from it.
- No silent fallback behavior. Configuration validation, CRCON failures, and Discord permission issues must fail explicitly with logs and admin-visible errors where appropriate.
- All Discord announcement repost/refresh behavior must go through `AnnouncementManager`.
- All VIP grant calculations must stay centralized so extension and fixed-duration behavior remain consistent.
- Deduplicate alerting or repeated automation actions before sending Discord notifications or game-server messages.
- Preserve async-safe Discord patterns. Avoid blocking the event loop with new long-running synchronous work.
- Keep secrets out of code and out of committed config files. Use `config.jsonc`, `.env`, or environment variables only.

## Events and workflows to handle
- `vip_claim_requested`: user presses the main VIP button and submits a Player ID.
- `quick_vip_requested`: authorized user presses the Quick VIP button and grants a fixed 10-minute VIP.
- `vip_assigned_by_moderator`: moderator grants temporary Discord access so a member can claim VIP.
- `vip_status_checked`: moderator or admin checks a player's current VIP expiration.
- `team_message_requested`: moderator sends an in-game message to Axis, Allies, or Both.
- `announcement_repost_requested`: admin refreshes the persistent control-panel messages.
- `bot_ready`: reconnect, resync commands, restore persistent views, and ensure announcement messages exist.

## HLL automation guidance
- Prefer explicit service boundaries for future extraction:
  - `crcon_service.py` for CRCON/RCON transport and retries.
  - `player_service.py` for player lookups, normalization, and player-state handling.
  - `alert_service.py` for Discord alert formatting, cooldowns, and deduplication.
- If player join/leave tracking, admin alerts, or rule enforcement are added later, implement them as services and event handlers instead of extending slash command code paths directly.
- Handle edge cases explicitly: missing player data, duplicate events, CRCON timeouts, Discord API failures, and partial message-delivery failures.

## Enforcement
The test suite includes a guard that fails if the repository root does not contain `AGENTS.md`.
