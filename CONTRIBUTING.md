# Contributing to remindabot

Thank you for helping to maintain the bot. It is small on purpose, and a new maintainer should be
able to change it safely after reading a few pages.

## Where to start

1. Read the [README](README.md) (what the bot does) and [docs/development.md](docs/development.md)
   (the layout of the code, the dev stack, and why things are the way they are).
2. Try it: `docker compose up -d --build` starts a throwaway Mattermost with demo boards and the bot
   on your machine only (see the README). `docker compose down -v` deletes it all.
3. Find the file to change in the table "Changing behaviour" of
   [docs/development.md](docs/development.md#changing-behaviour).

## Before you open a pull request

- **Run the tests**: `python3 -m unittest` from the repository root. They need no Docker and no
  network and take several minutes; `python3 -m unittest tests.test_reminder` runs one file.
  Everything under `tests/e2e` needs the dev stack and is skipped without it
  (`docker compose --profile test run --rm e2e`).
- **Add a test with every change.** A bug fix gets a test that fails without the fix.
- **Keep the documents true.** `tests/test_scripts.py` checks that the paths, settings and chat
  commands the documents name still exist; update the document in the same pull request.
  If you change a promise in [docs/security.md](docs/security.md), change its test too.
- **Explain the code the way the surrounding code does**: simple comments, and the reason rather
  than the mechanism.

## Rules that keep the bot safe

These are not style. Each has a test, and a pull request that breaks one will fail it.

- **Python 3.12 or newer (the image runs 3.14) and the standard library only.** No dependencies to update or to hijack.
- **Helpers are shell scripts**, not Python (Python lives in `src/` and `tests/` only).
- **No secret in the repository, and no secret on a command line.** Tokens and passwords go through
  a file, standard input or the environment, never an argument, because `ps` shows arguments to
  everybody on the machine. No log line or chat reply may contain one either.
- **No real people in the repository**: no real names, e-mail addresses or exports of a real
  database. Use the demo users of the dev stack.
- **The bot only reads boards and sends direct messages.** If a change makes it write anywhere
  else, say so loudly in the pull request; it is the most important property of the bot.
- **A change must never send a reminder twice**, or silently skip one. See
  [docs/how-it-works.md](docs/how-it-works.md) before touching `runner.py`, `logic.py` or `store.py`.

## Releasing a version

The server runs a published image, `ghcr.io/neiaac/remindabot:<version>`. A maintainer publishes one like this:

1. Raise `version` in `pyproject.toml`, commit it and push (a pull request is fine).
2. Tag that commit and push the tag: `git tag -a v1.0.1 -m "What changed" && git push origin v1.0.1`.

The release workflow (`.github/workflows/release.yml`) then checks that the tag is the version in
`pyproject.toml`, runs all the tests, and only then publishes the image (`1.0.1` and `1.0`; there is no
`latest`, on purpose). Production changes by editing the version in its `image:` line, which
Dependabot proposes in the infrastructure repository; going back is the same edit. A tag that was
published is never moved: publish a new version instead.

## Questions, problems, ideas

Open an issue on [NEIAAC/remindabot](https://github.com/NEIAAC/remindabot/issues). Write what you
expected, what happened, and the relevant log lines **with the token and any personal data removed**.
If you think you found a security problem, open an issue that says so *without the details* and a
maintainer will agree a private way to receive them.

## License

By contributing you agree that your contribution is released under the [MIT license](LICENSE) of the
project.
