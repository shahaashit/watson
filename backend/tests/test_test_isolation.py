import os
from pathlib import Path


def test_test_exporter_backup_is_forced_beneath_the_scratch_root(conn):
    from app.config import settings
    from app.services import exporter

    data_dir = Path(os.environ["WATSON_DATA_DIR"]).resolve()
    backup_dir = Path(os.environ["WATSON_BACKUP_DIR"]).resolve()
    user_default = Path("~/GoogleDrive/watson").expanduser().resolve()

    assert data_dir.name == "data"
    assert backup_dir.name == "backup"
    assert data_dir.parent == backup_dir.parent
    assert settings.backup_dir.resolve() == backup_dir
    assert exporter._backup_dir(conn) == backup_dir
    assert backup_dir != user_default
