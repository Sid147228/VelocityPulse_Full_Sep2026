import re
from datetime import datetime
from version import __version__, __codename__

CHANGELOG_FILE = "CHANGELOG.md"
VERSION_FILE = "version.py"

def bump_version(part="patch", notes=""):
    major, minor, patch = map(int, __version__.split("."))
    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    else:
        patch += 1

    new_version = f"{major}.{minor}.{patch}"
    build = datetime.now().strftime("%Y.%m.%d")

    # Update version.py
    with open(VERSION_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    content = re.sub(r'__version__ = ".*"', f'__version__ = "{new_version}"', content)
    content = re.sub(r'__build__ = ".*"', f'__build__ = "{build}"', content)
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(content)

    # Append to CHANGELOG.md
    with open(CHANGELOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n## v{new_version} ({build})\n- {notes or 'No notes provided.'}\n")

    print(f"Bumped to {new_version} ({build})")

if __name__ == "__main__":
    bump_version("patch", notes="Quick bug fix")
