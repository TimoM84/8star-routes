"""Restore a backup over the database. STOP THE APPLICATION FIRST.

    python scripts/restore.py /data/backups/manual-20261020T120000Z.sqlite

The current database is backed up to DATA_DIR/backups/pre-restore-*.sqlite first.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config  # noqa: E402
from app.db import Database, backup, restore  # noqa: E402

if len(sys.argv) != 2:
    sys.exit(__doc__)
src = Path(sys.argv[1])
if not src.is_file():
    sys.exit(f"Backup not found: {src}")
cfg = load_config()
if cfg.db_path.exists():
    db = Database(cfg.db_path)
    print("current database saved as", backup(db, cfg.backup_dir, prefix="pre-restore"))
    db.close()
restore(src, cfg.db_path)
print("restored", src, "->", cfg.db_path)
