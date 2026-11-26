ARG PYTHON_VERSION=3.10
FROM python:${PYTHON_VERSION}-slim AS builder
RUN pip install uv

FROM builder AS test
FROM node:18-alpine AS assets
FROM debian:bullseye-slim
FROM ubuntu:22.04
FROM ghcr.io/acme/base:1.0
FROM scratch
