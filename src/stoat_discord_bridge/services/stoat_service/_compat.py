"""Runtime patches for stoat.py bugs the bridge trips over.

stoat.py 1.2.1's `stoat.ext.commands` framework copies discord.py's
`Command.transform` / `Command.signature` but replaces discord.py's safe
identity checks (`converter is discord.Attachment`) with
`issubclass(converter, stoat.StatelessAsset)` / `issubclass(annotation,
stoat.Asset)`. `issubclass()` raises `TypeError: issubclass() arg 1 must be
a class` the moment a parameter is annotated with anything that isn't a bare
class - `typing.Optional[str]`, any `Union`, `Literal`, ... - so *every*
command that takes an optional argument blows up during argument parsing
(and again in the error handler, which reads `Command.signature`). That's
most of the bridge's `/link` / `/unlink` / `/linked` / `/mirror` tree
(issue #40).

`apply_stoat_command_patches()` shadows the `issubclass` global inside
`stoat.ext.commands.core` with a variant that returns `False` for a
non-class first argument instead of raising, matching how discord.py's
equivalent code behaves. Idempotent; called at import of this package's
command module.
"""

from __future__ import annotations

import inspect

from stoat.ext.commands import core as _stoat_commands_core

_PATCH_FLAG = "_sdb_safe_issubclass_installed"


def apply_stoat_command_patches() -> None:
    """Make `stoat.ext.commands.core`'s `issubclass(...)` calls tolerate a
    non-class first argument (an `Optional[...]` / `Union[...]` annotation)
    rather than raising `TypeError`. Safe to call more than once."""
    if getattr(_stoat_commands_core, _PATCH_FLAG, False):
        return

    _real_issubclass = _stoat_commands_core.__dict__.get("issubclass", issubclass)

    def _safe_issubclass(cls: object, classinfo: object) -> bool:
        if not inspect.isclass(cls):
            return False
        return _real_issubclass(cls, classinfo)  # type: ignore[arg-type]

    _stoat_commands_core.issubclass = _safe_issubclass  # type: ignore[attr-defined]
    setattr(_stoat_commands_core, _PATCH_FLAG, True)
