"""Local model management API endpoints, extracted from server.py."""

import os

from starlette.requests import Request
from starlette.responses import JSONResponse


async def api_local_model_start(request: Request) -> JSONResponse:
    from ouroboros.local_model import is_external_server_mode
    if is_external_server_mode():
        return JSONResponse(
            {"error": "Built-in server controls are disabled when a Server URL is set. "
                      "Manage that server directly."},
            status_code=400,
        )
    try:
        body = await request.json()
        source = body.get("source", "").strip()
        filename = body.get("filename", "").strip()
        port = int(body.get("port", 8766))
        n_gpu_layers = int(body.get("n_gpu_layers", -1))
        n_ctx = int(body.get("n_ctx", 0))
        chat_format = body.get("chat_format", "").strip()

        if not source:
            return JSONResponse({"error": "source is required"}, status_code=400)

        from ouroboros.local_model import get_manager
        mgr = get_manager()

        if mgr.is_running:
            return JSONResponse({"error": "Built-in model server is already running"}, status_code=409)

        import asyncio
        model_path = await asyncio.to_thread(mgr.download_model, source, filename)

        mgr.start_server(model_path, port=port, n_gpu_layers=n_gpu_layers, n_ctx=n_ctx, chat_format=chat_format)
        return JSONResponse({"status": "starting", "model_path": model_path})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_local_model_stop(request: Request) -> JSONResponse:
    from ouroboros.local_model import is_external_server_mode
    if is_external_server_mode():
        return JSONResponse(
            {"error": "Built-in server controls are disabled when a Server URL is set."},
            status_code=400,
        )
    try:
        from ouroboros.local_model import get_manager
        get_manager().stop_server()
        return JSONResponse({"status": "stopped"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_local_model_status(request: Request) -> JSONResponse:
    external_url = os.environ.get("LOCAL_MODEL_URL", "").strip()
    if external_url:
        from ouroboros.local_model import LocalModelManager, is_localhost_model_url
        if not is_localhost_model_url(external_url):
            try:
                health = LocalModelManager.health_check_external(external_url)
                if health.get("ok"):
                    return JSONResponse({
                        "status": "ready",
                        "external": True,
                        "url": external_url,
                        "model_name": health.get("model_name", ""),
                        "context_length": health.get("context_length", 0),
                        "error": None,
                    })
                return JSONResponse({
                    "status": "error",
                    "external": True,
                    "url": external_url,
                    "error": health.get("error", "Unreachable"),
                })
            except Exception as e:
                return JSONResponse({"status": "error", "external": True, "error": str(e)})

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
            return JSONResponse({"error": "Built-in model server is not running"}, status_code=400)
        result = mgr.test_tool_calling()
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_local_model_gpu_info(request: Request) -> JSONResponse:
    """Return detected NVIDIA GPU info."""
    try:
        from ouroboros.compat import detect_nvidia_gpu
        info = detect_nvidia_gpu()
        if info is None:
            return JSONResponse({"detected": False, "message": "No NVIDIA GPU detected (nvidia-smi not found)"})
        return JSONResponse({"detected": True, **info})
    except Exception as e:
        return JSONResponse({"detected": False, "error": str(e)})


async def api_local_model_install_cuda(request: Request) -> JSONResponse:
    """Install llama-cpp-python with CUDA support into embedded python-standalone."""
    import asyncio
    import subprocess
    import sys

    try:
        from ouroboros.compat import detect_nvidia_gpu, get_embedded_python, IS_WINDOWS

        if not IS_WINDOWS:
            return JSONResponse(
                {"error": "CUDA installer is for Windows only. On macOS use Metal wheels."},
                status_code=400,
            )

        gpu_info = detect_nvidia_gpu()
        if gpu_info is None:
            return JSONResponse(
                {"error": "No NVIDIA GPU detected. Install NVIDIA drivers first."},
                status_code=400,
            )

        python_path = get_embedded_python()
        if python_path is None:
            python_path = sys.executable

        cuda_index = "https://abetlen.github.io/llama-cpp-python/whl/cu124"
        pkg = "llama-cpp-python[server]==0.3.4"

        cmd = [
            str(python_path), "-m", "pip", "install",
            pkg, "--force-reinstall",
            "--extra-index-url", cuda_index,
        ]

        def _run_install():
            _kwargs = {"capture_output": True, "text": True, "timeout": 600}
            if IS_WINDOWS:
                _kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            return subprocess.run(cmd, **_kwargs)

        result = await asyncio.to_thread(_run_install)

        if result.returncode == 0:
            return JSONResponse({
                "ok": True,
                "message": f"CUDA support installed successfully ({pkg})",
                "gpu": gpu_info.get("gpu", ""),
            })
        else:
            stderr = (result.stderr or "").strip()[-1000:]
            return JSONResponse({
                "ok": False,
                "error": f"pip install failed (exit {result.returncode})",
                "details": stderr,
            }, status_code=500)

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
