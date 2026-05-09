import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "github" / "rerequest"))

# Set unconditionally so tests are deterministic even when the runner's
# environment already has these vars (common in CI). The tests sign with
# "testsecret" and assume "testtoken"; honouring an inherited value would
# cause signature mismatches.
os.environ["GITHUB_WEBHOOK_SECRET"] = "testsecret"
os.environ["GITHUB_TOKEN"] = "testtoken"
