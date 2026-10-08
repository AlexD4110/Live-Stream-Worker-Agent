"""python -m voice   Start the phone-line server (Plivo talks to this)."""
import sys

import uvicorn

from gifting import analysis
from voice import config, logs, server


def main():
    logs.configure()
    cfg = config.load()
    found = config.problems(cfg)
    if found:
        print("Not ready yet:")
        for p in found:
            print("  -", p)
        print("\nFix these in .env, then run again. Details: docs/PHONE_SETUP.md")
        sys.exit(1)
    data = analysis.load(cfg.data_path)
    print(f"Gifting Pulse phone line listening on port {cfg.port}.")
    print(f"Allowed callers: {len(cfg.allowed_callers)}. Point your tunnel at http://localhost:{cfg.port}.")
    uvicorn.run(server.create_app(cfg, data=data, warm=True), host="127.0.0.1", port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
