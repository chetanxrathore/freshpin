---
name: fastapi-stack
description: Scaffold a FastAPI service with Dockerfile, compose (Postgres + Redis) and CI
tags: [python, docker, github-actions]
runs: 2
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Write, Edit, Bash, Glob, Grep, Skill]
---

You are in an empty project directory. Scaffold a small FastAPI service:

- `requirements.txt` listing fastapi, uvicorn[standard], sqlalchemy, psycopg[binary], redis and pytest, each with a minimum version (`>=`).
- `app/main.py` with a `/health` endpoint.
- `Dockerfile` that runs the app with uvicorn on a slim official Python base image.
- `docker-compose.yml` with services `app` (built from the Dockerfile), `db` (official PostgreSQL image) and `cache` (official Redis image).
- `.github/workflows/ci.yml` that runs on push, checks out the code, sets up Python with actions/setup-python, installs the requirements and runs pytest.

Use current, production-appropriate versions. Do not ask questions: write the files, then finish with a one-paragraph summary.
