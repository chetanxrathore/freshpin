---
type: regex
target: {source: file, path: requirements.txt}
flags: im
pattern: '^fastapi[^\n]*0\.16[5-9]'
---

requirements.txt targets the current FastAPI release (0.165+).
