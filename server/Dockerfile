# syntax=docker/dockerfile:1
# Build from the repo root: docker buildx build -f server/Dockerfile -t regulator .
#
# One stage, because there is nothing to build. The UI is a single
# self-contained HTML file with no bundler, so this image has no Node in it at
# all: nothing to install, nothing to cache, nothing to go stale.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 10012 --shell /usr/sbin/nologin regulator

WORKDIR /app

# Both requirement sets: the control plane imports the worker package directly
# rather than shelling out to it, which is what keeps the web UI and the
# command line running identical code.
COPY worker/requirements.txt /app/worker-requirements.txt
COPY server/requirements.txt /app/server-requirements.txt
RUN pip install --no-cache-dir -r /app/worker-requirements.txt -r /app/server-requirements.txt

COPY worker/regulator_agent /app/regulator_agent
COPY server/regulator_server /app/regulator_server
COPY server/ui /app/ui
COPY scenarios /app/scenarios

ENV PYTHONPATH=/app \
    REG_BUILTIN_SCENARIOS_DIR=/app/scenarios \
    REG_DATABASE_URL=sqlite:////data/regulator.db

# The database and any run artefacts live here. Mount a volume, or every run
# and every target disappears when the container is replaced.
RUN mkdir -p /data && chown regulator:regulator /data
VOLUME ["/data"]

EXPOSE 8080

USER regulator

# Exec form, no shell: a shell between PID 1 and uvicorn swallows SIGTERM, and
# a control plane that is killed rather than asked to stop leaves runs marked
# running forever.
ENTRYPOINT ["python", "-m", "uvicorn", "regulator_server.app:app", "--host", "0.0.0.0", "--port", "8080"]
