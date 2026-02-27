'''Supervisor — Git operations.

Clone, checkout, reset, rescue snapshots, dependency sync, import test.
'''

from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

from supervisor.state import (
    load_state, save_state, append_jsonl, atomic_write_text,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level config (set via init())
# ---------------------------------------------------------------------------
REPO_DIR: pathlib.Path = pathlib.Path("/content/ouroboros_repo")
DRIVE_ROOT: pathlib.Path = pathlib.Path("/content/drive/MyDrive/Ouroboros")
REMOTE_URL: str = ""
BRANCH_DEV: str = "ouroboros"
BRANCH_STABLE: str = "ouroboros-stable"


def init(repo_dir: pathlib.Path, drive_root: pathlib.Path, remote_url: str,
         branch_dev: str = "ouroboros", branch_stable: str = "ouroboros-stable") -> None:
    global REPO_DIR, DRIVE_ROOT, REMOTE_URL, BRANCH_DEV, BRANCH_STABLE
    REPO_DIR = repo_dir
    DRIVE_ROOT = drive_root
    REMOTE_URL = remote_url
    BRANCH_DEV = branch_dev
    BRANCH_STABLE = branch_stable


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git_capture(cmd: List[str]) -> Tuple[int, str, str]:
    r = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, text=True)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()

def ensure_repo_present() -> None:
    if not (REPO_DIR / ".git").exists():
        subprocess.run(["rm", "-rf", str(REPO_DIR)], check=False)
        subprocess.run(["git", "clone", REMOTE_URL, str(REPO_DIR)], check=True)
    else:
        subprocess.run(["git", "remote", "set-url", "origin", REMOTE_URL],
                        cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "config", "user.name", "Ouroboros"], cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "config", "user.email", "ouroboros@users.noreply.github.com"],
                    cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "fetch", "origin"], cwd=str(REPO_DIR), check=True)


# ---------------------------------------------------------------------------
# Repo sync state collection
# ---------------------------------------------------------------------------

def _collect_repo_sync_state() -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "current_branch": "unknown",
        "dirty_lines": [],
        "unpushed_lines": [],
        "warnings": [],
    }

    rc, branch, err = git_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if rc == 0 and branch:
        state["current_branch"] = branch
    elif err:
        state["warnings"].append(f"branch_error:{err}")

    rc, dirty, err = git_capture(["git", "status", "--porcelain"])
    if rc == 0 and dirty:
        state["dirty_lines"] = [ln for ln in dirty.splitlines() if ln.strip()]
    elif rc != 0 and err:
        state["warnings"].append(f"status_error:{err}")

    upstream = ""
    rc, up, err = git_capture(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if rc == 0 and up:
        upstream = up
    else:
        current_branch = str(state.get("current_branch") or "")
        if current_branch not in ("", "HEAD", "unknown"):
            upstream = f"origin/{current_branch}"
        elif err:
            state["warnings"].append(f"upstream_error:{err}")

    if upstream:
        rc, unpushed, err = git_capture(["git", "log", "--oneline", f"{upstream}..HEAD"])
        if rc == 0 and unpushed:
            state["unpushed_lines"] = [ln for ln in (unpushed.splitlines() or []) if ln.strip()]
        elif rc != 0 and err:
            state["warnings"].append(f"unpushed_error:{err}")

    return state


def _copy_untracked_for_rescue(dst_root: pathlib.Path, max_files: int = 200,
                                max_total_bytes: int = 12_000_000) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "copied_files": 0, "skipped_files": 0, "copied_bytes": 0, "truncated": False,
    }
    rc, txt, err = git_capture(["git", "ls-files", "--others", "--exclude-standard"])
    if rc != 0:
        out["error"] = err or "git ls-files failed"
        return out

    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    if not lines:
        return out

    dst_root.mkdir(parents=True, exist_ok=True)
    for rel in lines:
        if out["copied_files"] >= max_files:
            out["truncated"] = True
            break
        src = (REPO_DIR / rel).resolve()
        try:
            src.relative_to(REPO_DIR.resolve())
        except Exception:
            out["skipped_files"] += 1
            continue
        if not src.exists() or not src.is_file():
            out["skipped_files"] += 1
            continue
        try:
            size = int(src.stat().st_size)
        except Exception:
            out["skipped_files"] += 1
            continue
        if (out["copied_bytes"] + size) > max_total_bytes:
            out["truncated"] = True
            break
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            out["copied_files"] += 1
            out["copied_bytes"] += size
        except Exception:
            out["skipped_files"] += 1
    return out


