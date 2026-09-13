"""Prompt without echo and pipe the credential directly to the read-only browser check."""
import getpass
from pathlib import Path
import subprocess

password = getpass.getpass("Web password (not echoed): ")
result = subprocess.run(["node", str(Path(__file__).with_name("admin_live_check.cjs"))], input=password, text=True)
raise SystemExit(result.returncode)
