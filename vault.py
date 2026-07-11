"""Git mechanics for syncing daily logs to a private GitHub vault repo over SSH.

Authenticates with a deploy key (VAULT_DEPLOY_KEY, newlines escaped as \\n) rather
than a personal access token, scoped to a single repo (VAULT_REPO_SSH_URL). No DB
knowledge lives here — callers pass rendered file content in, this module only
knows how to clone/write/commit/push.
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # module is self-sufficient regardless of import order relative to main.py

logger = logging.getLogger("time_steward.vault")

VAULT_REPO_SSH_URL = os.environ.get("VAULT_REPO_SSH_URL")
VAULT_DEPLOY_KEY = os.environ.get("VAULT_DEPLOY_KEY")

# Pinned via `ssh-keyscan -t ed25519 github.com` — avoids StrictHostKeyChecking=no
# (which would accept any host, i.e. no MITM protection) while needing no prompt.
# Matching is by the name given on the command line (github.com), not the
# HostName override below, so this stays correct even though we connect
# elsewhere physically — see https://docs.github.com/en/authentication/
# troubleshooting-ssh/using-ssh-over-the-https-port
GITHUB_KNOWN_HOST = (
    "github.com ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
)

COMMIT_AUTHOR_NAME = "Time Steward Bot"
COMMIT_AUTHOR_EMAIL = "time-steward-bot@users.noreply.github.com"


def is_configured() -> bool:
    return bool(VAULT_REPO_SSH_URL and VAULT_DEPLOY_KEY)


def _write_ssh_key(tmp_dir: Path) -> Path:
    key_path = tmp_dir / "deploy_key"
    key_path.write_text(VAULT_DEPLOY_KEY.replace("\\n", "\n"))
    key_path.chmod(0o600)
    return key_path


def _write_known_hosts(tmp_dir: Path) -> Path:
    known_hosts_path = tmp_dir / "known_hosts"
    known_hosts_path.write_text(GITHUB_KNOWN_HOST)
    return known_hosts_path


def _run_git(args: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def push_files(files: list[tuple[str, str]], commit_message: str) -> int:
    """Clone the vault repo fresh, write one or more files, commit, push.

    Returns the number of changed files (0 if content was already identical —
    no-op commit). Raises on any failure; callers should catch and log rather
    than let a sync failure crash the bot.
    """
    if not is_configured():
        raise RuntimeError("VAULT_REPO_SSH_URL / VAULT_DEPLOY_KEY not set")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        key_path = _write_ssh_key(tmp_dir)
        known_hosts_path = _write_known_hosts(tmp_dir)

        env = {
            **os.environ,
            # Many cloud platforms (Railway included) block outbound port 22.
            # GitHub's SSH service is also reachable over 443 for exactly this
            # reason — tunnel through that instead of the default port.
            "GIT_SSH_COMMAND": (
                f"ssh -i {key_path} -o UserKnownHostsFile={known_hosts_path} "
                "-o IdentitiesOnly=yes -o HostName=ssh.github.com -o Port=443 "
                "-o HostKeyAlias=github.com -o ConnectTimeout=10"
            ),
        }

        repo_dir = tmp_dir / "repo"
        _run_git(["clone", VAULT_REPO_SSH_URL, str(repo_dir)], cwd=tmp_dir, env=env)

        for relative_path, content in files:
            file_path = repo_dir / relative_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)
            _run_git(["add", relative_path], cwd=repo_dir, env=env)

        status = _run_git(["status", "--porcelain"], cwd=repo_dir, env=env)
        if not status.stdout.strip():
            logger.info("vault: no changes, skipping commit")
            return 0
        changed_count = len(status.stdout.strip().splitlines())

        _run_git(
            [
                "-c",
                f"user.name={COMMIT_AUTHOR_NAME}",
                "-c",
                f"user.email={COMMIT_AUTHOR_EMAIL}",
                "commit",
                "-m",
                commit_message,
            ],
            cwd=repo_dir,
            env=env,
        )
        _run_git(["push"], cwd=repo_dir, env=env)
        logger.info("vault: pushed %d file(s) — %s", changed_count, commit_message)
        return changed_count


def push_file(relative_path: str, content: str, commit_message: str) -> int:
    return push_files([(relative_path, content)], commit_message)
