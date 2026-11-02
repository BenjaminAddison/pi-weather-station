"""Make pytest import the package from this checkout, not an editable install elsewhere."""

import sys
from pathlib import Path

_APP_DIR = str(Path(__file__).resolve().parent)
if sys.path[:1] != [_APP_DIR]:
    sys.path.insert(0, _APP_DIR)
for _name in [m for m in sys.modules if m == "weatherpi_companion" or m.startswith("weatherpi_companion.")]:
    del sys.modules[_name]
