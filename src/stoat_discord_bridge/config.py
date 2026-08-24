"""Static and environment-derived configuration for the bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class DiscordConfig:
    bot_token: str
    guild_id: int


@dataclass(frozen=True)
class StoatServerConfig:
    server_id: str
    api_url: str
    bot_token: str


@dataclass(frozen=True)
class IrcConfig:
    host: str
    port: int
    use_tls: bool
    nick: str
    nickserv_password: str | None


@dataclass(frozen=True)
class MongoConfig:
    uri: str
    db_name: str


@dataclass(frozen=True)
class BridgeConfig:
    discord: DiscordConfig
    stoat_public: StoatServerConfig
    stoat_selfhosted: StoatServerConfig
    irc: IrcConfig
    mongo: MongoConfig


def load_config() -> BridgeConfig:
    return BridgeConfig(
        discord=DiscordConfig(
            bot_token=os.environ["DISCORD_BOT_TOKEN"],
            guild_id=int(os.environ["DISCORD_GUILD_ID"]),
        ),
        # api_url TODO: confirm each instance's actual API base URL (public vs.
        # self-hosted deployments can differ) before first run; override via env.
        stoat_public=StoatServerConfig(
            server_id=os.environ["STOAT_PUBLIC_SERVER_ID"],
            api_url=os.environ.get("STOAT_PUBLIC_API_URL", "https://app.stoat.chat/api"),
            bot_token=os.environ["STOAT_PUBLIC_BOT_TOKEN"],
        ),
        stoat_selfhosted=StoatServerConfig(
            server_id=os.environ["STOAT_SELFHOSTED_SERVER_ID"],
            api_url=os.environ.get("STOAT_SELFHOSTED_API_URL", "https://srv.satyrn.dev/api"),
            bot_token=os.environ["STOAT_SELFHOSTED_BOT_TOKEN"],
        ),
        irc=IrcConfig(
            host="irc.satyrn.dev",
            port=6697,
            use_tls=True,
            nick=os.environ.get("IRC_NICK", "StoatDiscordBridge"),
            nickserv_password=os.environ.get("IRC_NICKSERV_PASSWORD") or None,
        ),
        mongo=MongoConfig(
            uri=os.environ.get("MONGODB_URI", "mongodb://localhost:27017"),
            db_name=os.environ.get("MONGODB_DB_NAME", "stoat_discord_bridge"),
        ),
    )
