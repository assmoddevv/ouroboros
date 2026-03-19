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
_GPU_PACKAGES = [
    "llama-cpp-python[server]",
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


async def api_local_model_gpu_install(request: Request) -> JSONResponse:
    global _gpu_install_lock
    if _gpu_install_lock:
        return JSONResponse({"error": "Installation already in progress"}, status_code=409)

    import asyncio, subprocess, sys, os, logging

    log = logging.getLogger("ouroboros.local_model_api")

    sp = _gpu_backend_site_packages()
    os.makedirs(sp, exist_ok=True)

    python = sys.executable

    cmd = [
        python, "-m", "pip", "install",
        "--target", sp,
        "--prefer-binary",
        "--extra-index-url", _CUDA_WHEEL_INDEX,
    ] + _GPU_PACKAGES

    log.info("GPU backend install: %s", " ".join(cmd))

    _gpu_install_lock = True
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd,
            capture_output=True, text=True, timeout=1800,
        )
        if proc.returncode != 0:
            details = (proc.stderr or proc.stdout or "")[-2000:]
            log.error("GPU backend install failed (code %d): %s", proc.returncode, details[-500:])
            return JSONResponse({
                "error": f"pip install failed (code {proc.returncode})",
                "details": details,
            }, status_code=500)
        log.info("GPU backend installed to %s", sp)
        return JSONResponse({"status": "installed", "path": sp})
    except subprocess.TimeoutExpired:
        log.error("GPU backend install timed out")
        return JSONResponse({"error": "Installation timed out (30 min limit)"}, status_code=500)
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
