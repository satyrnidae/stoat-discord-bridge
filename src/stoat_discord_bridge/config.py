"""Static config (config.yaml) + secret env vars (.env) for the bridge.

config.yaml lists every connector the bridge should run - any number of
Discord guilds, Stoat servers, and IRC networks. Any field on any connector
can be supplied three ways, in priority order:

1. An `{SECTION}__{index}__{FIELD}` env var - Azure App Configuration /
   ASP.NET Core-style hierarchical env var binding, where `index` is the
   connector's 0-based position within its kind's config.yaml list (e.g.
   `IRC__0__NICKSERV_PASSWORD`, `STOAT__1__TOKEN` for the 2nd `stoat:`
   entry). Reordering a kind's list changes its indices, so prefer this only
   for fields you're comfortable pinning to list position.
2. An `{SECTION}__{index}__{FIELD}_FILE` env var naming a file whose
   contents are the value - the Docker/Kubernetes secrets convention (see
   e.g. the official postgres image's `POSTGRES_PASSWORD_FILE`). Lets a
   secret live as a Docker secret (mounted at `/run/secrets/<name>`) rather
   than plaintext in `.env`, where an accidental `docker inspect` or
   `docker compose config` prints every env var back out. Trailing
   whitespace is stripped; setting both this and the plain env var for the
   same field is a `ConfigError`, not a silent pick-one.
3. A literal value directly in config.yaml.

This keeps secrets out of config.yaml (which is itself gitignored anyway -
see config.yaml.example) while letting the *shape* of the deployment (which
servers, how many) live wherever you'd like: all in config.yaml, all in env
vars, or a mix.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

_DEFAULT_CONFIG_PATH = "config.yaml"


@dataclass(frozen=True)
class DiscordConnectorConfig:
    id: str
    label: str
    guild_id: int
    bot_token: str
    # Whether a /link-user'd sender should be masqueraded (via webhook
    # username/avatar override) as their linked local Discord identity
    # rather than their remote (Stoat/IRC) one. Defaults on; set false to
    # fall back to always showing the remote identity, e.g. while
    # diagnosing local-user masquerade failures.
    enable_local_user_masquerade: bool = True


@dataclass(frozen=True)
class StoatConnectorConfig:
    id: str
    label: str
    server_id: str
    api_url: str
    bot_token: str
    # Whether a /link-user'd sender should be masqueraded as their linked
    # local Stoat identity (display name + avatar) rather than their remote
    # (Discord/IRC) one. Defaults on; set false to fall back to always
    # masquerading as the remote identity, e.g. while diagnosing
    # local-user masquerade failures on a self-hosted Stoat instance.
    enable_local_user_masquerade: bool = True
    # When a Discord thread is mirrored here as a channel under a Category
    # named after its parent channel (see
    # DiscordSenderService._handle_thread_create), also move the parent
    # channel itself into that Category, at the top - so `#bot-config` and
    # every thread spawned from it sit together under one "bot-config"
    # Category. Re-checked on every relayed message, so turning it on
    # mid-deployment takes effect without a restart. Defaults on.
    group_parent_channel_with_threads: bool = True


@dataclass(frozen=True)
class IrcConnectorConfig:
    id: str
    label: str
    host: str
    port: int
    use_tls: bool
    nick: str
    nickserv_password: str | None
    default_channel_modes: str | None
    # RFC1459 ident (the "user" field of the USER command / left half of the
    # hostmask) - distinct from `nick` and from `oper_account` below. Most
    # ircds still enforce the historical ident-daemon length cap.
    ident: str | None
    # Login name for the OPER command - deliberately a separate field from
    # `ident` (an oper block's name is commonly *not* the connection ident,
    # and networks generally prefer they differ).
    oper_account: str | None
    oper_password: str | None
    client_cert_file: str | None
    client_key_file: str | None
    # Whether a /link-user'd sender should be relayed under their linked
    # local IRC nick rather than their remote (Discord/Stoat) display name.
    # Defaults on; set false to fall back to always showing the remote
    # identity, e.g. while diagnosing local-user masquerade failures.
    enable_local_user_masquerade: bool = True


@dataclass(frozen=True)
class MongoConfig:
    uri: str
    db_name: str


@dataclass(frozen=True)
class BridgeConfig:
    discord: list[DiscordConnectorConfig]
    stoat: list[StoatConnectorConfig]
    irc: list[IrcConnectorConfig]
    mongo: MongoConfig


class ConfigError(Exception):
    """Raised for a malformed config.yaml or a referenced .env var that isn't set."""


def _read_secret_file(path: str, *, env_name: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"couldn't read secret file '{path}' referenced by '{env_name}': {exc}") from exc


def _resolve_env(env_name: str) -> str | None:
    """Resolve one env var's value per the plain-value/`_FILE` priority
    documented at the top of this module. Shared by `_resolve` (per-connector
    fields) and `load_config`'s Mongo URI handling, which resolves an env var
    named indirectly (via config.yaml's `mongo.uri_env`) rather than one
    built from a fixed `{SECTION}__{index}__{FIELD}` pattern."""
    file_env_name = f"{env_name}_FILE"
    value = os.environ.get(env_name)
    file_path = os.environ.get(file_env_name)
    if value and file_path:
        raise ConfigError(f"both '{env_name}' and '{file_env_name}' are set - use only one")
    if value:
        return value
    if file_path:
        return _read_secret_file(file_path, env_name=file_env_name)
    return None


