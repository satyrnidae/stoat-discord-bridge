"""IRC sender/receiver services, split by area of concern.

Instantiated once per configured IRC connector (config.yaml's `irc` list
can have any number of entries).

Submodules:
  client.py     - the `irc.bot.SingleServerIRCBot` subclass + TLS wrapper
  commands.py   - admin DM command parsing/dispatch + the WHOIS-based oper check
  formatting.py - plain-text / line-limit helpers, synthetic message IDs
  sender.py     - `IrcSenderService`: connection lifecycle, channel management, inbound relay
  receiver.py   - `IrcReceiverService`: outbound relay
"""

from stoat_discord_bridge.services.irc_service.client import _IrcClient, _tls_wrap
from stoat_discord_bridge.services.irc_service.receiver import IrcReceiverService
from stoat_discord_bridge.services.irc_service.sender import IrcSenderService

__all__ = ["IrcSenderService", "IrcReceiverService", "_IrcClient", "_tls_wrap"]
