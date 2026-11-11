#!/bin/bash
# q.sh <project>: read-only SQL on stdin against that project's database
docker exec -i "$1-postgres-1" sh -c 'psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-$POSTGRES_USER}" -At -F " | "'
