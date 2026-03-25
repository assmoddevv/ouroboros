"""Helpers for starting the local model server from app startup."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def auto_start_local_model(settings: dict) -> None:
    """Download (if needed) and start the local model server in background.

    When LOCAL_MODEL_URL points to an external server, just health-check it.
    When it points to localhost (auto-filled by a previous Start), fall through
    to start the built-in server.
    """
    external_url = str(settings.get("LOCAL_MODEL_URL", "")).strip()
    if external_url:
        from ouroboros.local_model import LocalModelManager, is_localhost_model_url
        if not is_localhost_model_url(external_url):
            try:
                health = LocalModelManager.health_check_external(external_url)
                if health.get("ok"):
                    log.info(
                        "External local model server reachable: %s (model=%s)",
                        external_url, health.get("model_name"),
                    )
                else:
                    log.warning(
                        "External local model server at %s not reachable: %s",
                        external_url, health.get("error"),
                    )
            except Exception as exc:
                log.warning("External server health-check failed: %s", exc)
            return

    try:
        from ouroboros.local_model import get_manager

        mgr = get_manager()
        if mgr.is_running:
            return

        source = str(settings.get("LOCAL_MODEL_SOURCE", "")).strip()
        filename = str(settings.get("LOCAL_MODEL_FILENAME", "")).strip()
        port = int(settings.get("LOCAL_MODEL_PORT", 8766))
        n_gpu_layers = int(settings.get("LOCAL_MODEL_N_GPU_LAYERS", 0))
        n_ctx = int(settings.get("LOCAL_MODEL_CONTEXT_LENGTH", 16384))
        chat_format = str(settings.get("LOCAL_MODEL_CHAT_FORMAT", "")).strip()

        log.info("Auto-starting local model: %s / %s", source, filename)
        model_path = mgr.download_model(source, filename)
        mgr.start_server(
            model_path,
            port=port,
            n_gpu_layers=n_gpu_layers,
            n_ctx=n_ctx,
            chat_format=chat_format,
        )
        log.info("Local model auto-started successfully")
    except Exception as exc:
        log.warning("Local model auto-start failed: %s", exc)
