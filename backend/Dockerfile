FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency metadata first: this layer is cached across source edits, so an ordinary
# code change rebuilds in seconds rather than reinstalling the world.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# Non-root. The container has no reason to run with more privilege than it needs.
RUN useradd --create-home --uid 1000 quorum && chown -R quorum:quorum /app
USER quorum

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