def _resolve(entry: dict, *, section: str, index: int, field: str, yaml_key: str | None = None) -> str | None:
    """Resolve one connector field's raw value, per the priority order
    documented at the top of this module. `field` names the env var's
    `{FIELD}` segment and, unless overridden, the config.yaml key too - the
    two commonly match (`host`), but e.g. the bot-token field is
    `field="token"` (to match the `TOKEN` env segment already in use) while
    its config.yaml/dataclass key is `bot_token`.

    Returns None - not a type-specific default - if nothing supplies a
    value, leaving default handling and type coercion (int/bool/etc.) to the
    caller."""
    yaml_key = yaml_key or field

    env_name = f"{section.upper()}__{index}__{field.upper()}"
    value = _resolve_env(env_name)
    if value:
        return value

    if yaml_key in entry:
        return entry[yaml_key]

    return None


def _require(value, *, connector_id: str, section: str, index: int, field: str) -> str:
    if not value:
        env_name = f"{section.upper()}__{index}__{field.upper()}"
        raise ConfigError(
            f"connector '{connector_id}': '{field}' is required - set it directly in config.yaml "
            f"or via env var '{env_name}'"
        )
    return value


def _as_bool(value, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_config(path: str | Path | None = None) -> BridgeConfig:
    config_path = Path(path or os.environ.get("BRIDGE_CONFIG_PATH", _DEFAULT_CONFIG_PATH))
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {config_path}") from exc

    discord = []
    for index, entry in enumerate(raw.get("discord", [])):
        connector_id = entry["id"]
        bot_token = _require(
            _resolve(entry, section="discord", index=index, field="token", yaml_key="bot_token"),
            connector_id=connector_id,
            section="discord",
            index=index,
            field="token",
        )
        guild_id = _require(
            _resolve(entry, section="discord", index=index, field="guild_id"),
            connector_id=connector_id,
            section="discord",
            index=index,
            field="guild_id",
        )
        discord.append(
            DiscordConnectorConfig(
                id=connector_id,
                label=entry.get("label", connector_id),
                guild_id=int(guild_id),
                bot_token=bot_token,
                enable_local_user_masquerade=_as_bool(
                    _resolve(entry, section="discord", index=index, field="enable_local_user_masquerade"),
                    default=True,
                ),
            )
        )

    stoat = []
    for index, entry in enumerate(raw.get("stoat", [])):
        connector_id = entry["id"]
        bot_token = _require(
            _resolve(entry, section="stoat", index=index, field="token", yaml_key="bot_token"),
            connector_id=connector_id,
            section="stoat",
            index=index,
            field="token",
        )
        server_id = _require(
            _resolve(entry, section="stoat", index=index, field="server_id"),
            connector_id=connector_id,
            section="stoat",
            index=index,
            field="server_id",
        )
        api_url = _require(
            _resolve(entry, section="stoat", index=index, field="api_url"),
            connector_id=connector_id,
            section="stoat",
            index=index,
            field="api_url",
        )
        stoat.append(
            StoatConnectorConfig(
                id=connector_id,
                label=entry.get("label", connector_id),
                server_id=server_id,
                api_url=api_url,
                bot_token=bot_token,
                enable_local_user_masquerade=_as_bool(
                    _resolve(entry, section="stoat", index=index, field="enable_local_user_masquerade"),
                    default=True,
                ),
                group_parent_channel_with_threads=_as_bool(
                    _resolve(entry, section="stoat", index=index, field="group_parent_channel_with_threads"),
                    default=True,
                ),
            )
        )

    irc = []
    for index, entry in enumerate(raw.get("irc", [])):
        connector_id = entry["id"]
        host = _require(
            _resolve(entry, section="irc", index=index, field="host"),
            connector_id=connector_id,
            section="irc",
            index=index,
            field="host",
        )
        port = _resolve(entry, section="irc", index=index, field="port") or 6697
        nick = _resolve(entry, section="irc", index=index, field="nick") or "StoatDiscordBridge"
        ident = _resolve(entry, section="irc", index=index, field="ident")
        if ident is not None and not (1 <= len(ident) <= 9):
            raise ConfigError(f"connector '{connector_id}': ident must be 1-9 characters, got {len(ident)!r}")
        irc.append(
            IrcConnectorConfig(
                id=connector_id,
                label=entry.get("label", connector_id),
                host=host,
                port=int(port),
                use_tls=_as_bool(_resolve(entry, section="irc", index=index, field="use_tls"), default=True),
                nick=nick,
                nickserv_password=_resolve(entry, section="irc", index=index, field="nickserv_password"),
                default_channel_modes=_resolve(entry, section="irc", index=index, field="default_channel_modes"),
                ident=ident,
                oper_account=_resolve(entry, section="irc", index=index, field="oper_account"),
                oper_password=_resolve(entry, section="irc", index=index, field="oper_password"),
                client_cert_file=_resolve(entry, section="irc", index=index, field="client_cert_file"),
                client_key_file=_resolve(entry, section="irc", index=index, field="client_key_file"),
                enable_local_user_masquerade=_as_bool(
                    _resolve(entry, section="irc", index=index, field="enable_local_user_masquerade"),
                    default=True,
                ),
            )
        )

    all_ids = [c.id for c in (*discord, *stoat, *irc)]
    seen: set[str] = set()
    for connector_id in all_ids:
        if connector_id in seen:
            raise ConfigError(f"duplicate connector id '{connector_id}' - every connector id must be unique")
        seen.add(connector_id)

    mongo_raw = raw.get("mongo", {})
    mongo = MongoConfig(
        uri=_resolve_env(mongo_raw.get("uri_env", "MONGODB_URI")) or "mongodb://localhost:27017",
        db_name=mongo_raw.get("db_name", "stoat_discord_bridge"),
    )

    return BridgeConfig(discord=discord, stoat=stoat, irc=irc, mongo=mongo)
