"""Fails if anything that looks like a secret or credential is present in the repo.
Run before every commit and before any git push of this repo."""
import re
import sys
from pathlib import Path

SUSPICIOUS_NAME_PATTERNS = ["*.env", "*.env.*", "*secret*", "*credential*", "*private_key*"]
SUSPICIOUS_CONTENT_PATTERNS = [
    re.compile(r"PRIVATE_KEY", re.IGNORECASE),
    re.compile(r"_KEY\s*=\s*['\"][A-Za-z0-9]{16,}"),
    re.compile(r"0x[0-9a-fA-F]{64}"),
    re.compile(r"api[_-]?secret", re.IGNORECASE),
]
# Includes the scanner's own filenames: this script and its test module
# necessarily contain the string "secret"/"PRIVATE_KEY" as literal detection
# pattern text/name, not an actual secret, so they must not self-flag.
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "check_no_secrets.py", "test_no_secrets.py"}


def find_suspicious_paths(root: str) -> list[str]:
    violations = []
    root_path = Path(root)
    for path in root_path.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        for pattern in SUSPICIOUS_NAME_PATTERNS:
            if path.match(pattern):
                violations.append(f"suspicious filename: {path.relative_to(root_path)}")
                break
        try:
            text = path.read_text(errors="ignore")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in SUSPICIOUS_CONTENT_PATTERNS:
            if pattern.search(text):
                violations.append(f"suspicious content ({pattern.pattern}) in: {path.relative_to(root_path)}")
    return violations


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    found = find_suspicious_paths(root)
    if found:
        print("SECRET SCAN FAILED:")
        for v in found:
            print(f"  - {v}")
        sys.exit(1)
    print("Secret scan clean.")
