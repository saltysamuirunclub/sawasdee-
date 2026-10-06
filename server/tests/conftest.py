import os
import tempfile

# Isolate every test run from real data/secrets before the app is imported.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ["LOG_DIR"] = os.path.join(_tmp, "logs")
os.environ["DISABLE_SCHEDULER"] = "1"
