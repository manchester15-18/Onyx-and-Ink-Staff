"""Minimal HTTP server to serve the Onyx & Ink modern dashboard preview.

Serves preview-modern.html on port 8765, accessible on LAN and Tailscale.
"""
import http.server
import socketserver
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8765"))
HOST = "0.0.0.0"

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "dashboard"), **kwargs)

    def do_GET(self):
        # Serve preview-modern.html at root and /preview-modern.html
        if self.path in ("/", "/preview-modern.html"):
            self.path = "/preview-modern.html"
        return super().do_GET()

    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}")

with socketserver.TCPServer((HOST, PORT), Handler) as httpd:
    print(f"Onyx & Ink dashboard preview: http://{HOST}:{PORT}/preview-modern.html")
    print(f"LAN: http://10.0.0.20:{PORT}/preview-modern.html")
    httpd.serve_forever()
