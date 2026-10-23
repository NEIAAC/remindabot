# remindabot – due-date reminders for Mattermost Boards

A small bot for **Mattermost Boards**. A few days before a card's **Due Date** it sends a private
message to the people you chose on the card, so they remember to fill in and confirm the
information of the post. You decide, per card, **who** is told and **on which days**
(for example a week, three days and one day before).

- Plain **Python 3.12, standard library only** (no packages to install, nothing to rot).
- Runs as **one small Docker container** next to Mattermost, in the same `docker-compose.yml`.
- **Never sends the same reminder twice**, even after crashes or restarts.
- Never changes anything on your boards. It only reads cards and sends direct messages.
- **Controlled from chat by system administrators**: `/remind_send`, `/remind_status`,
  `/remind_setmessage`, `/remind_setsend` and more (see [docs/chat-commands.md](docs/chat-commands.md)).

> Written for the NEI/AAC community's chat. Nothing in it is specific to us except the default
> message wording and the Lisbon timezone; both are settings.

## Try it in two commands

You only need Docker. This starts a throwaway Mattermost with the Boards plugin, fake people, demo
boards and the bot itself, on your machine only (http://localhost:8066):

```shell
docker compose up -d --build     # the first start takes a few minutes: downloads, plugin, demo data
docker compose down -v           # stops it and deletes everything
```

Log in as `devadmin` (password `devadmin-password`) and type `/remind_help`. What it holds and how to
work on the bot with it: [docs/development.md](docs/development.md).

## Where to read next

| You want to... | Read |
| --- | --- |
| understand what a card needs to be reminded (this page, below) | [What it does](#what-it-does), [How a card is handled](#how-a-card-is-handled) |
| put it on your server | [docs/setup.md](docs/setup.md) |
| change a setting or the message | [docs/configuration.md](docs/configuration.md) |
| control it from chat | [docs/chat-commands.md](docs/chat-commands.md) |
| know what it guarantees, and how it works inside | [docs/how-it-works.md](docs/how-it-works.md) |
| know what it can and cannot do to the rest of your system | [docs/security.md](docs/security.md) |
| run it day to day, or find out why a message did not arrive | [docs/operations.md](docs/operations.md) |
| work on the code, run the tests, understand the decisions | [docs/development.md](docs/development.md) |
| send a change, report a problem, or learn the rules for a pull request | [CONTRIBUTING.md](CONTRIBUTING.md) |

## The repository

```
src/remindabot/   the bot (Python, standard library only)
tests/            unit, fuzz and integration tests (no Docker); tests/e2e runs against the dev stack
config/           message.txt: the default message, copied into the image
Dockerfile        the bot's image (compose.yaml and the production compose file build it)
compose.yaml      the local dev stack: Mattermost + Boards + demo data + the bot
dev/              what the dev stack needs besides the bot (toolbox image), the production-copy sandbox, the deployment rehearsal
scripts/          shell helpers: check the image, make the bot read-only, seed the dev stack
docs/             the documentation
CONTRIBUTING.md   how to propose a change; the rules a change must keep
LICENSE           MIT, copyright NEI/AAC
```

## What it does

Your Communication team puts cards on a board. Each card has a **Due Date**, a list of
**Notifyees** (the people who must be told) and a **Remind Before** list (how many days ahead to
remind, e.g. `7;3;1`). Every morning the bot looks at all cards; on each listed day, every
notifyee receives a message like this (the wording is yours to edit, see
[Changing the message](docs/configuration.md#changing-the-message)):

> Olá Ana! 👋
>
> Este é um lembrete automático da equipa de Comunicação: a informação do cartão **Workshop de
> Python** (quadro *Posts*) tem de estar preenchida e confirmada até **domingo, 25 de outubro de
> 2026** (**faltam 12 dias**).
>
> Por favor, verifica se estão completos e corretos: a descrição do post; o local; a hora.
>
> 👉 [Abrir o cartão](https://chat.example.com/boards/team/…)

## How a card is handled

Every day at `REMINDER_SEND_AT` (14:00, Europe/Lisbon) the bot looks at **every board it can see**
that has these three properties (matched by name, case-insensitive, and by *type*):

| Board property | Type | Meaning |
| --- | --- | --- |
| `Due Date` | Date | When the post must be ready. |
| `Notifyees` | Multi person (or Person) | **Who gets the message.** Only the people listed here are messaged. It is deliberately separate from `Assignees`, so you pick exactly who must be told. |
| `Remind Before` | **Text** | A **list** of "how many days before the Due Date to remind", separated by `;`. Set it per card. |

`Remind Before` is a **Text** property: type whole numbers separated by `;`.

| You type | Meaning |
| --- | --- |
| `7;3;1` | Remind 7, 3 and 1 days before the Due Date. |
| `7; 3; 1` | The same: spaces are ignored. |
| `12` | One reminder, 12 days before. |
| *(empty)*, `0` or `-1` | Never remind (use `0` / `-1` on cards you want ignored). |

That is the whole rule. Order and repeats do not matter (`1;7;3;7` is `7;3;1`). Anything between
two `;` that is not a whole number is **ignored and reported in the log**, while the numbers around
it still work. That includes `7,3,1` (commas do not separate), `7 dias`, `3d`, `1 semana`, `tres`
and `1.5`: nothing is guessed, so a typo never silently loses a reminder, and a bare `1` is never
left behind from `1 semana`.

The property must really be of type **Text**. Number, Select and Multi select are not read: a
board that has `Remind Before` with another type is skipped, with a warning in the log that names
the type. In Boards the type of an existing property is changed from its header menu (*Type*);
a leftover value that is not text is reported as `(not text)` until it is retyped.

A card gets reminders when **all** of these are true:

- `Remind Before` holds at least one whole number **greater than 0** (and at most 3660).
  Empty, `0` and `-1` mean "never remind"; use `0` / `-1` on cards you want ignored. Anything in
  the list that is not a whole number (`tres`, `1.5`, `7,3`) is ignored, and **the log warns about
  it**, so a typo never silently loses a reminder.
- It has a `Due Date` and at least one notifyee. (A card that is due a reminder but has nobody in
  `Notifyees` is reported in the log and in the summary line.)
- It is not a *card template* (Boards marks those itself) and not deleted.
- **A listed number of days has been reached:** today is at most that many days before the Due
  Date, and not after it.

Every listed number is one reminder. On any day, the card's current reminder is the **smallest
listed number that is still at least the number of days left**, and it is sent once. For
`Due Date = 25 Oct` and `Remind Before = 7;3;1`:

| Today | Days left | What happens |
| --- | --- | --- |
| 17 Oct | 8 | Too early: nothing. |
| 18 Oct | 7 | **Message** ("faltam 7 dias"). |
| 19 – 21 Oct | 6 – 4 | Nothing: the 7-day reminder was already sent. |
| 22 Oct | 3 | **Message** ("faltam 3 dias"). |
| 23 Oct | 2 | Nothing. |
| 24 Oct | 1 | **Message** ("falta 1 dia"). |
| 25 Oct | 0 | Nothing: the last reminder was yesterday. |
| 26 Oct | – | Passed: nothing is sent anymore. |

Rules worth knowing:

- Each person gets **each listed reminder once** per card per Due Date, and **never more than one
  message per card per day**. If the Due Date changes, the list starts over for the new date.
  Someone added to `Notifyees` later gets the reminder that applies at that moment.
- A card seen late is told once, not once per missed reminder: with `14;7;3;1` and a Due Date
  set when only 2 days are left, there is a single message (the "3 days" reminder, which says
  "faltam 2 dias") and then the "1 day" one.
- Editing the list never repeats a message. Once somebody was told at "3 days", adding or removing
  bigger numbers sends nothing more; only a smaller reminder still to come is sent.
- A card whose Due Date is set when a reminder is already due is reminded at the next daily run.
- Only people who may see the board are messaged: **members of the board** for a private board,
  **members of the team** for an open board. (The Notifyees field can hold any user id that
  someone with edit rights typed in, so it is not trusted to decide who reads a card title.)
- Card titles, board titles and names are typed by users but appear in a message that comes from
  the bot, so Markdown, links, `@mentions` and extra lines in them are neutralized: the text is
  still shown, just inert.
- A board that has the `Remind Before` property but is otherwise set up wrongly (a property
  missing, for example `Notifyees` on a board that still only has `Assignees`, or `Remind Before`
  created as a *Number* or *Select* instead of *Text*) is reported in the logs as a warning rather
  than failing silently.
