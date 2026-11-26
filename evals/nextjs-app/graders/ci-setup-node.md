---
type: regex
target: {source: file, path: .github/workflows/ci.yml}
pattern: 'actions/setup-node@(v7(?![0-9])|[0-9a-f]{40}\s*#\s*v7)'
---

CI uses the current major of actions/setup-node (v7).