def _create_rescue_snapshot(branch: str, reason: str,
                             repo_state: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.datetime.now(datetime.timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")
    rescue_dir = DRIVE_ROOT / "archive" / "rescue" / f"{ts}_{uuid.uuid4().hex[:8]}"
    rescue_dir.mkdir(parents=True, exist_ok=True)

    info: Dict[str, Any] = {
        "ts": now.isoformat(),
        "target_branch": branch,
        "reason": reason,
        "current_branch": repo_state.get("current_branch"),
        "dirty_count": len(repo_state.get("dirty_lines") or []),
        "unpushed_count": len(repo_state.get("unpushed_lines") or []),
        "warnings": list(repo_state.get("warnings") or []),
        "path": str(rescue_dir),
    }

    rc_status, status_txt, _ = git_capture(["git", "status", "--porcelain"])
    if rc_status == 0:
        atomic_write_text(rescue_dir / "status.porcelain.txt",
                          status_txt + ("\n" if status_txt else ""))

    rc_diff, diff_txt, diff_err = git_capture(["git", "diff", "--binary", "HEAD"])
    if rc_diff == 0:
        atomic_write_text(rescue_dir / "changes.diff",
                          diff_txt + ("\n" if diff_txt else ""))
    else:
        info["diff_error"] = diff_err or "git diff failed"

    untracked_meta = _copy_untracked_for_rescue(rescue_dir / "untracked")
    info["untracked"] = untracked_meta

    unpushed_lines = [ln for ln in (repo_state.get("unpushed_lines") or []) if str(ln).strip()]
    if unpushed_lines:
        atomic_write_text(rescue_dir / "unpushed_commits.txt",
                          "\n".join(unpushed_lines) + "\n")

    atomic_write_text(rescue_dir / "rescue_meta.json",
                      json.dumps(info, ensure_ascii=False, indent=2))
    return info


# ---------------------------------------------------------------------------
# Checkout + reset
# ---------------------------------------------------------------------------

def checkout_and_reset(branch: str, reason: str = "unspecified",
                       unsynced_policy: str = "ignore") -> Tuple[bool, str]:
    rc, _, err = git_capture(["git", "fetch", "origin"])
    if rc != 0:
        msg = f"git fetch failed: {err or 'unknown error'}"
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "reset_fetch_failed",
                "target_branch": branch, "reason": reason, "error": msg,
            },
        )
        return False, msg

    policy = str(unsynced_policy or "ignore").strip().lower()
    if policy not in {"ignore", "block", "rescue_and_block", "rescue_and_reset"}:
        policy = "ignore"

    repo_state = _collect_repo_sync_state()
    dirty_lines = list(repo_state.get("dirty_lines") or [])
    unpushed_lines = list(repo_state.get("unpushed_lines") or [])
    rescue_info: Dict[str, Any] = {}

    if dirty_lines or unpushed_lines:
        if policy in {"rescue_and_block", "rescue_and_reset"}:
            try:
                rescue_info = _create_rescue_snapshot(
                    branch=branch, reason=reason, repo_state=repo_state)
            except Exception as e:
                rescue_info = {"error": repr(e)}
        bits: List[str] = []
        if unpushed_lines:
            bits.append(f"unpushed={len(unpushed_lines)}")
        if dirty_lines:
            bits.append(f"dirty={len(dirty_lines)}")
        detail = ", ".join(bits) if bits else "unsynced"
        rescue_suffix = ""
        rescue_path = str(rescue_info.get("path") or "").strip()
        if rescue_path:
            rescue_suffix = f" Rescue saved to {rescue_path}."
        elif policy in {"rescue_and_block", "rescue_and_reset"} and rescue_info.get("error"):
            rescue_suffix = f" Rescue failed: {rescue_info.get('error')}"

        if policy in {"block", "rescue_and_block"}:
            msg = f"Reset blocked ({detail}) to protect local changes.{rescue_suffix}"
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "reset_blocked_unsynced_state",
                    "target_branch": branch, "reason": reason, "policy": policy,
                    "current_branch": repo_state.get("current_branch"),
                    "dirty_count": len(dirty_lines),
                    "unpushed_count": len(unpushed_lines),
                    "dirty_preview": dirty_lines[:20],
                    "unpushed_preview": unpushed_lines[:20],
                    "warnings": list(repo_state.get("warnings") or []),
                    "rescue": rescue_info,
                },
            )
            return False, msg

    # Create stable branch if missing
    if branch == BRANCH_STABLE:
        rc_check = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", BRANCH_STABLE],
            cwd=str(REPO_DIR),
            capture_output=True,
        ).returncode
        if rc_check != 0:
            log.warning("Stable branch %s missing - creating from dev", BRANCH_STABLE)
            try:
                # Ensure we're on dev branch
                subprocess.run(["git", "checkout", BRANCH_DEV], cwd=str(REPO_DIR), check=True)
                # Push dev to create stable
                subprocess.run(["git", "push", "origin", f"{BRANCH_DEV}:{BRANCH_STABLE}"], cwd=str(REPO_DIR), check=True)
                log.info("Created stable branch %s from %s", BRANCH_STABLE, BRANCH_DEV)
            except Exception as e:
                log.error("Failed to create stable branch: %s", repr(e))
                return False, "Failed to create stable branch"

    # For dev branch, ensure it exists on remote and locally
    if branch == BRANCH_DEV:
        # Verify remote has dev branch
        rc_verify_remote = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", BRANCH_DEV],
            cwd=str(REPO_DIR),
            capture_output=True,
        ).returncode
        if rc_verify_remote != 0:
            log.warning("Dev branch %s not found on remote - creating and pushing", BRANCH_DEV)
            try:
                # Ensure we're on main branch
                subprocess.run(["git", "checkout", "main"], cwd=str(REPO_DIR), check=True)
                # Create local dev branch from main
                subprocess.run(["git", "checkout", "-b", BRANCH_DEV], cwd=str(REPO_DIR), check=True)
                # Push to remote
                subprocess.run(["git", "push", "-u", "origin", BRANCH_DEV], cwd=str(REPO_DIR), check=True)
                log.info("Created dev branch %s from main", BRANCH_DEV)
            except Exception as e:
                log.error("Failed to create dev branch: %s", repr(e))
                return False, "Failed to create dev branch"

    rc_verify = subprocess.run(
        ["git", "rev-parse", "--verify", f"origin/{branch}"],
        cwd=str(REPO_DIR),
        capture_output=True,
    ).returncode
    if rc_verify != 0:
        msg = f"Branch {branch} not found on remote"
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "reset_branch_missing",
                "target_branch": branch, "reason": reason,
            },
        )
        return False, msg

    subprocess.run(["git", "checkout", branch], cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "reset", "--hard", f"origin/{branch}"], cwd=str(REPO_DIR), check=True)
    # Clean __pycache__ to prevent stale bytecode (git checkout may not update mtime)
    for p in REPO_DIR.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    st = load_state()
    st["current_branch"] = branch
    st["current_sha"] = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO_DIR),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    save_state(st)
    return True, "ok"



