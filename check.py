# SPDX-License-Identifier: GPL-3.0-only
"""Quality guard: keys in repository files, ruff (standard), vulture (dead code), pylint (duplicates), tests with
at least 80% logic coverage.

Run: .venv\\Scripts\\python.exe check.py
"""

import subprocess
import sys

# Looks like keys and tokens: OpenRouter and OpenAI, GitHub, AWS, private keys, a header with a token
KEYS = (
    r"sk-or-v1-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|Bearer [A-Za-z0-9._-]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}"
)

CHECKS = [
    ("ruff", ["-m", "ruff", "check", "knotes", "tests", "check.py", "build.py", "launch.pyw"]),
    ("dead code", ["-m", "vulture", "knotes", "--min-confidence", "80"]),
    ("duplicates", ["-m", "pylint", "--disable=all", "--enable=duplicate-code", "--min-similarity-lines=6", "knotes"]),
    ("tests", ["-m", "coverage", "run", "-m", "unittest", "discover", "-s", "tests", "-t", "."]),
    ("coverage", ["-m", "coverage", "report"]),
]


def keys() -> list[str]:
    """Repository files (including new, not yet added ones, except ignored) that contain something like a key.
    Only file names are printed: the key itself never reaches the output."""
    result = subprocess.run(
        ["git", "grep", "--untracked", "-I", "-l", "-E", KEYS],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout.split()


def main() -> int:
    sys.stdout.reconfigure(errors="replace")  # a Russian console has no code page for some test output
    found = keys()
    print(f"== keys: {'FAILED, something like a key in ' + ', '.join(found) if found else 'ok'}")
    failed = ["keys"] if found else []
    for name, arguments in CHECKS:
        result = subprocess.run(
            [sys.executable, *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        output = (result.stdout + result.stderr).strip()
        print(f"== {name}: {'ok' if result.returncode == 0 else 'FAILED'}")
        if result.returncode or name == "coverage":
            print(output[-3000:])
        if result.returncode:
            failed.append(name)
    print("Result:", "all checks passed" if not failed else f"failed: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
