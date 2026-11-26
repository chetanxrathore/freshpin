---
name: nextjs-app
description: Scaffold a Next.js + TypeScript app with Dockerfile and CI
tags: [javascript, docker, github-actions]
runs: 2
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Write, Edit, Bash, Glob, Grep, Skill]
---

You are in an empty project directory. Scaffold a minimal Next.js app written in TypeScript:

- `package.json` with dependencies next, react and react-dom, and devDependencies typescript, @types/node and eslint, using caret (`^`) ranges. Do not run npm install.
- `app/page.tsx` that renders "Hello".
- `Dockerfile` that builds and serves the app on an official Node.js base image.
- `.github/workflows/ci.yml` that runs on push, checks out the code, sets up Node.js with actions/setup-node, runs `npm ci` and `npm test`.

Use current, production-appropriate versions. Do not ask questions: write the files, then finish with a one-paragraph summary.