# ---------------------------------------------------------------------------
# Dependencies + import test
# ---------------------------------------------------------------------------

def sync_runtime_dependencies(reason: str) -> Tuple[bool, str]:
    req_path = REPO_DIR / "requirements.txt"
    cmd: List[str] = [sys.executable, "-m", "pip", "install", "-q"]
    source = ""
    if req_path.exists():
        cmd += ["-r", str(req_path)]
        source = f"requirements:{req_path}"
    else:
        cmd += ["openai>=1.0.0", "requests"]
        source = "fallback:minimal"
    try:
        subprocess.run(cmd, cwd=str(REPO_DIR), check=True)
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "deps_sync_ok", "reason": reason, "source": source,
            },
        )
        return True, source
    except Exception as e:
        msg = repr(e)
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "deps_sync_error", "reason": reason, "source": source, "error": msg,
            },
        )
        return False, msg


def import_test() -> Tuple[bool, str]:
    try:
        import ouroboros
        import supervisor
        return True, "ok"
    except Exception as e:
        msg = repr(e)
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "import_test_error", "error": msg,
            },
        )
        return False, msg


def safe_restart(reason: str, unsynced_policy: str = "rescue_and_reset") -> Tuple[bool, str]:
    """Safe restart workflow: checkout -> deps -> import test. Returns (ok, message)."""
    ok, msg = checkout_and_reset(BRANCH_DEV, reason=reason, unsynced_policy=unsynced_policy)
    if not ok:
        return False, msg
    ok, msg = sync_runtime_dependencies(reason)
    if not ok:
        return False, msg
    ok, msg = import_test()
    if not ok:
        return False, msg
    # Save current state after successful refresh
    st = load_state()
    st["last_restart_ok_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    st["last_restart_reason"] = reason
    save_state(st)
    return True, "ok"


def promote_to_stable(reason: str) -> Tuple[bool, str]:
    """Promote current branch to stable. Creates or updates stable branch."""
    current_branch = load_state().get("current_branch", "")
    if not current_branch:
        return False, "No current branch in state"

    # Checkout stable if possible
    ok, msg = checkout_and_reset(BRANCH_STABLE, reason=reason, unsynced_policy="ignore")
    if ok:
        # Fast-forward stable from current (we're on stable so we know it was freshly reset)
        subprocess.run(["git", "merge", "--ff-only", current_branch], cwd=str(REPO_DIR), check=True)
    else:
        # Stable doesn't exist - create from current
        log.info("Stable branch missing - creating from current %s", current_branch)
        subprocess.run(["git", "checkout", "-b", BRANCH_STABLE], cwd=str(REPO_DIR), check=True)

    # Check if there are changes to push
    rc, _, _ = git_capture(["git", "diff", "--quiet", "origin/%s..%s" % (BRANCH_STABLE, BRANCH_STABLE)])
    if rc == 1:  # There are differences
        subprocess.run(["git", "push", "origin", BRANCH_STABLE], cwd=str(REPO_DIR), check=True)
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "branch_promoted",
                "stable_branch": BRANCH_STABLE,
                "from_branch": current_branch,
                "reason": reason,
            },
        )
        return True, f"{BRANCH_STABLE} updated from {current_branch}"
    else:
        return True, f"{BRANCH_STABLE} already up-to-date with {current_branch}"


