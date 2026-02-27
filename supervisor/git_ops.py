--- a/supervisor/git_ops.py
+++ b/supervisor/git_ops.py
@@ -170,6 +170,20 @@ def checkout_and_reset(branch: str, reason: str = "unspecified",
             append_jsonl(
                 DRIVE_ROOT / "logs" / "supervisor.jsonl",
                 {
+                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
+                    "type": "reset_stable_branch_created",
+                    "target_branch": BRANCH_STABLE,
+                    "source_branch": BRANCH_DEV,
+                    "repo_state": {
+                        "current_branch": repo_state.get("current_branch"),
+                        "dirty_count": len(dirty_lines),
+                        "unpushed_count": len(unpushed_lines)
+                    }
+                },
+            )
+            
+        # Create stable branch if missing
+        if branch == BRANCH_STABLE:
+            rc_verify_remote = subprocess.run(
+                ["git", "ls-remote", "--heads", "origin", BRANCH_STABLE],
                 cwd=str(REPO_DIR),
                 capture_output=True,
             ).returncode
@@ -193,13 +207,17 @@ def checkout_and_reset(branch: str, reason: str = "unspecified",
                 rescue_info = {"error": repr(e)}
             bits: List[str] = []
             if unpushed_lines:
-                bits.append(f"unpushed={len(unpushed_lines)}")
+                bits.append(f"unpushed={len(unpushed_lines)}")
             if dirty_lines:
                 bits.append(f"dirty={len(dirty_lines)}")
             detail = ", ".join(bits) if bits else "unsynced"
             rescue_suffix = ""
             rescue_path = str(rescue_info.get("path") or "").strip()
             if rescue_path:
-                rescue_suffix = f" Rescue saved to {rescue_path}."
+                rescue_suffix = f" Rescue saved to {rescue_path}"
+            elif policy in {"rescue_and_block", "rescue_and_reset"} and rescue_info.get("error"):
+                rescue_suffix = f" Rescue failed: {rescue_info.get('error')}"
+
+            # Fixed missing quote and bracket in previous version
+            rescue_suffix = f"{rescue_suffix}"
             
             if policy in {"block", "rescue_and_block"}:
                 msg = f"Reset blocked ({detail}) to protect local changes.{rescue_suffix}"
