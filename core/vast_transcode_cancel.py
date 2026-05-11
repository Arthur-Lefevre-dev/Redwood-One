"""Cancel a Vast transcode test task: Redis flag, Vast destroy, S3 cleanup, Celery revoke. Comments in English."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from config import get_settings

logger = logging.getLogger(__name__)

REDIS_CANCEL_PREFIX = "redwood:vast_transcode_cancel:"


def cancel_flag_key(task_id: str) -> str:
    return f"{REDIS_CANCEL_PREFIX}{task_id}"


def set_cancel_flag(task_id: str, ttl_sec: int = 172800) -> None:
    """Worker polls this key to exit the wait loop cooperatively."""
    try:
        import redis

        r = redis.from_url(get_settings().redis_url, decode_responses=True)
        r.set(cancel_flag_key(task_id), "1", ex=int(ttl_sec))
    except Exception:
        logger.warning("vast_transcode_cancel: could not set Redis flag for %s", task_id)


def is_cancel_requested(task_id: Optional[str]) -> bool:
    if not task_id:
        return False
    try:
        import redis

        r = redis.from_url(get_settings().redis_url, decode_responses=True)
        return bool(r.get(cancel_flag_key(task_id)))
    except Exception:
        return False


def clear_cancel_flag(task_id: str) -> None:
    try:
        import redis

        r = redis.from_url(get_settings().redis_url, decode_responses=True)
        r.delete(cancel_flag_key(task_id))
    except Exception:
        pass


def cleanup_vast_transcode_artifacts(meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Best-effort: destroy Vast instance and remove S3 keys listed in task meta.
    Safe to call multiple times (Vast/S3 may already be gone).
    """
    from core import vast_ai
    from core.s3 import delete_object_key

    summary: Dict[str, Any] = {"vast_destroyed": False, "s3_deleted": []}
    inst = meta.get("vast_instance_id")
    if inst is not None:
        try:
            vast_ai.destroy_instance(int(inst))
            summary["vast_destroyed"] = True
            logger.info("vast_transcode_cancel: destroyed instance %s", inst)
        except Exception as e:
            logger.warning("vast_transcode_cancel: destroy_instance %s: %s", inst, e)

    keys: list[str] = []
    ik = meta.get("input_key")
    ok = meta.get("output_key")
    if isinstance(ik, str) and ik.strip():
        keys.append(ik.strip())
    if isinstance(ok, str) and ok.strip():
        keys.append(ok.strip())
    jt = meta.get("job_token")
    se = meta.get("src_ext")
    if not keys and isinstance(jt, str) and jt.strip() and isinstance(se, str) and se.strip():
        base = jt.strip()
        ext = se.strip() if se.startswith(".") else f".{se.strip()}"
        keys.append(f"vast-test/{base}/input{ext}")
        keys.append(f"vast-test/{base}/output.mp4")

    seen: set[str] = set()
    for k in keys:
        if not k or k in seen:
            continue
        seen.add(k)
        try:
            delete_object_key(k)
            summary["s3_deleted"].append(k)
        except Exception as e:
            logger.warning("vast_transcode_cancel: delete %s: %s", k, e)
    return summary


def cancel_vast_transcode_test(app: Any, task_id: str) -> Dict[str, Any]:
    """
    Set cancel flag, read Celery meta, destroy Vast + S3, revoke task (SIGTERM).
    Call from admin API only (requires VAST_API_KEY for destroy).
    """
    from celery.result import AsyncResult

    set_cancel_flag(task_id)
    res = AsyncResult(task_id, app=app)
    meta: Dict[str, Any] = dict(res.info) if isinstance(res.info, dict) else {}
    _merge_job_envelope(task_id, meta)
    summary = cleanup_vast_transcode_artifacts(meta)
    try:
        app.control.revoke(task_id, terminate=True, signal="SIGTERM")
    except Exception as e:
        logger.warning("vast_transcode_cancel: revoke %s: %s", task_id, e)
    _delete_job_envelope(task_id)
    summary["ok"] = True
    summary["task_id"] = task_id
    return summary


def _job_envelope_key(task_id: str) -> str:
    return f"redwood:vast_transcode_job:{task_id}"


def store_job_envelope(task_id: str, job_token: str, src_ext: str, ttl_sec: int = 172800) -> None:
    """API stores this at enqueue so cancel can clean S3 before Celery meta exists."""
    try:
        import json
        import redis

        r = redis.from_url(get_settings().redis_url, decode_responses=True)
        r.set(
            _job_envelope_key(task_id),
            json.dumps({"job_token": job_token, "src_ext": src_ext}),
            ex=int(ttl_sec),
        )
    except Exception:
        logger.warning("vast_transcode_cancel: could not store job envelope for %s", task_id)


def _merge_job_envelope(task_id: str, meta: Dict[str, Any]) -> None:
    try:
        import json
        import redis

        r = redis.from_url(get_settings().redis_url, decode_responses=True)
        raw = r.get(_job_envelope_key(task_id))
        if not raw:
            return
        extra = json.loads(raw)
        if isinstance(extra, dict):
            for k, v in extra.items():
                meta.setdefault(k, v)
    except Exception:
        pass


def _delete_job_envelope(task_id: str) -> None:
    try:
        import redis

        r = redis.from_url(get_settings().redis_url, decode_responses=True)
        r.delete(_job_envelope_key(task_id))
    except Exception:
        pass