# ---------------------------------------------------------------------------
# Runtime restart
# ---------------------------------------------------------------------------

RESTART_MARKER_PATH = DRIVE_ROOT / "state" / "pending_restart_verify.json"


def request_restart(reason: str) -> None:
    """Request restart. Persists expected state for verification post-restart."""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_DIR),
                            capture_output=True, text=True, check=True).stdout.strip()
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(REPO_DIR),
                             capture_output=True, text=True, check=True).stdout.strip()
        atomic_write_text(RESTART_MARKER_PATH, json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "expected_sha": sha,
            "expected_branch": branch,
            "reason": reason,
        }, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning("Failed to save restart marker", exc_info=True)
        pass
    # Signal supervisor to restart - this will cause launcher to exit
    raise RuntimeError("RESTART_REQUEST")


def _verify_restart_marker() -> Tuple[bool, str]:
    """Verify restart marker exists and matches current state."""
    if not RESTART_MARKER_PATH.exists():
        return True, "No restart marker"
    try:
        marker = json.loads(RESTART_MARKER_PATH.read_text(encoding="utf-8"))
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_DIR),
                            capture_output=True, text=True, check=True).stdout.strip()
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(REPO_DIR),
                             capture_output=True, text=True, check=True).stdout.strip()
        if sha != marker.get("expected_sha") or branch != marker.get("expected_branch"):
            msg = f"Restart verification failed: {branch}@{sha} != {marker.get('expected_branch')}@{marker.get('expected_sha')}"
            log.warning(msg)
            return False, msg
        return True, "ok"
    except Exception as e:
        msg = f"Restart marker verification error: {repr(e)}"
        log.warning(msg)
        return False, msg
    finally:
        try:
            RESTART_MARKER_PATH.unlink()
        except Exception:
            pass


def auto_resume_after_restart() -> None:
    """Called at launcher start to handle post-restart verification/resume."""
    ok, msg = _verify_restart_marker()
    if not ok:
        log.warning("Restart verification failed: %s", msg)
        # TODO: handle failed verification? For now we proceed
    st = load_state()
    st["auto_resume_ok"] = ok
    st["auto_resume_msg"] = msg
    save_state(st)