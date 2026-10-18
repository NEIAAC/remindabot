"""The registry of chat commands: each command is declared where it is written.

    @command("remind_status", summary="Settings, what was sent today ...", slow=True)
    def status(ctx): ...

It is the same idea as `@listen_to` / `@listen_webhook` in other Mattermost bots (and the command
table of a Mattermost plugin): the handler and what Mattermost needs to know about it (the trigger,
the hint shown while typing, the one-line description) sit together. So a command cannot be written
and left out of `--register-commands` (register.py reads the registry) or out of `/remind_help`
(which does too).

A pop-up form has the same kind of handler for its submission: `@dialog("setmessage", ...)`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Union

if TYPE_CHECKING:     # only for the type hints: context.py itself needs nothing from here
    from .context import Ctx, DialogCtx

# What a command handler returns: the text of the reply (shown to the author only), or the complete
# JSON answer for Mattermost (the form opened by /remind_setmessage answers with `{}`).
Reply = Union[str, dict]


@dataclass(frozen=True)
class Spec:
    trigger: str                          # "remind_status": what is typed after the slash
    hint: str                             # the arguments, for the help text and the autocomplete hint
    summary: str                          # one line, for /remind_help and the command's description in Mattermost
    slow: bool                            # reads many boards: answer "working..." now, the result later
    handler: Callable[["Ctx"], Reply]


@dataclass(frozen=True)
class DialogSpec:
    callback_id: str
    again: str                            # what to type to get the form back (shown when it has expired)
    handler: Callable[["DialogCtx"], dict]


_commands: list[Spec] = []
_dialogs: dict[str, DialogSpec] = {}


def command(trigger: str, *, summary: str, hint: str = "", slow: bool = False):
    """Register a function as the handler of `/<trigger>`."""
    def register(handler: Callable[["Ctx"], Reply]):
        if any(spec.trigger == trigger for spec in _commands):
            raise ValueError(f"two handlers for /{trigger}")
        _commands.append(Spec(trigger, hint, summary, slow, handler))
        return handler
    return register


def dialog(callback_id: str, *, again: str):
    """Register a function as the handler of the submission of the pop-up form `callback_id`."""
    def register(handler: Callable[["DialogCtx"], dict]):
        if callback_id in _dialogs:
            raise ValueError(f"two handlers for the form {callback_id!r}")
        _dialogs[callback_id] = DialogSpec(callback_id, again, handler)
        return handler
    return register


def commands() -> tuple[Spec, ...]:
    """Every registered command, in the order they were declared (that is the order of /remind_help)."""
    return tuple(_commands)


def find_command(trigger: str) -> Optional[Spec]:
    return next((spec for spec in _commands if spec.trigger == trigger), None)


def find_dialog(callback_id: object) -> Optional[DialogSpec]:
    return _dialogs.get(callback_id) if isinstance(callback_id, str) else None
