---
type: regex
target: {source: file, path: Dockerfile}
flags: m
pattern: '^FROM\s+(docker\.io/)?(library/)?node:26'
---

The Dockerfile uses the current Node.js LTS (26).
