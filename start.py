import subprocess
import sys
import os

# Запускает бота и сервер одновременно
port = os.getenv("PORT", "8000")
subprocess.Popen([sys.executable, "bot.py"])
subprocess.run([sys.executable, "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", port])
