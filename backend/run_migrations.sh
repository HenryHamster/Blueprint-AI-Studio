#!/bin/bash
set -e

echo "Running Alembic migrations..."

# Ensure DATABASE_URL is set
if [ -z "$DATABASE_URL" ]; then
  echo "Error: DATABASE_URL environment variable is not set."
  echo "Please set it using: export DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dbname"
  exit 1
fi

alembic upgrade head

echo "Migrations complete."
