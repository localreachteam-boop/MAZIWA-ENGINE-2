"""
Dashboard Server — Serves the web UI and streams trade events via WebSocket.
"""
import asyncio
import json
import os
import websockets
from pathlib import Path

connected_clients = set()
event_queue = asyncio.Queue()

PORT = 8765

# Simple HTTP server for the HTML page
from http.server import HTTPServer, SimpleHTTPRequestHandler
import threading

class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent / "templates"), **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, format, *args):
        pass

def run_http_server(port=8766):
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    server.serve_forever()

# WebSocket server for streaming
async def ws_handler(websocket):
    connected_clients.add(websocket)
    try:
        # Send current state on connect
        state_msg = get_current_state()
        await websocket.send(json.dumps(state_msg))
        async for message in websocket:
            pass  # Client doesn't send anything
    except websockets.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(websocket)

def get_current_state():
    """Return current trading state."""
    try:
        state_file = Path(__file__).parent.parent / "trading_state.json"
        if state_file.exists():
            with open(state_file) as f:
                return json.load(f)
    except:
        pass
    return {
        "balance": 0, "trades": 0, "wins": 0, "losses": 0,
        "win_rate": 0, "daily_loss": 0, "cycles": 0,
        "bestStreak": 0, "type": "state",
    }

async def broadcast(message):
    """Send message to all connected dashboard clients."""
    if connected_clients:
        msg = json.dumps(message)
        await asyncio.gather(
            *[client.send(msg) for client in connected_clients],
            return_exceptions=True,
        )

async def start_servers():
    # Start HTTP server in thread
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()
    print(f"[Dashboard] HTTP server on http://0.0.0.0:8766")

    # Start WebSocket server
    print(f"[Dashboard] WebSocket server on ws://0.0.0.0:{PORT}")
    async with websockets.serve(ws_handler, "0.0.0.0", PORT):
        await asyncio.Future()  # Run forever

# Entry point for standalone
if __name__ == "__main__":
    asyncio.run(start_servers())
