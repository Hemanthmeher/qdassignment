import os
import subprocess
import sys

# Ensure PaySwift strictly listens on port 8081
os.environ["PORT"] = "8081"

# Run PaySwift sandbox
subprocess.run([sys.executable, "payswift/server.pyc"])
