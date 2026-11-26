---
type: regex
target: {source: file, path: docker-compose.yml}
pattern: 'postgres:19(?![0-9])'
---

Compose uses the current PostgreSQL major (19).
