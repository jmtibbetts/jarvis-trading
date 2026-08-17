"""Shared deterministic fixtures for the test suite.

Nothing here reaches a network, a provider or the operator's database. Every
helper writes into whatever database conftest.py has already redirected the
process to, which is a per-session temporary file.
"""
