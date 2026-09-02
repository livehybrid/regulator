# syntax=docker/dockerfile:1
# Build from the repo root: docker buildx build -f worker/Dockerfile -t regulator-worker .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 10011 --shell /usr/sbin/nologin regulator

WORKDIR /app

# Dependency layer first so source-only changes reuse the pip layer.
COPY worker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY worker/regulator_agent /app/regulator_agent

# The built-in scenario library ships inside the image on purpose. A worker is
# scheduled onto whatever node has capacity, often with no git credentials and
# sometimes with no egress to the control plane's git remote at all, so a cold
# start that had to fetch its scenarios would be a cold start that fails. Runs
# that use a custom library still override the directory at run time; this copy
# is the floor that guarantees the image alone is runnable.
COPY scenarios /app/scenarios
ENV REG_BUILTIN_SCENARIOS_DIR=/app/scenarios

ENV PYTHONPATH=/app

USER regulator

# Exec form, no shell. A shell between PID 1 and the agent swallows SIGTERM,
# which turns an orderly drain (finish the in-flight search, post the final
# result) into a 10 s wait and a SIGKILL that loses the run's tail latencies.
ENTRYPOINT ["python", "-m", "regulator_agent"]
