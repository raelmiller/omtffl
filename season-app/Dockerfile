FROM python:3.12-slim

WORKDIR /srv

# The rules engine lives in shadow/ and is imported, not reimplemented. It
# ships with the app so the container has one source of truth for scoring.
COPY season-app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shadow/ ./shadow/
COPY season-app/app/ ./app/

ENV PYTHONUNBUFFERED=1 SHADOW_DIR=/srv/shadow
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
