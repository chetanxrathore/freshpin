---
type: regex
target: {source: file, path: package.json}
pattern: '"typescript"\s*:\s*"[\^~]?7\.'
---

package.json targets the current TypeScript major (7).
