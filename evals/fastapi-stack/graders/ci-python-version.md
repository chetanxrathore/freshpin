---
type: regex
target: {source: file, path: .github/workflows/ci.yml}
pattern: 'python-version:[^\n]*3\.15'
---

CI tests on the current Python release (3.15).
