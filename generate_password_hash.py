#!/usr/bin/env python3
"""
Run this once, from inside the pcflip folder (next to docker-compose.yml
and the `data` folder), before your first `docker compose up`. It writes
`data/auth_config.json` with your login credentials.

Deliberately NOT env vars or a .env file: Docker Compose parses "$" as a
variable substitution wherever it reads config, which silently corrupts a
bcrypt hash like $2b$12$.... A plain file inside ./data (already
bind-mounted into the container) sidesteps that entirely -- Compose never
looks at its contents.

Usage:
    pip install bcrypt --break-system-packages   # if not already installed
    python3 generate_password_hash.py
"""
import getpass
import json
import os
import secrets

import bcrypt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
AUTH_PATH = os.path.join(DATA_DIR, "auth_config.json")


def main():
    username = input("Choose an admin username: ").strip()
    while True:
        password = getpass.getpass("Choose a password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords didn't match, try again.\n")
            continue
        if len(password) < 8:
            print("Use at least 8 characters.\n")
            continue
        break

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    secret_key = secrets.token_hex(32)

    if os.path.exists(AUTH_PATH):
        answer = input(f"\n{AUTH_PATH} already exists. Overwrite it? [y/N] ").strip().lower()
        if answer != "y":
            print("Left the existing file untouched.")
            return

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(AUTH_PATH, "w") as f:
        json.dump({
            "username": username,
            "password_hash": password_hash,
            "secret_key": secret_key,
        }, f, indent=2)
    os.chmod(AUTH_PATH, 0o600)  # not world-readable

    print(f"\nWrote {AUTH_PATH}")
    print("Nothing to paste anywhere -- docker-compose.yml doesn't need to know")
    print("about your credentials at all. Now run: docker compose up -d --build")


if __name__ == "__main__":
    main()
