import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# Isolated data + storage so tests never touch your real index or question log.
_tmp = Path(tempfile.mkdtemp(prefix="hr_assist_test_"))
(_tmp / "data").mkdir()
shutil.copy(ROOT / "data" / "Qorvexa_HR_Handbook.pdf", _tmp / "data")
# A second, restricted copy that only managers may read.
shutil.copy(ROOT / "data" / "Qorvexa_HR_Handbook.pdf", _tmp / "data" / "Manager_Playbook.pdf")
os.environ.update({
    "DATA_DIR": str(_tmp / "data"),
    "STORAGE_DIR": str(_tmp / "storage"),
    "AUTH_MODE": "dev",
    "ANTHROPIC_API_KEY": "",  # passage mode: no network calls in tests
})
