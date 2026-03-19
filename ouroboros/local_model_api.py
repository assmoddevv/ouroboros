"""Local model management API endpoints, extracted from server.py."""

from starlette.requests import Request
from starlette.responses import JSONResponse


async def api_local_model_start(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        source = body.get("source", "").strip()
        filename = body.get("filename", "").strip()
        port = int(body.get("port", 8766))
        backend = body.get("backend", "cpu").strip()
        gpu_device = str(body.get("gpu_device", "auto")).strip()
        n_ctx = int(body.get("n_ctx", 0))
        chat_format = body.get("chat_format", "").strip()

        if not source:
            return JSONResponse({"error": "source is required"}, status_code=400)

        from ouroboros.local_model import get_manager
        mgr = get_manager()

        if mgr.is_running:
            return JSONResponse({"error": "Local model server is already running"}, status_code=409)

        # Download can be slow, run in thread to not block the async event loop
        import asyncio
        model_path = await asyncio.to_thread(mgr.download_model, source, filename)
        
        mgr.start_server(model_path, port=port, backend=backend, gpu_device=gpu_device, n_ctx=n_ctx, chat_format=chat_format)
        return JSONResponse({"status": "starting", "model_path": model_path})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_local_model_stop(request: Request) -> JSONResponse:
    try:
        from ouroboros.local_model import get_manager
        get_manager().stop_server()
        return JSONResponse({"status": "stopped"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_local_model_status(request: Request) -> JSONResponse:
    try:
        from ouroboros.local_model import get_manager
        return JSONResponse(get_manager().status_dict())
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)})


async def api_local_model_test(request: Request) -> JSONResponse:
    try:
        from ouroboros.local_model import get_manager
        mgr = get_manager()
        if not mgr.is_running:
            return JSONResponse({"error": "Local model server is not running"}, status_code=400)
        result = mgr.test_tool_calling()
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── GPU backend management ───────────────────────────────────────────

_gpu_install_lock = False

_CUDA_WHEEL_INDEX = "https://abetlen.github.io/llama-cpp-python/whl/cu124"
_CUDA_RUNTIME_PACKAGES = [
    "nvidia-cuda-runtime-cu12==12.4.127",
    "nvidia-cublas-cu12==12.4.5.8",
]


def _gpu_backend_site_packages() -> str:
    from ouroboros.config import GPU_BACKEND_DIR
    return str(GPU_BACKEND_DIR / "site-packages")


async def api_local_model_gpu_status(request: Request) -> JSONResponse:
    import os
    sp = _gpu_backend_site_packages()
    installed = os.path.isdir(os.path.join(sp, "llama_cpp"))
    return JSONResponse({"installed": installed, "path": sp})


async def _run_pip(cmd, env=None, timeout=1800):
    """Run pip in a thread, return (success, stdout+stderr)."""
    import asyncio, subprocess
    proc = await asyncio.to_thread(
        subprocess.run, cmd,
        capture_output=True, text=True, timeout=timeout,
        env=env,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output


def _build_env_with_cuda() -> dict:
    """Build environment dict with CUDA paths for source compilation."""
    import os, glob
    env = os.environ.copy()
    env["CMAKE_ARGS"] = "-DGGML_CUDA=on"

    cuda_path = env.get("CUDA_PATH", "")
    if not cuda_path:
        for pattern in [
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v*",
            "/usr/local/cuda*",
        ]:
            matches = sorted(glob.glob(pattern), reverse=True)
            if matches:
                cuda_path = matches[0]
                break

    if cuda_path:
        env["CUDA_PATH"] = cuda_path
        cuda_bin = os.path.join(cuda_path, "bin")
        if os.path.isdir(cuda_bin) and cuda_bin not in env.get("PATH", ""):
            env["PATH"] = cuda_bin + os.pathsep + env.get("PATH", "")

    return env


async def api_local_model_gpu_install(request: Request) -> JSONResponse:
    """Install GPU backend.

    1. Try source build with CUDA (picks up CUDA Toolkit from standard paths).
    2. If source build fails, fall back to pre-built wheel.
    """
    global _gpu_install_lock
    if _gpu_install_lock:
        return JSONResponse({"error": "Installation already in progress"}, status_code=409)

    import sys, os, shutil, logging
    from ouroboros.config import GPU_BACKEND_DIR

    log = logging.getLogger("ouroboros.local_model_api")

    sp = _gpu_backend_site_packages()
    python = sys.executable

    _gpu_install_lock = True
    try:
        # ── Attempt 1: source build with CUDA ────────────────────────
        if GPU_BACKEND_DIR.exists():
            shutil.rmtree(GPU_BACKEND_DIR)
        os.makedirs(sp, exist_ok=True)

        build_env = _build_env_with_cuda()
        cmd_source = [
            python, "-m", "pip", "install",
            "--target", sp, "--no-cache-dir",
            "llama-cpp-python[server]",
        ]
        log.info("GPU install attempt 1 (source build): CUDA_PATH=%s, %s",
                 build_env.get("CUDA_PATH", ""), " ".join(cmd_source))
        ok, output = await _run_pip(cmd_source, env=build_env)

        if ok:
            cmd_rt = [python, "-m", "pip", "install", "--target", sp,
                      "--prefer-binary"] + _CUDA_RUNTIME_PACKAGES
            await _run_pip(cmd_rt, timeout=600)
            log.info("GPU backend installed (source build) to %s", sp)
            return JSONResponse({"status": "installed", "method": "source", "path": sp})

        source_details = output[-2000:]
        log.warning("Source build failed: %s", source_details[-500:])

        # ── Attempt 2: pre-built wheel ───────────────────────────────
        if GPU_BACKEND_DIR.exists():
            shutil.rmtree(GPU_BACKEND_DIR)
        os.makedirs(sp, exist_ok=True)

        cmd_prebuilt = [
            python, "-m", "pip", "install",
            "--target", sp, "--prefer-binary",
            "--extra-index-url", _CUDA_WHEEL_INDEX,
            "llama-cpp-python[server]",
        ] + _CUDA_RUNTIME_PACKAGES

        log.info("GPU install attempt 2 (pre-built wheel): %s", " ".join(cmd_prebuilt))
        ok2, output2 = await _run_pip(cmd_prebuilt)

        if ok2:
            log.info("GPU backend installed (pre-built) to %s", sp)
            return JSONResponse({
                "status": "installed",
                "method": "prebuilt",
                "path": sp,
                "warning": "Installed pre-built GPU wheel (may not support newest models). "
                           "For full compatibility: install CUDA Toolkit + CMake + C++ compiler, "
                           "then reinstall.",
            })

        log.error("Both install methods failed")
        return JSONResponse({
            "error": "GPU install failed. Source build log (truncated):",
            "details": source_details,
        }, status_code=500)

    except Exception as e:
        log.error("GPU backend install error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        _gpu_install_lock = False


async def api_local_model_gpu_remove(request: Request) -> JSONResponse:
    import shutil
    from ouroboros.config import GPU_BACKEND_DIR
    try:
        if GPU_BACKEND_DIR.exists():
            shutil.rmtree(GPU_BACKEND_DIR)
        return JSONResponse({"status": "removed"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
