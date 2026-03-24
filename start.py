import subprocess
import sys
import os

# Bot now runs via webhook inside app.py (or falls back to polling).
# No separate bot subprocess needed.
port = os.getenv("PORT", "8000")
subprocess.run([
    sys.executable, "-m", "uvicorn", "app:app",
    "--host", "0.0.0.0", "--port", port
])
