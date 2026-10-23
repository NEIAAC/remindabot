FROM python:3.15-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Unprivileged user; /data holds the SQLite state (mounted as a named volume by compose).
RUN useradd --system --uid 10001 --no-create-home reminder \
 && mkdir /data \
 && chown reminder /data

# Fail the build (instead of failing at 14:00 one day) if the timezone database is missing.
RUN python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Lisbon')"

# Default message, so the image works on its own. Deployments normally mount their own copy
# over /config/message.txt (docs/configuration.md) so the wording can be edited without rebuilding.
COPY config/message.txt /config/message.txt

# The package sits in /srv/src and is found through PYTHONPATH: nothing is pip-installed, so the
# build needs no network beyond the base image.
WORKDIR /srv
COPY src ./src
ENV PYTHONPATH=/srv/src
USER reminder

# The chat-command endpoint (REMINDER_COMMANDS_PORT). Mattermost calls it over the Docker network:
# never publish this port.
EXPOSE 8080

# The main loop touches /tmp/heartbeat every minute (and while a pass is running).
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/heartbeat') < 300 else 1)"

CMD ["python", "-m", "remindabot"]
