import sqlite3
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib


def get_db_path() -> str:
    config_path = Path("config.toml")
    if not config_path.exists():
        print("config.toml not found, defaulting to crawler.db")
        return "crawler.db"
    
    with open(config_path, "rb") as f:
        config = tomllib.load(f)
    return config.get("storage", {}).get("sqlite_db_path", "crawler.db")


def migrate():
    db_path = get_db_path()
    if not Path(db_path).exists():
        print(f"Database {db_path} does not exist. Nothing to migrate.")
        return

    print(f"Migrating {db_path}...")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 1. Read existing data
    try:
        rows = conn.execute("SELECT * FROM island_ranges").fetchall()
    except sqlite3.OperationalError:
        print("Table island_ranges does not exist or cannot be read. Exiting.")
        conn.close()
        return

    data = []
    has_newest_boundary = False
    has_newest_checked_at = False

    if rows:
        keys = rows[0].keys()
        has_newest_boundary = "newest_boundary" in keys
        has_newest_checked_at = "newest_checked_at" in keys

        if has_newest_checked_at:
            print("Table already has newest_checked_at. No migration needed.")
            conn.close()
            return

        for row in rows:
            data.append({
                "author": row["author"],
                "min_id": row["min_id"],
                "max_id": row["max_id"],
                "oldest_boundary": row["oldest_boundary"],
                # We default the newest_checked_at to 0.0 for all existing records
                "newest_checked_at": 0.0,
            })
    else:
        # Check columns if table is empty
        cursor = conn.execute("PRAGMA table_info(island_ranges)")
        columns = [col["name"] for col in cursor.fetchall()]
        if "newest_checked_at" in columns:
            print("Table already has newest_checked_at. No migration needed.")
            conn.close()
            return

    # 2. Drop the old table
    conn.execute("DROP TABLE island_ranges")

    # 3. Create the new table
    DDL_ISLAND_RANGES = """
    CREATE TABLE island_ranges (
        author           TEXT NOT NULL,
        min_id           INTEGER NOT NULL,
        max_id           INTEGER NOT NULL,
        oldest_boundary  INTEGER DEFAULT 0,
        newest_checked_at REAL DEFAULT 0.0,
        PRIMARY KEY (author, min_id)
    );
    """
    conn.execute(DDL_ISLAND_RANGES)

    # 4. Insert data back
    if data:
        conn.executemany(
            "INSERT INTO island_ranges (author, min_id, max_id, oldest_boundary, newest_checked_at) "
            "VALUES (:author, :min_id, :max_id, :oldest_boundary, :newest_checked_at)",
            data,
        )

    conn.commit()
    conn.close()
    print(f"Migration completed. {len(data)} records migrated.")


if __name__ == "__main__":
    migrate()
