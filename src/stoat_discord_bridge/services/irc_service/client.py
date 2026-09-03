"""The `irc` library client subclass for the IRC connector.

The irc library dispatches events by looking up `on_<event>` methods on the
bot instance, so *something* has to subclass `SingleServerIRCBot`. This
module keeps that subclass (and the TLS socket wrapper it needs) apart from
the service logic it delegates to.
"""

from __future__ import annotations

import functools
import ssl
from typing import TYPE_CHECKING

import irc.bot
import irc.connection

from stoat_discord_bridge.config import IrcConnectorConfig

if TYPE_CHECKING:
    from stoat_discord_bridge.services.irc_service.sender import IrcSenderService


def _tls_wrap(sock, *, server_hostname: str, client_cert_file: str | None, client_key_file: str | None):
    # ssl.SSLContext.wrap_socket() defaults to check_hostname=True, which
    # raises ValueError unless server_hostname is supplied.
    context = ssl.create_default_context()
    if client_cert_file:
        # client_key_file may be None if client_cert_file is a combined
        # cert+key PEM - load_cert_chain accepts that.
        context.load_cert_chain(certfile=client_cert_file, keyfile=client_key_file)
    return context.wrap_socket(sock, server_hostname=server_hostname)


class _IrcClient(irc.bot.SingleServerIRCBot):
    """The irc library dispatches events by looking up `on_<event>` methods on
    the bot instance itself, so *something* has to subclass SingleServerIRCBot.
    This subclass exists only to satisfy that and delegates every callback to
    the owning IrcSenderService, which otherwise doesn't need to inherit from
    a third-party client class."""

    def __init__(self, owner: "IrcSenderService", config: IrcConnectorConfig) -> None:
        connect_factory = (
            irc.connection.Factory(
                wrapper=functools.partial(
                    _tls_wrap,
                    server_hostname=config.host,
                    client_cert_file=config.client_cert_file,
                    client_key_file=config.client_key_file,
                )
            )
            if config.use_tls
            else irc.connection.Factory()
        )
        # `username` (the ident/USER-command field) is forwarded straight
        # through to ServerConnection.connect() the same way connect_factory
        # is, per SingleServerIRCBot's **connect_params passthrough - falls
        # back to its own default (the nickname) if we don't set it.
        #
        # Verified against irc 20.5.0: SingleServerIRCBot.__init__ collects
        # every kwarg past (server_list, nickname, realname) into
        # **connect_params, and _connect() splats those into
        # self.connect(host, port, nickname, password, ircname=realname,
        # **connect_params) - i.e. straight into ServerConnection.connect(),
        # which takes `username` (and `connect_factory`) as real named
        # parameters and sends `USER <username> 0 * :<ircname>`. `username`
        # defaults to the nickname there when unset, so omitting the kwarg
        # (no `ident` configured) is the documented no-op. `realname` is the
        # bot's own named param and reaches the same USER line as `ircname`.
        username_kwarg = {"username": config.ident} if config.ident else {}
        super().__init__(
            server_list=[(config.host, config.port)],
            nickname=config.nick,
            realname=config.nick,
            connect_factory=connect_factory,
            **username_kwarg,
        )
        self._owner = owner

    def on_welcome(self, connection, event) -> None:
        self._owner._handle_welcome(connection)

    def on_disconnect(self, connection, event) -> None:
        self._owner._handle_disconnect()

    def on_privmsg(self, connection, event) -> None:
        self._owner._handle_privmsg(connection, event)

    def on_pubmsg(self, connection, event) -> None:
        self._owner._handle_pubmsg(event)

    def on_pubnotice(self, connection, event) -> None:
        self._owner._handle_pubnotice(event)

    def on_privnotice(self, connection, event) -> None:
        self._owner._handle_privnotice(event)

    def on_join(self, connection, event) -> None:
        self._owner._handle_join(connection, event)

    def on_nochanmodes(self, connection, event) -> None:
        # Numeric 477. RFC2812 defines this as ERR_NOCHANMODES ("channel
        # doesn't support modes"), but InspIRCd's services module (this
        # bridge's target network - see the history-replay heuristic above)
        # repurposes it as ERR_NEEDREGGEDNICK: "you need to be identified to
        # a registered account to join this channel" - the failure mode this
        # handler exists for. A network that sends 477 for its RFC meaning
        # instead would just cause a harmless spurious rejoin attempt here.
        self._owner._handle_join_blocked(event)

    def on_youreoper(self, connection, event) -> None:
        # Numeric 381 (RPL_YOUREOPER) - the server confirming the OPER
        # command _handle_welcome sent succeeded.
        self._owner._handle_youreoper()

    def on_whoisoperator(self, connection, event) -> None:
        # Fired only if the WHOIS target IS an oper - always arrives before
        # on_endofwhois for the same WHOIS exchange.
        self._owner._resolve_whois(event.arguments[0], True)

    def on_endofwhois(self, connection, event) -> None:
        # Always fires at the end of a WHOIS exchange, oper or not. If
        # on_whoisoperator already resolved this nick's future to True,
        # _resolve_whois is a no-op here - see its docstring.
        self._owner._resolve_whois(event.arguments[0], False)

    def on_nosuchnick(self, connection, event) -> None:
        # WHOIS target doesn't exist (e.g. they disconnected between sending
        # the DM and this WHOIS resolving) - treat as "not oper".
        self._owner._resolve_whois(event.arguments[0], False)
