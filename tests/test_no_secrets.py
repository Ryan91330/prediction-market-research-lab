from pathlib import Path

from check_no_secrets import find_suspicious_paths

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_repo_has_no_suspicious_files():
    violations = find_suspicious_paths(str(REPO_ROOT))
    assert violations == [], f"Found suspicious content: {violations}"
