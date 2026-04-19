"""Entry point for the management UI.

Spawned by ``memory.start_ui()`` (Phase 10). Binds to 127.0.0.1:0, writes
the resulting URL atomically to ``$BETTER_MEMORY_HOME/ui.url`` so the
parent process can discover it, then serves forever until killed by
``/shutdown`` or the inactivity watchdog.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path

from werkzeug.serving import make_server

from better_memory.config import resolve_home
from better_memory.ui.app import create_app


def _ui_url_path() -> Path:
    return resolve_home() / "ui.url"


def _write_url_atomically(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(url)
    os.replace(tmp, dest)


def _delete_url(dest: Path) -> None:
    try:
        dest.unlink()
    except FileNotFoundError:
        pass


def main() -> None:
    app = create_app()
    server = make_server(host="127.0.0.1", port=0, app=app, threaded=False)
    port = server.port  # werkzeug BaseWSGIServer.port is the bound port
    url = f"http://127.0.0.1:{port}"

    dest = _ui_url_path()
    _write_url_atomically(url, dest)
    atexit.register(_delete_url, dest)

    try:
        server.serve_forever()
    finally:
        _delete_url(dest)


if __name__ == "__main__":
    main()
