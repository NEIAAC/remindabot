# The few programs the helper scripts (scripts/) and the live-stack tests need, so that nothing but
# Docker has to be installed on a developer's machine. Used by compose.yaml (the `seed` step and the
# `unit` / `e2e` test services) and by dev/sandbox. Nothing of it ships with the bot.
FROM python:3.12-alpine

RUN apk add --no-cache bash curl jq tzdata ca-certificates

# The Boards plugin, the version production runs, fetched at BUILD time so Docker's build cache keeps
# it: a stack that is recreated does not download 45 MB again.
ARG BOARDS_VERSION=9.1.3
RUN curl -fsSL -o /opt/boards.tar.gz \
      "https://github.com/mattermost/mattermost-plugin-boards/releases/download/v${BOARDS_VERSION}/mattermost-plugin-boards-v${BOARDS_VERSION}-linux-amd64.tar.gz" \
 && [ "$(wc -c < /opt/boards.tar.gz)" -gt 10000000 ]

ENV TZ=Europe/Lisbon \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /repo
