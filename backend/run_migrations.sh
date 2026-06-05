#!/bin/bash
set -e

echo "Running Alembic migrations..."

# Ensure DATABASE_URL is set
if [ -z "$DATABASE_URL" ]; then
  echo "Error: DATABASE_URL environment variable is not set."
  echo "Please set it using: export DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dbname"
  exit 1
fi

# Reconcile the Alembic 0143 revision-identity collision BEFORE upgrading: on a
# pre-renumber main DB (stamped at the divergent 0143 with `computejob` absent)
# this rewinds the version pointer to the shared parent so `alembic upgrade head`
# re-derives the chain and creates `computejob`. Strict no-op on fresh,
# already-correct, or already-reconciled databases. See app/migration_reconciliation.py.
echo "Reconciling Alembic revision-identity divergences (pre-renumber main)..."
python3 -m app.migration_reconciliation

alembic upgrade head

echo "Migrations complete."
