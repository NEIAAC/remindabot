"""One-time setup of the slash commands in Mattermost:  python -m remindabot --register-commands
(and `--unregister-commands` to take them away again)

Mattermost only knows a slash command once somebody with the right to manage them creates it, and
a command belongs to ONE team. This creates (or updates) every command of commands.SPECS in every
team and remembers the secret token Mattermost generated for each, in the bot's state file, so the
bot can tell a genuine call from a forged one. Run it again whenever a team is added or the commands
change; it is safe to repeat.

It needs the token of a system administrator for this single run (REMINDER_ADMIN_TOKEN). The bot
itself never holds that: it works with the token of its own bot account.
"""
from __future__ import annotations

import logging
import urllib.parse

from .commands import SPECS
from .config import Config
from .driver import ApiError, Client
from .overrides import Overrides
from .store import Store

log = logging.getLogger("reminder")


def register_commands(cfg: Config, admin_token: str, store: Store) -> tuple[list[str], bool]:
    """Create/update the commands in every team. Returns (report lines, everything went well)."""
    api = Client(cfg.mm_url, admin_token, prefix="/api/v4")
    report: list[str] = []
    ok = True
    url = cfg.commands_url.rstrip("/") + "/command"

    try:
        me = api.request("GET", "/users/me")
    except ApiError as exc:
        return [f"The administrator token does not work: {exc}"], False
    if "system_admin" not in str(me.get("roles") or "").split():
        return [f"@{me.get('username')} is not a system administrator; use an administrator's token."], False

    teams: list[dict] = []
    page = 0
    while True:
        chunk = api.request("GET", f"/teams?per_page=200&page={page}") or []
        teams += chunk
        if len(chunk) < 200:
            break
        page += 1

    tokens: dict[str, dict[str, str]] = {}
    for team in teams:
        team_id = team["id"]
        existing = {c.get("trigger"): c for c in
                    (api.request("GET", f"/commands?team_id={team_id}&custom_only=true") or [])}
        tokens[team_id] = {}
        created = updated = 0
        for spec in SPECS:
            desired = {
                "team_id": team_id, "trigger": spec.trigger, "method": "P", "url": url,
                "display_name": f"Remindabot: {spec.trigger}", "description": spec.summary,
                # only /remind_help is offered while typing: the others are for administrators who read it
                "auto_complete": spec.trigger == "remind_help",
                "auto_complete_desc": spec.summary, "auto_complete_hint": spec.hint,
            }
            try:
                current = existing.get(spec.trigger)
                if current is None:
                    command = api.request("POST", "/commands", desired, idempotent=False)
                    created += 1
                else:
                    command = current
                    if any(current.get(k) != v for k, v in desired.items()):
                        command = api.request("PUT", f"/commands/{current['id']}", {**current, **desired},
                                              idempotent=False)
                        updated += 1
                tokens[team_id][spec.trigger] = command["token"]
            except (ApiError, KeyError) as exc:
                ok = False
                report.append(f"team {team.get('name')}: /{spec.trigger} failed: {exc}")
        report.append(f"team {team.get('name')!r}: {created} command(s) created, {updated} updated, "
                      f"{len(SPECS) - created - updated} already right")

    Overrides(store).set_command_tokens(tokens)
    report.append(f"{sum(len(t) for t in tokens.values())} token(s) saved in {cfg.state_file}")

    # The one setting that makes it work or not, and that this tool cannot change when the server's
    # configuration is managed by environment variables: say so instead of leaving a mystery.
    try:
        config = api.request("GET", "/config")
        service = (config or {}).get("ServiceSettings", {})
        host = urllib.parse.urlsplit(cfg.commands_url).hostname or ""
        allowed = str(service.get("AllowedUntrustedInternalConnections") or "").split()
        if service.get("EnableCommands") is False:
            ok = False
            report.append("WARNING: custom slash commands are switched off in Mattermost "
                          "(System Console > Integrations > Integration Management > Enable Custom Slash Commands).")
        if host and host not in allowed:
            ok = False
            report.append(f"WARNING: Mattermost may not call {host!r}. Add it to 'Allow untrusted internal "
                          "connections' (System Console > Environment > Developer, or the environment variable "
                          f"MM_SERVICESETTINGS_ALLOWEDUNTRUSTEDINTERNALCONNECTIONS={host}) and restart "
                          "Mattermost; until then every command answers 'failed'.")
    except ApiError as exc:
        report.append(f"(could not read the Mattermost configuration to check the allowed connections: {exc})")
    return report, ok


def unregister_commands(cfg: Config, admin_token: str, store: Store) -> tuple[list[str], bool]:
    """Delete this bot's commands from every team (and forget their tokens).

    Used to leave a Mattermost clean when the bot is taken away. A command whose bot is gone is
    worse than no command: it answers "Command with a trigger of ... failed" instead of "unknown".
    Only commands with this bot's triggers are touched.
    """
    api = Client(cfg.mm_url, admin_token, prefix="/api/v4")
    report: list[str] = []
    try:
        me = api.request("GET", "/users/me")
    except ApiError as exc:
        return [f"The administrator token does not work: {exc}"], False
    if "system_admin" not in str(me.get("roles") or "").split():
        return [f"@{me.get('username')} is not a system administrator; use an administrator's token."], False
    triggers = {spec.trigger for spec in SPECS}
    ok = True
    removed = 0
    page = 0
    teams: list[dict] = []
    while True:
        chunk = api.request("GET", f"/teams?per_page=200&page={page}") or []
        teams += chunk
        if len(chunk) < 200:
            break
        page += 1
    for team in teams:
        for command in api.request("GET", f"/commands?team_id={team['id']}&custom_only=true") or []:
            if command.get("trigger") not in triggers:
                continue
            try:
                api.request("DELETE", f"/commands/{command['id']}", idempotent=False)
                removed += 1
            except ApiError as exc:
                ok = False
                report.append(f"team {team.get('name')}: could not remove /{command.get('trigger')}: {exc}")
    Overrides(store).set_command_tokens({})
    report.append(f"{removed} command(s) removed from {len(teams)} team(s)")
    return report, ok
