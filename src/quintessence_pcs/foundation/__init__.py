"""Low-level PCS building blocks: errors, identifier checks, SQLite plumbing.

The modules here have no domain knowledge.  They are the pieces every later
store or engine is expected to reuse instead of re-opening SQLite by hand.
"""

from __future__ import annotations
