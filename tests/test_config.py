"""config.py tests run with a completely empty os.environ (see the
isolated_env fixture) - the real repo's .env carries live secrets, and this
module's whole point is testing the resolution *priority order* between
env vars and config.yaml, which would be meaningless (or could silently
pass/fail for the wrong reason) if real values leaked in underneath it.
"""

from __future__ import annotations

import os

import pytest

from stoat_discord_bridge.config import ConfigError, load_config


@pytest.fixture
def isolated_env(monkeypatch):
    monkeypatch.setattr(os, "environ", {})


def _write_config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_literal_values_used_when_no_env_override(tmp_path, isolated_env, monkeypatch):
    path = _write_config(
        tmp_path,
        """
        discord:
          - id: discord
            guild_id: 123
            bot_token: literal-token
        """,
    )
    config = load_config(path)
    assert config.discord[0].guild_id == 123
    assert config.discord[0].bot_token == "literal-token"


def test_positional_env_var_overrides_literal_config(tmp_path, isolated_env, monkeypatch):
    monkeypatch.setenv("DISCORD__0__GUILD_ID", "999")
    path = _write_config(
        tmp_path,
        """
        discord:
          - id: discord
            guild_id: 123
            bot_token: literal-token
        """,
    )
    config = load_config(path)
    assert config.discord[0].guild_id == 999  # env var won over the literal 123


def test_index_is_0_based_per_connector_kind(tmp_path, isolated_env, monkeypatch):
    monkeypatch.setenv("STOAT__1__SERVER_ID", "from-env")
    monkeypatch.setenv("STOAT__0__TOKEN", "t0")
    monkeypatch.setenv("STOAT__1__TOKEN", "t1")
    path = _write_config(
        tmp_path,
        """
        stoat:
          - id: stoat_first
            server_id: literal-0
            api_url: https://a.example/api
          - id: stoat_second
            server_id: literal-1
            api_url: https://b.example/api
        """,
    )
    config = load_config(path)
    assert config.stoat[0].server_id == "literal-0"  # no STOAT__0__SERVER_ID set - literal wins
    assert config.stoat[1].server_id == "from-env"  # STOAT__1__SERVER_ID set - env wins
    assert config.stoat[0].bot_token == "t0"
    assert config.stoat[1].bot_token == "t1"


def test_stoat_group_parent_channel_with_threads_defaults_on_and_is_overridable(tmp_path, isolated_env, monkeypatch):
    monkeypatch.setenv("STOAT__1__GROUP_PARENT_CHANNEL_WITH_THREADS", "false")
    path = _write_config(
        tmp_path,
        """
        stoat:
          - id: stoat_a
            server_id: s1
            api_url: https://a.example/api
            bot_token: t0
          - id: stoat_b
            server_id: s2
            api_url: https://b.example/api
            bot_token: t1
        """,
    )
    config = load_config(path)
    assert config.stoat[0].group_parent_channel_with_threads is True  # default
    assert config.stoat[1].group_parent_channel_with_threads is False  # env override


def test_missing_required_field_raises(tmp_path, isolated_env):
    path = _write_config(
        tmp_path,
        """
        discord:
          - id: discord
            guild_id: 123
        """,
    )
    with pytest.raises(ConfigError, match="'token' is required"):
        load_config(path)


def test_duplicate_connector_id_across_kinds_raises(tmp_path, isolated_env, monkeypatch):
    monkeypatch.setenv("DISCORD__0__TOKEN", "t")
    monkeypatch.setenv("STOAT__0__TOKEN", "t")
    path = _write_config(
        tmp_path,
        """
        discord:
          - id: shared
            guild_id: 123
        stoat:
          - id: shared
            server_id: s1
            api_url: https://a.example/api
        """,
    )
    with pytest.raises(ConfigError, match="duplicate connector id"):
        load_config(path)


def test_missing_config_file_raises(tmp_path, isolated_env):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nonexistent.yaml")


def test_irc_defaults(tmp_path, isolated_env, monkeypatch):
    path = _write_config(
        tmp_path,
        """
        irc:
          - id: irc
            host: irc.example.net
        """,
    )
    config = load_config(path)
    irc = config.irc[0]
    assert irc.port == 6697
    assert irc.use_tls is True
    assert irc.nick == "StoatDiscordBridge"
    assert irc.nickserv_password is None
    assert irc.ident is None


@pytest.mark.parametrize("raw_value,expected", [("true", True), ("false", False), ("1", True), ("0", False)])
def test_irc_use_tls_env_var_coercion(tmp_path, isolated_env, monkeypatch, raw_value, expected):
    monkeypatch.setenv("IRC__0__USE_TLS", raw_value)
    path = _write_config(tmp_path, "irc:\n  - id: irc\n    host: irc.example.net\n")
    config = load_config(path)
    assert config.irc[0].use_tls is expected


