"""Root conftest for the Stratum test suite.

Ensures the repository root is importable so `import src...` and
`import config` resolve regardless of how pytest is invoked.

Note: the sandbox-only broken plugin workaround lives in pytest.ini
(`addopts = -p no:libtmux`), because pytest.ini is read before plugins
are autoloaded, whereas this file is not.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
