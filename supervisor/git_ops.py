--- a/supervisor/git_ops.py
+++ b/supervisor/git_ops.py
@@ -193,7 +193,7 @@ def checkout_and_reset(branch: str, reason: str = "unspecified",
             rescue_path = str(rescue_info.get("path") or "").strip()
             if rescue_path:
                 rescue_suffix = f" Rescue saved to {rescue_path}."
-            elif policy in {"rescue_and_block", "rescue_and_block"} and rescue_info.get("error"):
+            elif policy in {"rescue_and_block", "rescue_and_reset"} and rescue_info.get("error"):
                 rescue_suffix = f" Rescue failed: {rescue_info.get('error')}"
 
             if policy in {"block", "rescue_and_block"}: