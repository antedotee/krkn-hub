"""pytest config — add `.docs-sync/` to sys.path so tests can import top-level modules."""
import sys
from pathlib import Path

DOCS_SYNC_ROOT = Path(__file__).parent.resolve()
if str(DOCS_SYNC_ROOT) not in sys.path:
    sys.path.insert(0, str(DOCS_SYNC_ROOT))
