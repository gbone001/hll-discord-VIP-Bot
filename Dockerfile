# syntax=docker/dockerfile:1

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FRONTLINE_STATE_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --shell /usr/sbin/nologin frontline \
    && mkdir -p /data \
    && chown -R frontline:frontline /app /data

USER frontline

CMD ["python", "frontline-pass.py"]
