"""Makes `import weatherpi` work in pi/tests and forces fake hardware with a temp root."""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("WEATHERPI_FAKE", "1")
os.environ.setdefault("WEATHERPI_ROOT", tempfile.mkdtemp(prefix="weatherpi-root-"))
sys.path.insert(0, str(Path(__file__).parent))
