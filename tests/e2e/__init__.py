"""Live-stack tests: the bot against a REAL Mattermost and a REAL Boards plugin.

Run them from the repository root with the dev stack up (docs/development.md):

    docker compose up -d --build
    docker compose --profile test run --rm e2e

They need what only the stack has (the demo boards, the bot's and the admin's tokens), so without
it they are skipped, and a plain `python -m unittest` stays green on a machine with no Docker.
"""
