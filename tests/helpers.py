import json
import sqlite3
from pathlib import Path

# Redacted fixture of Scout's assistant_usage_events contract: exact required
# columns and id primary-key semantics, with fictional values only.
SOURCE_SCHEMA = """
CREATE TABLE assistant_usage_events (
 id INTEGER PRIMARY KEY, session_id TEXT, model TEXT,
 input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
 cache_write_tokens INTEGER, reasoning_tokens INTEGER, total_nano_aiu INTEGER,
 token_details_json TEXT, created_at TEXT, api_endpoint TEXT
)
"""


def make_source(path: Path, rows=None):
    connection = sqlite3.connect(path)
    connection.execute(SOURCE_SCHEMA)
    for row in rows or []:
        connection.execute(
            "INSERT INTO assistant_usage_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row
        )
    connection.commit()
    connection.close()


def detail(tokens=10, cost=2, batch=1):
    return json.dumps([{"tokenCount": tokens, "costPerBatch": cost, "batchSize": batch}])


def event(identifier=1, session="synthetic-session", model="model-a", total=20,
          details=None, created="2025-01-01T00:00:00Z"):
    return (identifier, session, model, 10, 5, 2, 1, 3, total,
            details if details is not None else detail(), created, "/synthetic")
