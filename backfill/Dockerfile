FROM python:3.13-slim

WORKDIR /app

# build context is this directory (alongside fly.toml). bulk-enrich.py here is
# a copy of scripts/bulk-enrich.py kept in sync at deploy time (see
# README.md or the justfile recipe). stdlib-only — no pip install needed.
COPY bulk-enrich.py /app/bulk-enrich.py

ENV PYTHONUNBUFFERED=1

# starts from rowid 0 — script's skip-already-enriched logic blows past the
# fast already-good prefix in minutes. crashing exits non-zero → fly restarts
# (max 5 attempts) → resumes from 0 again (cheap to walk past good rows).
CMD ["python3", "/app/bulk-enrich.py"]
