# AGENTS.md

Handy notes for agents working on this repo, especially to avoid the usual
mistakes. Long-lived design goes in `CLAUDE.md`; this covers the agent-specific
gotchas and quick-start facts.

## Project

Multi-way chat bridge across any number of **Discord**, **Stoat** (public +
self-hosted), and **IRC** servers, configured entirely in `config.yaml` (no code
changes needed to add another server). Messages flow from each connector's
sender into every other connector's receiver. Incoming messages are relayed into
Discord "as" the origin Stoat/IRC user via per-channel **webhooks** (username +
avatar override), not the bridge bot's own identity.

Read `CLAUDE.md` first for the deep architecture and the accurate test-scope
summary.

## Setup & Commands

- Env: Windows (Git Bash); `python -m venv` + activate; no separate conda needed.
- Test: `pip install -e ".[test]"` then `pytest` (installed `pytest`+`pytest-asyncio`; `pip install -e .` alone skips the test extras).
- Don't invoke linters/type-checkers/JS tooling: this repo (Python) has no lint/typecheck/formatter/CI config, so don't run tools that don't exist here.
- `.env` and `config.yaml` are real runtime inputs — don't invent values; use fixtures / env vars / the `config.yaml.example`.

## Configuration & Connector Add-How

- Field resolution rules are documented for agents in `config.py`'s module docstring and the repo README.
- Every connector entry has a unique `id` (used as `<source>` for the admin
  commands). IDs must be unique across all three connector kinds (verified by
  `tests/test_config.py`).
- **Adding a connector** = one new `config.yaml` list entry under `discord:` /
  `stoat:` / `irc:`; fields can be literals or `{SECTION}__{index}__{FIELD}` env
  vars (e.g. `STOAT__1__TOKEN` is the 2nd `stoat:` entry's token).
- `config.py` calls `load_dotenv()` at import time, so tests that need
  environment loading must invoke it (or set vars explicitly); they don't rely
  on a loaded `.env` being present on disk.

## Layout

- `src/stoat_discord_bridge/` — `config.py`, `models.py` (StandardMessage),
  `admin_commands.py`, `bridge.py` (BridgeCoordinator), `status.py`
  (HealthTracker), `health_server.py` (liveness `/healthz`, `/status`),
  `channel_structure.py`, and `storage/` (`mongo.py`, `channel_mappings.py`,
  `message_sync.py`, `emoji_mappings.py`, **`user_mappings.py`**), plus
  `services/` (`base.py`, `formatting.py`, `mentions.py`, `irc_service.py`,
  `stoat_service.py`, `discord_service.py`).
- Connector instances are per-config: one sender/receiver per `config.yaml`
  entry. `services/formatting.py` formats messages per platform; `services/mentions.py`
  translates platform-specific mentions (e.g. `/u@#channel` <-> `@nickname`).
- `scripts/` may hold runtime helpers but contains no source logic.

## Testing

- pytest runs with `os.environ` cleared; tests use isolated_env + monkeypatch to
  load envs from temp dicts rather than trusting a global env.
- `conftest.py`/`tests/conftest.py` provide a `FakeDb` with a fake Mongo, a
  `FakeChannel`, and `FakeConnector`/`FakeStandardMessage`; fixtures are
  `fake_db`, `fake_channel`, `fake_connector`, `fake_msg`, and `connectors`.
- Tests exercise `services/base.py` for the partial-return contract:
  `PartialRelayError` signals partial delivery so callers track state and return
  native IDs.
- IRC tests build a real `IrcService` instance and monkeypatch its `connection`
  property with a `FakeConnection` (see `test_irc_service.py`'s `_patch_connection`)
  so `_check_is_oper`/`_resolve_whois` resolve against faked WHOIS responses —
  they never stand up a live IRC server, so all network calls are faked.

## Gotchas

- Git Bash (this repo's shell, on cygpath): Windows paths render as `/c/...`,
   `/d/...`, `/tmp` maps to `C:\Users\<user>\AppData\Local\Temp`; the external
   `bash` tool defaults to plain Git Bash unless otherwise configured.
- `pytest` needs NO `@pytest.mark.asyncio`: pyproject enables
  `asyncio_mode = "auto"` and `testpaths = ["tests"]`.

## Conventions

- Prefer editing existing files over adding new ones.
- Don't add emoji unless explicitly requested.
