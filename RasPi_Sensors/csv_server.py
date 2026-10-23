#!/usr/bin/env python3
"""
================================================================================
CSV FILE SERVER — Serves sensor data CSV files over HTTP
================================================================================

Lightweight HTTP server that serves monthly CSV files from the Raspberry Pi.
Used with Cloudflare Tunnel + Grafana Infinity plugin to query historical
data older than the 30-day InfluxDB Cloud retention limit.

USAGE:
    python3 csv_server.py                    # Start on port 8090
    python3 csv_server.py --port 9000        # Custom port

FILES SERVED:
    ~/sensor_logs/   — Monthly CSV files written by pi_sensor_reader.py
                       e.g. A0_2026-03.csv, A1_2026-03.csv, ...

    Directory listing at http://localhost:8090/ returns JSON list of files.
    Individual files at  http://localhost:8090/A0_2026-03.csv

SECURITY:
    - Only serves files from the logs directory (no path traversal)
    - Only serves .csv files
    - Read-only (GET only, no uploads)
    - Intended to sit behind Cloudflare Tunnel (adds auth + HTTPS)

SETUP:
    1. Copy this file to ~/sensor_project/csv_server.py
    2. Run once to test: python3 csv_server.py
    3. Set up as systemd service (see CSV_SERVER_SETUP.md)
    4. Set up Cloudflare Tunnel to expose it

================================================================================
"""

import os
import json
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

# --- Configuration ---
SERVE_DIR = os.path.expanduser("~/sensor_logs")
DEFAULT_PORT = 8090


class CSVHandler(BaseHTTPRequestHandler):
    """Serves CSV files from the sensor logs directory."""

    def do_GET(self):
        path = unquote(self.path).strip("/")
        if path == "" or path == "/":
            self._serve_file_list()
            return
        self._serve_file(path)

    def _serve_file_list(self):
        """Return JSON list of available CSV files with metadata."""
        if not os.path.isdir(SERVE_DIR):
            self._send_json({"error": "Logs directory not found", "files": []}, 404)
            return

        files = []
        for f in sorted(os.listdir(SERVE_DIR)):
            if f.endswith(".csv"):
                filepath = os.path.join(SERVE_DIR, f)
                stat = os.stat(filepath)
                files.append({
                    "name": f,
                    "size_bytes": stat.st_size,
                    "modified": stat.st_mtime,
                    "url": f"/{f}",
                })

        self._send_json({"files": files, "count": len(files)})

    def _serve_file(self, filename):
        """Serve a specific CSV file."""
        if not filename.endswith(".csv"):
            self._send_error(403, "Only .csv files are served")
            return
        if "/" in filename or "\\" in filename or ".." in filename:
            self._send_error(403, "Path traversal not allowed")
            return

        filepath = os.path.join(SERVE_DIR, filename)
        if not os.path.isfile(filepath):
            self._send_error(404, f"File not found: {filename}")
            return

        try:
            with open(filepath, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self._send_error(500, str(e))

    def _send_json(self, data, status=200):
        content = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content)

    def _send_error(self, status, message):
        self._send_json({"error": message}, status)

    def log_message(self, format, *args):
        print(f"[csv_server] {self.address_string()} - {format % args}")


def main():
    parser = argparse.ArgumentParser(description="CSV File Server for Grafana")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to listen on (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    os.makedirs(SERVE_DIR, exist_ok=True)

    server = HTTPServer(("0.0.0.0", args.port), CSVHandler)
    print("=" * 60)
    print("CSV FILE SERVER")
    print("=" * 60)
    print(f"Serving files from: {SERVE_DIR}")
    print(f"Listening on:       http://0.0.0.0:{args.port}")
    print(f"File list:          http://localhost:{args.port}/")
    print(f"Example file:       http://localhost:{args.port}/A0_2026-03.csv")
    print("=" * 60)
    print("Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
