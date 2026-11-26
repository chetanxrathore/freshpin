---
type: regex
target: {source: file, path: .github/workflows/ci.yml}
pattern: 'node-version:[^\n]*26'
---

CI uses the current Node.js LTS (26).
