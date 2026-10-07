"""Entry point: ``python run.py``. This is also the target PyInstaller bundles in the packaging phase."""

from __future__ import annotations

import uvicorn

from app.config import get_settings
from app.main import app


def main() -> None:
    settings = get_settings()
    # Pass the app object (not an import string) so a frozen build needs no dynamic import.
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())
    server = uvicorn.Server(config)
    app.state.server = server  # lets POST /api/control/shutdown request a graceful exit
    server.run()


if __name__ == "__main__":
    main()
