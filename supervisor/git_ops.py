import os
import subprocess
import logging
from pathlib import Path
from typing import Tuple, Optional

log = logging.getLogger(__name__)

BRANCH_DEV = "ouroboros"
BRANCH_STABLE = "ouroboros-stable"

REPO_DIR: Optional[Path] = None
DRIVE_ROOT: Optional[Path] = None
REMOTE_URL: Optional[str] = None


def init(repo_dir: Path, drive_root: Path, remote_url: str,
          branch_dev: str = BRANCH_DEV, branch_stable: str = BRANCH_STABLE):
    global REPO_DIR, DRIVE_ROOT, REMOTE_URL
    REPO_DIR = repo_dir
    DRIVE_ROOT = drive_root
    REMOTE_URL = remote_url


def _git(*args: str, cwd: Optional[Path] = None, check: bool = True) -> Tuple[int, str, str]:
    """Run git command in specified directory."""
    cmd = ['git'] + list(args)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd or REPO_DIR),
        text=True,
        capture_output=True,
        check=check,
    )
    return proc.returncode, proc.stdout, proc.stderr


def ensure_repo_present() -> None:
    """Make sure repo exists and is properly initialized."""
    assert REPO_DIR and DRIVE_ROOT, "git_ops not initialized"
    REPO_DIR.mkdir(parents=True, exist_ok=True)
    
    if not (REPO_DIR / '.git').exists():
        log.info("Initializing new git repo at %s", REPO_DIR)
        _git('init', cwd=REPO_DIR)
        
        # Add initial empty commit so we have HEAD
        (REPO_DIR / 'README.md').write_text('# Ouroboros Agent\n')
        _git('add', 'README.md', cwd=REPO_DIR)
        _git('commit', '-m', 'Initial commit', cwd=REPO_DIR)
        
    # Make sure origin is set up
    _, out, _ = _git('remote', '-v', cwd=REPO_DIR, check=False)
    if 'origin' not in out:
        log.info("Setting up origin remote:", REMOTE_URL)
        _git('remote', 'add', 'origin', REMOTE_URL, cwd=REPO_DIR)


def checkout_and_reset(branch: str) -> Tuple[bool, str]:
    """Checkout branch and hard reset to origin's state. Returns (success, message)."""
    assert REPO_DIR, "git_ops not initialized"

    # Try to checkout branch
    code, _, err = _git('checkout', branch, cwd=REPO_DIR, check=False)
    if code != 0:
        return False, f"Checkout failed: {err.strip()[:200]}"

    # Try to reset to origin
    code, _, err = _git('reset', '--hard', f"origin/{branch}", cwd=REPO_DIR, check=False)
    if code != 0:
        # If origin doesn't have branch yet (first push), just do soft reset
n        code, _, err = _git('reset', '--soft', 'HEAD', cwd=REPO_DIR, check=False)
        if code != 0:
            return False, f"Reset failed: {err.strip()[:200]}"

    return True, ""


def sync_runtime_dependencies() -> None:
    """Make sure worker processes use latest code for queue snapshot handling."""
    assert DRIVE_ROOT, "git_ops not initialized"
    
    # Late import to avoid circular dependency
    from supervisor.queue import persist_queue_snapshot
    persist_queue_snapshot(reason="pre-sync")
    
    # Late import to avoid circular dependency
    from supervisor.workers import kill_workers, spawn_workers
    kill_workers()
    spawn_workers(1)  # Just enough to handle state


def safe_restart(reason: str, unsynced_policy: str = "rescue") -> Tuple[bool, str]:
    """Safe restart routine that handles queue snapshots and branch issues.

    unsynced_policy:
        'rescue' - try to recover state before restart
        'reset'  - hard reset to origin (lose local changes)
    """
    assert REPO_DIR and DRIVE_ROOT, "git_ops not initialized"
    current_branch = None
    
    # Late imports to avoid circular dependency
    from supervisor.state import load_state, save_state
    current_branch = load_state().get('current_branch', BRANCH_DEV)

    # Pre-restart cleanup
    from supervisor.queue import persist_queue_snapshot
    persist_queue_snapshot(reason="pre-restart")

    # Try normal checkout
    ok, msg = checkout_and_reset(current_branch)
    if not ok:
        if unsynced_policy == "reset":
            log.warning("Forcing hard reset due to %s", msg)
            _git('reset', '--hard', 'origin/%s' % current_branch, cwd=REPO_DIR)
            _git('clean', '-fd', cwd=REPO_DIR)
        elif unsynced_policy == "rescue":
            # Try to create branch from dev if missing
            if current_branch == BRANCH_STABLE:
                log.warning("Stable branch missing — creating from dev")
                _git('checkout', '-B', BRANCH_STABLE, BRANCH_DEV, cwd=REPO_DIR)
                _git('push', '-u', 'origin', BRANCH_STABLE, cwd=REPO_DIR)
            else:
                return False, f"Cannot rescue branch {current_branch}: {msg}"

    # Update state with current branch/sha
    _, sha, _ = _git('rev-parse', 'HEAD', cwd=REPO_DIR)
    st = load_state()
    st['current_branch'] = current_branch
    st['current_sha'] = sha.strip()
    save_state(st)

    return True, "Restarted: %s (%.7s)" % (current_branch, sha.strip())


def import_test() -> bool:
    """Smoke test for import chain (used by test_smoke.py)."""
    try:
        # Absolute imports work
        from supervisor.state import load_state
        from supervisor.queue import persist_queue_snapshot
        return True
    except ImportError as e:
        log.error("Import test failed: %s", e)
        return False