# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

## Project Overview

Polytopia-ELO-Bot is a Discord bot for the mobile game Polytopia. It provides matchmaking, ELO-based leaderboards, and league management across multiple Discord servers (primarily the main Polytopia server and PolyChampions).

## Tech Stack

- Python 3.9+
- discord.py ~2.1 (Discord bot framework)
- Peewee ORM with PostgreSQL database
- FastAPI for optional REST API
- Matplotlib/Pandas/SciPy for statistics and graphing

## Running the Bot

```bash
# Activate virtual environment
source .venv/bin/activate  # or source bin/activate for legacy venv

# Install dependencies
pip install -r requirements.txt

# Run the Discord bot
python bot.py

# Run with options
python bot.py --skip_tasks        # Skip background tasks
python bot.py --add_default_data  # Initialize default database data
python bot.py --recalc_elo        # Recalculate all ELO ratings
python bot.py --game_export       # Export game data to file
```

## Running the API Server

```bash
python3 -m uvicorn server:server --host 0.0.0.0 --port 8000
```

## Configuration

- `config.ini` - Discord API key, PostgreSQL credentials, owner ID (copy from `config.ini-EXAMPLE`)
- `server_settings.py` - Server-specific settings, channel IDs, role names (copy from `server_settings-EXAMPLE.py`)

## Architecture

### Entry Points
- `bot.py` - Main Discord bot entry point. Initializes `MyBot` class and loads cog extensions
- `server.py` - FastAPI server entry point for REST API

### Core Modules (in `modules/`)
- `models.py` - Peewee ORM models (DiscordMember, Player, Team, Game, GameSide, Lineup, etc.). Contains ELO calculation logic
- `utilities.py` - Helper functions: DB connection management, game record locking, role lookups
- `settings.py` - Runtime settings, permission checks (`is_staff`, `is_mod`), guild configuration lookup via `guild_setting()`

### Discord Cogs (in `modules/`)
- `games.py` - Core game commands: win/lose, game info, player stats, ELO graphs
- `matchmaking.py` - Open game hosting, joining via reactions, matchmaking lobbies
- `league.py` - PolyChampions-specific: team management, drafts, house/tier system
- `administration.py` - Staff commands: game corrections, bans, bulk operations
- `misc.py` - Utility commands: guide, roles, info
- `bullet.py` - Bullet league management
- `customhelp.py` - Custom help command formatting
- `api_cog.py` - Discord cog that wraps API functionality

### Key Data Models
- `DiscordMember` - Discord user with Polytopia name
- `Player` - Server-specific player profile (DiscordMember + guild)
- `Team` - Competitive team with ELO rating
- `House` - Affiliation of teams (PolyChampions)
- `Game` - A match with sides, date, completion status
- `GameSide` - One side of a game (team or players)
- `Lineup` - Player assignments within a GameSide

### Multi-Server Architecture
The bot runs on multiple Discord servers with per-guild settings. `settings.config` (loaded from `server_settings.py`) contains server-specific configuration. Use `settings.guild_setting(guild_id, 'setting_name')` to retrieve guild-specific values.

## Command Prefix

Default prefix is `$` but can be configured per-guild via `command_prefix` setting.

## Permission System

User levels (0-7) control command access:
- Level 0: Unregistered
- Level 1-3: Progressive game hosting/joining permissions
- Level 4: Advanced matchmaking
- Level 5: Staff (helper roles)
- Level 6: Mod
- Level 7: Owner

Check with `settings.is_staff()`, `settings.is_mod()`, `settings.get_user_level()`.
