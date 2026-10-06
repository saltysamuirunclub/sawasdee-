"""Log to stdout (for journalctl) and to a rotating file."""
import logging
from logging.handlers import RotatingFileHandler

from .config import settings


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if getattr(root, "_club_configured", False):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            settings.log_dir / "app.log", maxBytes=2_000_000, backupCount=5
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:  # read-only FS etc. — console logging still works
        root.warning("File logging disabled: %s", exc)

    root._club_configured = True
