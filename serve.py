"""Serve the dashboard on this computer.  python serve.py  ->  http://localhost:8000"""
import functools
import http.server
import os
import webbrowser

PORT = int(os.environ.get("PORT", "8000"))
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")

if __name__ == "__main__":
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
    with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler) as srv:
        url = f"http://localhost:{PORT}"
        print(f"Gifting Pulse running at {url}  (Ctrl+C to stop)")
        webbrowser.open(url)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
