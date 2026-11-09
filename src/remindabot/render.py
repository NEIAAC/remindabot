"""The message: load the editable template, fill it in, and make user-typed text safe.

* `load_template` reads message.txt, drops the editors' comment header and checks every `$variable`.
* `render` fills the variables in for one recipient.
* `sanitize_inline` neutralizes text typed by users (card titles, names) before it is put in a
  message that comes from the bot.
* `date_values` gives the template a date (the due date, the last reminder's day) as NUMBERS.
* `render` also subtracts numbers written like `$days_left-$last_remind_days_left`; `\\-` between two
  variables is a plain hyphen instead (see `_subtract`).

The code puts no words of its own in a reminder: every word of the message is in the template, so
changing the language is editing a file. That is why a date is handed over as numbers (the template
arranges them as it likes: 2026-10-25, 25/10/2026, ...) and why there is no "N days left" phrase (the
template writes "$days_left days left"; "1 days left" is accepted).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from functools import reduce
from pathlib import Path
from string import Template
from typing import Optional

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
    "due_date",           # 2026-10-25
    "due_year",           # 2026
    "due_month",          # 10
    "due_day",            # 25
    "last_remind_date",   # the day of the card's LAST reminder (due date minus its smallest "Remind Before"): 2026-10-15
    "last_remind_year",   # 2026
    "last_remind_month",  # 10
    "last_remind_day",    # 15
    "days_left",          # 12
    "remind_before",      # the reminder behind this message, in days: 7 = the "7 days before" one
    "last_remind_days_left",  # the card's LAST reminder, in days: the smallest "Remind Before" number, e.g. 10
    "card_url",
)

# What stands in for a name or a title that is missing: a symbol, because the code has no words.
UNKNOWN = "\u2014"


class TemplateError(Exception):
    """The message template is missing, empty, or uses a variable that does not exist."""


def date_values(day: Optional[date], prefix: str = "due") -> dict[str, str]:
    """A date as numbers only, for the template to arrange: 2026-10-25, 2026, 10, 25.

    The names start with `prefix`: due_date, due_year, due_month, due_day, or last_remind_*. A date that
    does not exist (None) is the dash in all four.
    """
    parts = ((UNKNOWN,) * 4 if day is None
             else (day.isoformat(), f"{day.year:04d}", f"{day.month:02d}", f"{day.day:02d}"))
    return dict(zip((f"{prefix}_{part}" for part in ("date", "year", "month", "day")), parts))


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


# Variables joined by "-" in a template ($days_left-$last_remind_days_left, $x or ${x}) are subtracted
# when every value is a whole number. A joint written "\-" is a plain hyphen instead, and ends the
# subtraction before it ($due_day\-$due_month\-$due_year is the text 25-10-2026). `$$` is matched too,
# so that an escaped dollar is never taken for the start of a chain.
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_VARIABLE = r"\$(?:\{" + _NAME + r"\}|" + _NAME + ")"
_CHAINS = re.compile(r"\$\$|" + _VARIABLE + r"(?:\\?-" + _VARIABLE + ")+")
_JOINTS = re.compile(r"(\\-|-)")   # what splits a chain into variables (neither can be in a name)
_WHOLE = re.compile(r"[0-9]{1,18}")  # plain digits only: no sign, spaces, "1_0", "1e3" or other scripts' digits


def _number(variable: str, values: dict[str, str]) -> Optional[int]:
    """The value of a `$name` / `${name}` as a whole number, or None when it is anything else."""
    name = variable[2:-1] if variable.startswith("${") else variable[1:]
    value = values.get(name, "")
    return int(value) if _WHOLE.fullmatch(value) else None


def _subtract(found: "re.Match[str]", values: dict[str, str], sums: dict[str, str]) -> str:
    """What to write for one chain `_CHAINS` found, as template text.

    Variables joined by `-` are subtracted left to right when every value is a whole number, and are
    left exactly as they were otherwise (they are then shown as text, `Ana-12`). A `\\-` joint ends
    the subtraction and becomes a plain `-`: `$a-$b\\-$c` is (a minus b), a hyphen, then c.

    A result is not written into the text but kept in `sums` and referred to as `${_sum0}`: written as
    digits, it could run into the variable before it (`$card_title` and `2` would read `$card_title2`).
    """
    text = found.group(0)
    if text == "$$":
        return text
    parts = _JOINTS.split(text)                       # variable, joint, variable, joint, ...
    groups = [[parts[0]]]
    for joint, variable in zip(parts[1::2], parts[2::2]):
        if joint == "-":
            groups[-1].append(variable)
        else:
            groups.append([variable])
    shown = []
    for group in groups:
        numbers = [_number(variable, values) for variable in group]
        if len(group) > 1 and None not in numbers:
            name = f"_sum{len(sums)}"
            sums[name] = str(reduce(lambda left, right: left - right, numbers))
            shown.append("${" + name + "}")
        else:
            shown.append("-".join(group))
    return "-".join(shown)


def render(template: Template, values: dict[str, str]) -> str:
    """Fill in one message. `values` must contain every name in PLACEHOLDERS.

    Variables joined by `-` are subtracted first ($days_left-$last_remind_days_left is 2 when 12 days
    are left and the last reminder is 10 days before the due date). That works on the TEMPLATE, never on
    the values, and the values are inserted as plain text: a `$` inside a card title is never interpreted.
    """
    sums: dict[str, str] = {}
    text = _CHAINS.sub(lambda found: _subtract(found, values, sums), template.template)
    return Template(text).substitute(values | sums)


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
