# Temporary local-gate setup — injects backend venv site-packages into toolchain python
import sys
_backend_venv = "/tmp/blueprint-backend-venv/lib/python3.12/site-packages"
if _backend_venv not in sys.path:
    sys.path.insert(0, _backend_venv)
