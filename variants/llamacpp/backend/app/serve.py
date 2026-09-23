import os
from pathlib import Path

from app.main import create_app

app = create_app(Path(os.environ["DB_PATH"]), Path(os.environ["STORAGE_DIR"]))
