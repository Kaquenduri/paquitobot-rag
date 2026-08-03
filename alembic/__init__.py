"""Alembic migration environment.

Reads the async SQLAlchemy URL from the ``SUPABASE_DATABASE_URL`` environment
variable (the same key consumed by ``app.core.config.Settings``) and runs
migrations against it. The first real migration is introduced in PR 3.
"""
