"""Run BEFORE the first `git push`:   python scripts/prepush_check.py
Checks what git WOULD commit (tracked files + new files that are not ignored) for things that must never reach GitHub: .env, the data
folders, secrets in file contents, files too big for GitHub. The report names the problem and the file - it never prints the secret itself.
Standard library only. Exit code 0 = safe to push, 1 = fix the listed problems first."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".idea"}
SKIP_EXT = {".zip", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".xlsx", ".pyc", ".ico", ".woff", ".woff2", ".gz"}
# every pattern works on ONE line (no \s that could run into the next line)
SECRETS = {"Google API key": r"AIza[0-9A-Za-z_\-]{35}",
           "GitHub token": r"gh[pousr]_[0-9A-Za-z]{30,}",
           "private key": r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----",
           "service-account JSON": r'"private_key"[ \t]*:',
           "admin password hash": r"pbkdf2_sha256\$\d+\$[0-9a-f]{32}\$[0-9a-f]{64}",
           "hard-coded password": r"(?i)\b(?:password|passwd|app_password)[ \t]*=[ \t]*[\"'][A-Za-z0-9 ]{12,}[\"']",
           "token inside a URL": r"https://[^\s/@:]+:[A-Za-z0-9_\-]{20,}@"}
BIG = 20 * 1024 * 1024                      # data belongs on the data branch; GitHub itself rejects 100 MB and warns at 50 MB


def candidates(root: Path) -> list[str]:
    """Files git would commit: tracked + untracked-not-ignored. Falls back to a plain walk when this is not a git repository."""
    try:
        r = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return sorted(set(l for l in r.stdout.splitlines() if l))
    except (OSError, subprocess.SubprocessError):
        pass
    ign = (root / ".gitignore").read_text(encoding="utf-8").splitlines() if (root / ".gitignore").exists() else []
    skip = {".env"} if ".env" in [l.strip() for l in ign] else set()
    out = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if p.is_file() and not any(part in SKIP_DIRS for part in rel.parts) and rel.as_posix() not in skip \
                and not (rel.parts[0].startswith("data_store") and any(l.strip().startswith("data_store") for l in ign)):
            out.append(rel.as_posix())
    return sorted(out)


def scan(root: Path = ROOT) -> list[str]:
    root, problems, files = Path(root), [], None
    files = candidates(root)
    envs = [f for f in files if Path(f).name == ".env"]
    for f in envs:
        problems.append(f".env file would be committed: {f} - add `.env` to .gitignore (it holds secrets)")
    stores = sorted({f.split("/")[0] for f in files if f.split("/")[0] in ("data_store", "data_store_test")})
    for s in stores:
        problems.append(f"{s} folder would be committed - add `{s}/` to .gitignore (the data lives on the separate data branch)")
    for rel in files:
        p = root / rel
        if not p.is_file() or p.suffix.lower() in SKIP_EXT or rel in envs:
            continue
        size = p.stat().st_size
        if size > BIG:
            problems.append(f"{rel}: {size // 1048576} MB - too big for the code branch (GitHub rejects 100 MB, warns at 50 MB); keep data out of it")
            continue
        if size > 5 * 1048576:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, rx in SECRETS.items():
            if re.search(rx, text):
                problems.append(f"{rel}: contains a {label}")
    return problems


def main() -> int:
    probs = scan()
    print("OK - nothing sensitive found; safe to push." if not probs else "FIX BEFORE PUSHING:\n  " + "\n  ".join(probs))
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main())
