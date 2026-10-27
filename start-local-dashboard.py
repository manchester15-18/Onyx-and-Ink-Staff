"""Start the Onyx & Ink dashboard locally with Tailscale access.

Usage:
    python start-local-dashboard.py

This binds dashboard.py to 0.0.0.0:8765 so it's reachable on the LAN
and via Tailscale. Add your Tailscale IP to ONYX_ALLOWED_HOSTS in .env.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def main():
    # Ensure we run from the project root
    os.chdir(ROOT)

    # Set defaults for local + Tailscale access
    os.environ.setdefault("ONYX_BIND_HOST", "0.0.0.0")
    os.environ.setdefault("ONYX_CLOUD_MODE", "false")
    os.environ.setdefault("PORT", "8765")

    print("=== Onyx & Ink Dashboard (Local + Tailscale) ===")
    print(f"Project root: {ROOT}")
    print(f"Bind host: {os.environ['ONYX_BIND_HOST']}")
    print(f"Port: {os.environ['PORT']}")
    print("Dashboard will be reachable at:")
    print("  http://localhost:8765")
    print("  http://10.0.0.156:8765   (LAN)")
    print("  http://<tailscale-ip>:8765 (Tailscale)")
    print()
    print("IMPORTANT: Add your Tailscale IP to ONYX_ALLOWED_HOSTS in .env")
    print("           before accessing remotely, or requests will be rejected.")
    print()

    # Start dashboard
    print("Starting dashboard.py...")
    try:
        subprocess.run([sys.executable, "dashboard.py"], check=True)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    except subprocess.CalledProcessError as e:
        print(f"\nDashboard exited with code {e.returncode}")
        sys.exit(e.returncode)

if __name__ == "__main__":
    main()
