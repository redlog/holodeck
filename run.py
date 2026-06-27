"""Holodeck entry point — launches the FastAPI server and opens a browser.

    python run.py

The game runs in your browser at http://127.0.0.1:8000. The agent and world
layers (agents/, world/) are unchanged from the original pygame build; only
the presentation layer moved to HTML/CSS/JS served by server/app.py.
"""

import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import uvicorn

HOST = "127.0.0.1"
PORT = 8000


def _open_browser():
    webbrowser.open(f"http://{HOST}:{PORT}/")


def main():
    # Open the browser shortly after the server starts accepting connections.
    threading.Timer(1.5, _open_browser).start()
    uvicorn.run("server.app:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
