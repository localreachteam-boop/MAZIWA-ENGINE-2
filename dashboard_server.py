#!/usr/bin/env python3
"""Dashboard v5 — overview layout, JS fetches state.json live."""
import http.server, socketserver, json, time, threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 9102
DIR = Path(__file__).parent / "dashboard" / "templates"
STATE_FILE = Path(__file__).parent / "trading_state.json"

def sync_loop():
    while True:
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE) as f: d = json.load(f)
                with open(DIR / 'state.json', 'w') as f: json.dump(d, f)
        except: pass
        time.sleep(2)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/state.json':
            self._serve('application/json', DIR / 'state.json')
        elif path == '/heartbeat.json':
            self._serve('application/json', DIR / 'heartbeat.json')
        elif path == '/activity.json':
            self._serve('application/json', DIR / 'activity.json')
        elif path in ('/', '/index.html'):
            self._serve('text/html', DIR / 'index.html')
        elif (DIR / path.lstrip('/')).exists():
            f = DIR / path.lstrip('/')
            ct = 'text/html' if str(f).endswith('.html') else 'application/json' if str(f).endswith('.json') else 'application/javascript' if str(f).endswith('.js') else 'text/plain'
            self._serve(ct, f)
        else:
            self.send_error(404)
    def _serve(self, ct, fp):
        try:
            data = Path(fp).read_text()
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data.encode())
        except:
            self.send_error(404)
    def log_message(self, *a): pass

threading.Thread(target=sync_loop, daemon=True).start()
HTTPServer.allow_reuse_address = True
import socket
socketserver.TCPServer.allow_reuse_address = True
httpd = HTTPServer(("0.0.0.0", PORT), Handler)
print(f"  Dashboard v5: http://0.0.0.0:{PORT}", flush=True)
httpd.serve_forever()
