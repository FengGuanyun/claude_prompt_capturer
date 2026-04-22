"""
Unified Server — Entry point.
Delegates to the server package for all logic.
"""

import sys
import platform
from pathlib import Path

# Ensure server package is importable
sys.path.insert(0, str(Path(__file__).parent))

from server import app, _start_ws_handler

def main():
    ws_port = _start_ws_handler(app)

    print("=" * 50)
    print("Unified Server — Port 5000")
    print("=" * 50)
    print(f"\nHome (Terminal):  http://localhost:5000")
    print(f"Agent:            http://localhost:5000/agent")
    print(f"WebSocket PTY:    ws://localhost:{ws_port}")
    print("\n" + "=" * 50)

    app.run(host="0.0.0.0", port=5000, threaded=True)

if __name__ == "__main__":
    main()