def test_irc_ident_too_long_via_env_var(tmp_path, isolated_env, monkeypatch):
    monkeypatch.setenv("IRC__0__IDENT", "toolongforanidentxx")
    path = _write_config(tmp_path, "irc:\n  - id: irc\n    host: irc.example.net\n")
    with pytest.raises(ConfigError, match="1-9 characters"):
        load_config(path)


def test_irc_ident_empty_via_literal_config(tmp_path, isolated_env, monkeypatch):
    # an empty env var is treated as "not set" by _resolve() (falsy check),
    # so an empty ident can only actually reach validation via a literal
    # config.yaml value - this exercises that path specifically.
    path = _write_config(tmp_path, 'irc:\n  - id: irc\n    host: irc.example.net\n    ident: ""\n')
    with pytest.raises(ConfigError, match="1-9 characters"):
        load_config(path)


def test_irc_valid_ident_accepted(tmp_path, isolated_env, monkeypatch):
    monkeypatch.setenv("IRC__0__IDENT", "bridge")
    path = _write_config(tmp_path, "irc:\n  - id: irc\n    host: irc.example.net\n")
    config = load_config(path)
    assert config.irc[0].ident == "bridge"


# ---------------------------------------------------------------- *_FILE secrets


def test_file_env_var_reads_the_secret_from_disk(tmp_path, isolated_env, monkeypatch):
    secret_file = tmp_path / "token"
    secret_file.write_text("from-file-token\n", encoding="utf-8")
    monkeypatch.setenv("DISCORD__0__TOKEN_FILE", str(secret_file))
    path = _write_config(tmp_path, "discord:\n  - id: discord\n    guild_id: 123\n")

    config = load_config(path)

    assert config.discord[0].bot_token == "from-file-token"  # trailing newline stripped


def test_file_env_var_overrides_literal_config(tmp_path, isolated_env, monkeypatch):
    secret_file = tmp_path / "token"
    secret_file.write_text("from-file-token", encoding="utf-8")
    monkeypatch.setenv("DISCORD__0__TOKEN_FILE", str(secret_file))
    path = _write_config(
        tmp_path,
        """
        discord:
          - id: discord
            guild_id: 123
            bot_token: literal-token
        """,
    )

    config = load_config(path)

    assert config.discord[0].bot_token == "from-file-token"


def test_plain_env_var_overrides_the_file_variant(tmp_path, isolated_env, monkeypatch):
    # the plain env var still takes priority when only it is set - _FILE is
    # an alternative source, not a higher-priority one.
    secret_file = tmp_path / "token"
    secret_file.write_text("from-file-token", encoding="utf-8")
    monkeypatch.setenv("DISCORD__0__TOKEN", "from-env-token")
    path = _write_config(tmp_path, "discord:\n  - id: discord\n    guild_id: 123\n")

    config = load_config(path)

    assert config.discord[0].bot_token == "from-env-token"


def test_setting_both_plain_and_file_env_vars_raises(tmp_path, isolated_env, monkeypatch):
    secret_file = tmp_path / "token"
    secret_file.write_text("from-file-token", encoding="utf-8")
    monkeypatch.setenv("DISCORD__0__TOKEN", "from-env-token")
    monkeypatch.setenv("DISCORD__0__TOKEN_FILE", str(secret_file))
    path = _write_config(tmp_path, "discord:\n  - id: discord\n    guild_id: 123\n")

    with pytest.raises(ConfigError, match="both 'DISCORD__0__TOKEN' and 'DISCORD__0__TOKEN_FILE' are set"):
        load_config(path)


def test_file_env_var_pointing_at_a_missing_file_raises(tmp_path, isolated_env, monkeypatch):
    monkeypatch.setenv("DISCORD__0__TOKEN_FILE", str(tmp_path / "does-not-exist"))
    path = _write_config(tmp_path, "discord:\n  - id: discord\n    guild_id: 123\n")

    with pytest.raises(ConfigError, match="couldn't read secret file"):
        load_config(path)


def test_mongo_uri_resolved_from_a_file(tmp_path, isolated_env, monkeypatch):
    secret_file = tmp_path / "mongo-uri"
    secret_file.write_text("mongodb://from-file/db\n", encoding="utf-8")
    monkeypatch.setenv("MONGO__URI_FILE", str(secret_file))
    path = _write_config(
        tmp_path,
        """
        discord:
          - id: discord
            guild_id: 123
            bot_token: t
        mongo:
          uri_env: MONGO__URI
        """,
    )

    config = load_config(path)

    assert config.mongo.uri == "mongodb://from-file/db"


def test_legacy_env_pointer_field_is_ignored(tmp_path, isolated_env, monkeypatch):
    """*_env pointer fields (e.g. nick_env) were removed - config.yaml
    setting one should just be a plain unused key, not resolve anything."""
    monkeypatch.setenv("SOME_OTHER_VAR", "should-not-be-picked-up")
    path = _write_config(
        tmp_path,
        """
        irc:
          - id: irc
            host: irc.example.net
            nick_env: SOME_OTHER_VAR
        """,
    )
    config = load_config(path)
    assert config.irc[0].nick == "StoatDiscordBridge"  # nick_env is inert - falls through to the default
