---
type: regex
target: {source: file, path: docker-compose.yml}
pattern: 'redis:8(\.12)?(?![0-9.])'
---

Compose uses the current Redis release line (8 / 8.12).
