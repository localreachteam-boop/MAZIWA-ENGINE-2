#!/usr/bin/env python3
"""Minimal dashboard server — just serves HTML + state.json from brain_active.py."""
import http.server, json, time, threading
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler

PORT = 9100
TEMPLATES = str(Path(__file__).parent / "dashboard" / "templates")
STATE_FILE = Path(__file__).parent / "trading_state.json"
DASH_STATE = Path(TEMPLATES) / "state.json"

class DashHandler(SimpleHTTPRequestHandler):
    def __init__(s, *a, **k): super().__init__(*a, directory=TEMPLATES, **k)
    def log_message(s, *a): pass

def sync_state():
    """Copy brain's state to dashboard's state.json every 2s."""
    while True:
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE) as f:
                    state = json.load(f)
                with open(DASH_STATE, 'w') as f:
                    json.dump(state, f)
        except: pass
        time.sleep(2)

if __name__ == "__main__":
    # Start state sync thread
    threading.Thread(target=sync_state, daemon=True).start()
    
    HTTPServer.allow_reuse_address = True
    httpd = HTTPServer(("0.0.0.0", PORT), DashHandler)
    print(f"  Dashboard: http://0.0.0.0:{PORT}")
    httpd.serve_forever()
