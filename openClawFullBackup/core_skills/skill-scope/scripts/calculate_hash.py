#!/usr/bin/env python3
import os
import sys
import hashlib
from pathlib import Path

IGNORE_DIRS = {".github", ".git", "__pycache__", "tests"}
IGNORE_FILES = {
    "LICENSE.txt",
    "LICENSE",
    "README.md",
    "_meta.json",
    "pytest.ini",
    "requirements.txt",
    ".gitignore",
    ".env.example",
}

def calculate_file_sha256(file_path: str) -> str:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    except Exception as e:
        return f"Error: {e}"

def calculate_string_sha256(input_string: str) -> str:
    return hashlib.sha256(input_string.encode("utf-8")).hexdigest()

def calculate_project_hash(project_path: str) -> str:
    file_hashes = []

    def walk_dir(dir_path: str):
        dirs = []
        files = []

        for entry in os.scandir(dir_path):
            if entry.is_dir():
                if entry.name not in IGNORE_DIRS:
                    dirs.append(entry.name)
            elif entry.is_file():
                if entry.name not in IGNORE_FILES:
                    files.append(entry.name)

        for filename in files:
            fpath = os.path.join(dir_path, filename)
            file_hashes.append(calculate_file_sha256(fpath))

        for d in dirs:
            walk_dir(os.path.join(dir_path, d))

    walk_dir(project_path)
    file_hashes.sort()
    return calculate_string_sha256("\n".join(file_hashes))

def main():
    if len(sys.argv) != 2:
        print("Usage: python calculate_hash.py <skill_directory>", file=sys.stderr)
        sys.exit(1)

    target_dir = sys.argv[1]

    try:
        result_hash = calculate_project_hash(target_dir)
        if result_hash:
            print(result_hash)
        else:
            print("Error: No valid files found in directory to calculate hash.", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"Error: Calculation failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()