#!/bin/bash
cd "$(dirname "$0")"
PYTHONUNBUFFERED=1 exec venv/bin/python -u app.py
