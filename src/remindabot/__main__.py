"""Entry point of the bot:  `python -m remindabot --help`

Two kinds of use:

* Daemon (no flags)   - what the container runs: `Bot.run()` (bot.py), one pass per day, forever,
                        plus the small endpoint for the chat commands.
* One-off commands    - `--check`, `--join-boards`, `--once` (optionally with `--dry-run`),
                        `--register-commands`. Meant for the person setting the bot up; see docs/setup.md.

This file only reads the command line and the environment and calls the right method of `Bot`.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

from . import render
from .bot import Bot
from .config import Config, ConfigError, env_or_file

log = logging.getLogger("reminder")


def main(argv=None) -> int:
    """Parse the command line and run. Returns the process exit code:
    0 = ok, 1 = a run failed, 2 = the configuration or message template is invalid."""
    parser = argparse.ArgumentParser(prog="remindabot", description="Boards due-date reminder bot")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run a single pass now and exit")
    mode.add_argument("--check", action="store_true", help="validate config, template and token, then exit")
    mode.add_argument("--join-boards", action="store_true",
                      help="join every relevant OPEN board once (so its cards can be read), then exit")
    mode.add_argument("--register-commands", action="store_true",
                      help="create the /remind_* slash commands in every team (needs REMINDER_ADMIN_TOKEN), then exit")
    mode.add_argument("--unregister-commands", action="store_true",
                      help="remove the /remind_* slash commands from every team (needs REMINDER_ADMIN_TOKEN), then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --once: print what would be sent; no messages, no state changes, no joining")
    args = parser.parse_args(argv)
    if args.dry_run and not args.once:
        # Without this, `--dry-run` alone would fall through to the REAL daemon.
        parser.error("--dry-run only makes sense together with --once")

    logging.basicConfig(
        level=os.environ.get("REMINDER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    # As PID 1 in a container the process would otherwise ignore SIGTERM (docker stop would wait
    # 10 seconds and then kill it).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        cfg = Config.from_env()
        render.load_template(cfg.template_file)   # fail at startup, not at 14:00
    except (ConfigError, render.TemplateError) as exc:
        log.error("%s", exc)
        return 2

    bot = Bot(cfg)
    if args.register_commands or args.unregister_commands:
        try:
            admin_token = env_or_file(os.environ, "ADMIN_TOKEN")
        except ConfigError as exc:
            log.error("%s", exc)
            return 2
        if not admin_token:
            log.error("this needs the token of a system administrator in REMINDER_ADMIN_TOKEN "
                      "(or REMINDER_ADMIN_TOKEN_FILE; for this one run only, the bot never keeps it)")
            return 2
        return bot.register_commands(admin_token, remove=args.unregister_commands)
    if args.check:
        return bot.check()
    if args.once or args.join_boards:
        return bot.once(dry_run=args.dry_run, join_only=args.join_boards)
    return bot.run()


if __name__ == "__main__":
    sys.exit(main())
