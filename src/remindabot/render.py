"""The message: load the editable template, fill it in, and make user-typed text safe.

* `load_template` reads message.txt, drops the editors' comment header and checks every `$variable`.
* `render` fills the variables in for one recipient.
* `sanitize_inline` neutralizes text typed by users (card titles, names) before it is put in a
  message that comes from the bot.
* `format_long_pt` / `days_text` produce the Portuguese date and "N days left" phrases.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path
from string import Template

# Everything above this line in message.txt is a comment for editors and is never sent.
CUT_MARKER = "-----8<-----"

# The variables a message template may use ($name or ${name}). Add one here, then fill it in where
# the message is built (runner._remind_card). docs/configuration.md lists them for people who
# edit the message.
PLACEHOLDERS = (
    "responsible_name",   # who receives the message
    "co_responsibles",    # the OTHER people notified about this card (empty when there are none)
    "card_title",
    "board_title",
    "due_date",           # 25/10/2026
    "due_date_long",      # domingo, 25 de outubro de 2026
    "days_left",          # 12
    "days_left_text",     # whole phrase: "faltam 12 dias", "falta 1 dia" or "é hoje"
    "remind_before",      # the reminder behind this message, in days: 7 = the "7 days before" one
    "card_url",
)

# Portuguese names, spelled out here so the output never depends on the machine's locale settings.
_MONTHS = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
           "agosto", "setembro", "outubro", "novembro", "dezembro")
_WEEKDAYS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
             "sexta-feira", "sábado", "domingo")


class TemplateError(Exception):
    """The message template is missing, empty, or uses a variable that does not exist."""


def format_long_pt(day: date) -> str:
    """'domingo, 25 de outubro de 2026'."""
    return f"{_WEEKDAYS[day.weekday()]}, {day.day} de {_MONTHS[day.month - 1]} de {day.year}"


def days_text(days: int) -> str:
    """Whole phrase, because the verb changes with the number: faltam 12 dias / falta 1 dia / é hoje."""
    if days == 0:
        return "é hoje"
    return "falta 1 dia" if days == 1 else f"faltam {days} dias"


def template_body(text: str) -> str:
    """The part of a template file that is the message: everything below the cut marker, if there
    is one, with surrounding blank lines removed. (Above the marker is a note for whoever edits.)"""
    if CUT_MARKER in text:
        text = text.split(CUT_MARKER, 1)[1]
    return text.strip("\n")


def parse_template(text: str, source: str = "the message") -> Template:
    """Validate message text and turn it into a template. Raises TemplateError with a readable reason.

    Validating here (every variable must exist) means a typo such as $card_titel is caught when the
    bot starts, when an administrator saves a new message, and on every pass, instead of producing
    a broken message.
    """
    body = template_body(text)
    if not body.strip():
        raise TemplateError(f"{source} is empty")
    template = Template(body)
    try:  # fail early and loudly on typos such as $card_titel
        template.substitute({name: "x" for name in PLACEHOLDERS})
    except (KeyError, ValueError) as exc:
        raise TemplateError(
            f"{source} has a problem with {exc}. Valid placeholders: "
            + ", ".join("$" + n for n in PLACEHOLDERS)
            + ". Use $$ for a literal dollar sign."
        ) from exc
    return template


def load_template(path: Path) -> Template:
    """Read and validate the message template file. Raises TemplateError with a readable reason."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateError(f"cannot read template {path}: {exc}") from exc
    return parse_template(text, f"template {path}")


def render(template: Template, values: dict[str, str]) -> str:
    """Fill in one message. `values` must contain every name in PLACEHOLDERS.
    Values are inserted as plain text: a `$` inside a card title is never interpreted."""
    return template.substitute(values)


_ZWSP = "\u200b"  # zero-width space: invisible, but breaks syntax that depends on adjacency
# Exactly the characters Mattermost's Markdown renderer (the mattermost/marked fork) accepts after a
# backslash. Escaping anything else would show a literal backslash (e.g. `\<`).
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()>#+\-!|])")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")


def sanitize_inline(text: object, max_len: int = 200, fallback: str = "") -> str:
    """Make user-controlled text (card titles, board titles, display names) safe to drop into a message.

    These strings are typed by any board member, but the message comes from the bot and carries its
    authority, so they must not be able to inject formatting, links, mentions or extra lines:
      * control characters / newlines become single spaces, invisible "format" characters
        (including the right-to-left overrides that could visually reverse the rest of the
        sentence) are removed, and the length is capped;
      * Markdown syntax is backslash-escaped, so `[click](http://x)` shows as plain text;
      * `@name` / `~channel` mentions and `scheme://` / `www.` autolinks are broken with an
        invisible character, so nothing becomes clickable or pings anyone.
    """
    value = _CONTROL.sub(" ", str(text if text is not None else ""))
    # Unicode category "Cf" = invisible formatting characters (bidi overrides, zero-width joiners...).
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Cf")
    value = re.sub(r" {2,}", " ", value).strip()
    if not value:
        return fallback
    if len(value) > max_len:
        value = value[: max_len - 1].rstrip() + "…"
    value = _MD_SPECIAL.sub(r"\\\1", value)
    value = value.replace("@", "@" + _ZWSP).replace("~", "~" + _ZWSP)
    # Autolinkers can start in the middle of a word ("xhttp://..." or "ãwww.x"), so no word-boundary
    # logic here: every "://" and every "www." is broken, whatever surrounds it.
    value = value.replace("://", ":" + _ZWSP + "//")
    value = re.sub(r"(?i)www\.", lambda m: m.group(0)[:3] + _ZWSP + ".", value)
    return value
