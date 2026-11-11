#!/bin/sh
# read-only helper: SQL on stdin
docker exec -i opspilot-postgres-1 sh -c "psql -U \"\$POSTGRES_USER\" -d \"\${POSTGRES_DB:-\$POSTGRES_USER}\" -At -F \" | \""
