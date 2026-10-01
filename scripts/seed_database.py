#!/usr/bin/env python3
"""
Seed and initialize database schema, roles, and default permissions.
Usage:
    python scripts/seed_database.py
"""
import os
import sys
import asyncio

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from postgresql_db.setup_pbac_schema import setup

if __name__ == "__main__":
    print("Starting template database initialization...")
    asyncio.run(setup())
    print("Database seeding completed successfully.")
