# syntax=docker/dockerfile:1
# Build from the repo root: docker buildx build -f server/Dockerfile -t regulator .
#
# Two stages. The first compiles the operator console from web/ with Node; the
# second is the image that actually runs, and has no Node, no npm and no
# node_modules in it at all. Only the built bundle crosses between them.
#
# The console is built here rather than committed because a 1.5 MB minified
# artefact in git conflicts on every branch and tells a reviewer nothing. The
# cost is that this image cannot be built without a network the first time;
# the benefit is that the bundle can never be out of step with its sources,
# which is the failure the old committed-HTML arrangement invited.

# ----------------------------------------------------------------- ui build
FROM node:20-slim AS ui

WORKDIR /ui

# Dependencies first, and only the manifests, so a change to the console's
# source does not re-download the Splunk component library.
COPY web/package.json web/package-lock.json web/.npmrc ./
RUN npm ci --no-audit --no-fund

COPY web/ ./
# The build writes to ../server/ui, which is outside this WORKDIR by design:
# it is where the control plane looks for the console.
RUN mkdir -p /server/ui && npm run build

# ------------------------------------------------------------------ runtime
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
COPY scenarios /app/scenarios

# The console, from the Node stage. index.html plus one content-hashed bundle
# under assets/; app.py serves the first and mounts the second.
COPY --from=ui /server/ui /app/ui

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
