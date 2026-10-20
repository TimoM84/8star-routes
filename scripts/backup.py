"""Write a consistent online backup of the database to DATA_DIR/backups.

Safe while the application is running:  python scripts/backup.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config  # noqa: E402
from app.db import Database, backup  # noqa: E402

cfg = load_config()
db = Database(cfg.db_path)
path = backup(db, cfg.backup_dir, prefix="manual", keep=cfg.backup_keep)
print(path)
