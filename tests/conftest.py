import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "github" / "rerequest"))

os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("GITHUB_TOKEN", "testtoken")
