"""Entrypoint for Vercel (it looks for `app` in index.py). Locally, use: uvicorn app.main:app"""
from app.main import app  # noqa: F401
