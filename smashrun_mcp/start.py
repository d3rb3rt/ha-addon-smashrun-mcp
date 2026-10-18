#!/usr/bin/env python3
"""Startskript der Home-Assistant-App: liest die App-Optionen und startet den HTTP-Server."""
import json
import os
import sys

OPTIONS_FILE = os.environ.get("OPTIONS_FILE", "/data/options.json")


def main() -> None:
    try:
        with open(OPTIONS_FILE, encoding="utf-8") as f:
            opts = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"Optionen konnten nicht gelesen werden ({OPTIONS_FILE}): {e}")

    token = (opts.get("access_token") or "").strip()
    secret = (opts.get("secret_path") or "").strip()
    if not token:
        sys.exit("Option 'access_token' fehlt (Smashrun-Token aus dem API Explorer).")
    if not secret:
        sys.exit("Option 'secret_path' fehlt (mindestens 24 Zeichen, z. B. mit 'openssl rand -hex 24' erzeugen).")

    cidrs = [c.strip() for c in (opts.get("allowed_cidrs") or []) if c and c.strip()]
    os.environ["SMASHRUN_ACCESS_TOKEN"] = token
    os.environ["SMASHRUN_SECRET_PATH"] = secret
    os.environ["SMASHRUN_ALLOWED_CIDRS"] = ",".join(cidrs)
    os.environ["SMASHRUN_ALLOW_WRITE"] = "1" if opts.get("allow_write", True) else "0"
    os.environ.setdefault("HOST", "0.0.0.0")
    os.environ.setdefault("PORT", "8099")

    import http_app  # nach dem Setzen der Umgebung importieren, server.py liest sie beim Import

    http_app.main()


if __name__ == "__main__":
    main()
