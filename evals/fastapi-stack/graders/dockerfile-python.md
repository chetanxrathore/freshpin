---
type: regex
target: {source: file, path: Dockerfile}
flags: m
pattern: '^FROM\s+(docker\.io/)?(library/)?python:3\.15'
---

The Dockerfile uses the current Python release line (3.15).
