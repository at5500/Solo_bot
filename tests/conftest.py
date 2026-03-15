"""Minimal conftest — no app-level fixtures.

The project has many compiled .so modules that prevent loading the full
FastAPI app in a test environment without all runtime dependencies.
Tests are written as unit tests against individual functions and schemas.
"""
