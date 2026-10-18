"""The chat commands (/remind_status, /remind_send, ...), for system administrators only.

    registry.py   `@command(...)` declares a command and `@dialog(...)` a pop-up form's handler
    router.py     CommandRouter: checks who is calling, runs the handler, answers (late, if slow)
    context.py    Ctx: what a handler is given (configuration, state, clients, who asked)
    show.py       commands that only look:      help, status, next, boards, card, history, getmessage
    control.py    commands that act:            send, resend, test, pause, resume
    adjust.py     commands that change things:  setmessage, setsend, setcap, reset, alerts
    markup.py     Markdown helpers (tables, inert text) / cards.py: finding a card from what was typed

The daily pass (runner.py, scheduler.py) knows nothing about chat. These modules only call it
(`run_once` for sending and, as a dry run, for looking), `Overrides` for what an administrator may
change and `Store` for history, so a command can never do anything the daily pass could not.

To add a command: write a function `(ctx) -> str` in one of the three modules and decorate it with
`@command("remind_<name>", summary=...)`. Registering it in Mattermost (`--register-commands`),
/remind_help and the router pick it up from the registry; docs/chat-commands.md must mention it
(a test checks).
"""
# Importing the modules is what registers their commands. The order is the order of /remind_help.
from . import show, control, adjust
from .cards import find_cards
from .registry import Spec, command, commands, dialog
from .router import CommandRouter

HANDLER_MODULES = (show, control, adjust)
SPECS: tuple[Spec, ...] = commands()
SPEC_BY_TRIGGER = {spec.trigger: spec for spec in SPECS}

__all__ = ["CommandRouter", "HANDLER_MODULES", "SPECS", "SPEC_BY_TRIGGER", "Spec", "command", "commands", "dialog", "find_cards"]
