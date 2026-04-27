#!/usr/bin/env python3
"""Start en lokal webserver og åbn dashboard i browser."""
import os
import webbrowser
import http.server
import socketserver
from pathlib import Path

PORT = 8080
BASE_DIR = Path(__file__).parent

os.chdir(BASE_DIR)

url = f"http://localhost:{PORT}/dashboard/"
print(f"Dashboard:  {url}")
print(f"Stop:       Ctrl+C\n")
webbrowser.open(url)

with socketserver.TCPServer(("", PORT), http.server.SimpleHTTPRequestHandler) as httpd:
    httpd.serve_forever()
