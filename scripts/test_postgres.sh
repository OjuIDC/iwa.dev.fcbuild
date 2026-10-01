#!/usr/bin/env sh
# Start a disposable PostgreSQL 16 for the test suite, bound to localhost only.
#   eval "$(scripts/test_postgres.sh)" && python -m pytest
# Stop it with:  docker rm -f fc27-test-postgres
set -eu
PORT="${TEST_POSTGRES_PORT:-55432}"
docker rm -f fc27-test-postgres >/dev/null 2>&1 || true
docker run -d --name fc27-test-postgres -p "127.0.0.1:${PORT}:5432" \
  -e POSTGRES_USER=fc27test -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine >/dev/null
i=0; until docker exec fc27-test-postgres pg_isready -U fc27test >/dev/null 2>&1; do
  i=$((i+1)); [ "$i" -gt 60 ] && { echo "postgres did not start" >&2; exit 1; }; sleep 1; done
echo "export TEST_POSTGRES_URI=postgresql://fc27test@127.0.0.1:${PORT}/postgres"
