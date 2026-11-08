"""Create local credentials without overwriting existing configuration or printing secrets."""

import os
import secrets
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    destination = root / ".env"
    if destination.exists():
        print(".env already exists; left unchanged.")
        return
    template = (root / ".env.example").read_text(encoding="utf-8")
    lines = []
    for line in template.splitlines():
        if line.endswith("=__GENERATE__"):
            line = line.replace("__GENERATE__", secrets.token_urlsafe(36))
        lines.append(line)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print("Created private .env. Administrator credentials are stored there.")


if __name__ == "__main__":
    main()
