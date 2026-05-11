"""Run a one-shot transcode on a Vast.ai GPU instance (onstart + S3 presigned URLs). Comments in English."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from config import get_settings

logger = logging.getLogger(__name__)

# Env: RW_IN, RW_OUT, RW_EXT, RW_BR/MR/BF, RW_GPU_WAIT (max seconds to wait for /dev/nvidia0).
# Tries NVENC (GPU); if no CUDA device, falls back to libx264 (CPU) so the job still completes.
VAST_TRANSCODE_ONSTART = r"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates ffmpeg
# Vast attaches GPU device nodes to the container; they can appear shortly after boot.
GW="${RW_GPU_WAIT:-90}"
i=0
while [ "$i" -lt "$GW" ]; do
  if [ -e /dev/nvidia0 ]; then break; fi
  i=$((i + 1))
  sleep 1
done
curl -fSL "${RW_IN}" -o "/tmp/in${RW_EXT}"
set +e
ffmpeg -y -hwaccel cuda -hwaccel_output_format cuda -i "/tmp/in${RW_EXT}" \
  -c:v h264_nvenc -b:v "${RW_BR}" -maxrate "${RW_MR}" -bufsize "${RW_BF}" \
  -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart /tmp/out.mp4
RC=$?
if [ "${RC}" != "0" ]; then
  ffmpeg -y -i "/tmp/in${RW_EXT}" \
    -c:v h264_nvenc -b:v "${RW_BR}" -maxrate "${RW_MR}" -bufsize "${RW_BF}" \
    -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart /tmp/out.mp4
  RC=$?
fi
if [ "${RC}" != "0" ]; then
  ffmpeg -y -i "/tmp/in${RW_EXT}" \
    -c:v libx264 -preset faster -b:v "${RW_BR}" -maxrate "${RW_MR}" -bufsize "${RW_BF}" \
    -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart /tmp/out.mp4
  RC=$?
fi
set -e
if [ "${RC}" != "0" ]; then
  echo "ffmpeg: all encode paths failed (rc=${RC})" >&2
  exit "${RC}"
fi
curl -f -X PUT -H "Content-Type: video/mp4" --upload-file /tmp/out.mp4 "${RW_OUT}"
"""


def run_vast_transcode_test(
    task_self: Any,
    job_token: str,
    src_ext: str,
    offer_id: Optional[int] = None,
    film_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Pick a Vast offer, create an instance whose onstart downloads the input from S3 (presigned GET),
    transcodes (NVENC if GPU visible, else libx264), uploads MP4 via presigned PUT; poll S3; destroy instance.
    """
    from core import vast_ai
    from core.s3 import (
        delete_object_key,
        object_size_or_none,
        presigned_put_url,
        presigned_stream_url,
    )
    from core.vast_transcode_cancel import clear_cancel_flag, is_cancel_requested

    s = get_settings()
    rid = str(getattr(task_self.request, "id", "") or "").strip() or None
    input_key = f"vast-test/{job_token}/input{src_ext}"
    output_key = f"vast-test/{job_token}/output.mp4"
    ttl = int(s.VAST_TRANSCODE_URL_TTL_SEC)
    poll_sec = max(5, int(s.VAST_TRANSCODE_POLL_INTERVAL_SEC))
    max_wait = max(120, int(s.VAST_TRANSCODE_MAX_WAIT_SEC))
    inst_id: Optional[int] = None

    def meta(**kw: Any) -> None:
        task_self.update_state(state="PROGRESS", meta=kw)

    try:
        meta(
            step="presign_urls",
            progress=4,
            job_token=job_token,
            input_key=input_key,
            output_key=output_key,
            src_ext=src_ext,
            celery_task_id=rid,
        )
        if is_cancel_requested(rid):
            raise RuntimeError("Cancelled by user")
        get_url = presigned_stream_url(input_key, expires=ttl)
        put_url = presigned_put_url(output_key, expires=ttl, content_type="video/mp4")

        meta(step="pick_offer", progress=10)
        if is_cancel_requested(rid):
            raise RuntimeError("Cancelled by user")
        picked_gpu_name: Optional[str] = None
        if offer_id is not None and int(offer_id) > 0:
            oid = int(offer_id)
        else:
            first = vast_ai.pick_first_verified_bundle_offer(
                vast_ai.default_gpu_name_list(),
                search_limit=48,
            )
            oid = int(first["id"])
            picked_gpu_name = first.get("gpu_name") if isinstance(first.get("gpu_name"), str) else None

        br = f"{int(s.TRANSCODE_VIDEO_BITRATE_KBPS)}k"
        mr = f"{int(s.TRANSCODE_VIDEO_MAXRATE_KBPS)}k"
        bf = f"{int(s.TRANSCODE_VIDEO_BUFSIZE_KBPS)}k"
        gpu_wait = max(5, int(s.VAST_TRANSCODE_GPU_DEVICE_WAIT_SEC))
        env = {
            "RW_IN": get_url,
            "RW_OUT": put_url,
            "RW_EXT": src_ext,
            "RW_BR": br,
            "RW_MR": mr,
            "RW_BF": bf,
            "RW_GPU_WAIT": str(gpu_wait),
            # Help some hosts expose the GPU to the container (no-op if unsupported).
            "NVIDIA_VISIBLE_DEVICES": "all",
            "NVIDIA_DRIVER_CAPABILITIES": "compute,video,utility",
        }
        image = (s.VAST_TRANSCODE_DOCKER_IMAGE or "nvidia/cuda:12.3.1-runtime-ubuntu22.04").strip()
        disk = max(16, int(s.VAST_TRANSCODE_DISK_GB))

        meta_kw: Dict[str, Any] = {
            "step": "create_vast_instance",
            "progress": 15,
            "offer_id": oid,
            "vast_image": image,
        }
        if picked_gpu_name:
            meta_kw["picked_gpu_name"] = picked_gpu_name
        meta(**meta_kw)
        if is_cancel_requested(rid):
            raise RuntimeError("Cancelled by user")
        raw = vast_ai.create_instance(
            oid,
            image=image,
            disk_gb=disk,
            runtype="ssh_direct",
            label=f"redwood-vast-tx-{job_token[:10]}",
            env=env,
            onstart=VAST_TRANSCODE_ONSTART,
        )
        inst_id = raw.get("new_contract") if isinstance(raw, dict) else None
        if inst_id is None:
            raise RuntimeError(f"Vast create_instance returned no new_contract: {raw!r}")

        meta(
            step="wait_output_on_s3",
            progress=20,
            vast_instance_id=int(inst_id),
            offer_id=oid,
            input_key=input_key,
            output_key=output_key,
            job_token=job_token,
            src_ext=src_ext,
            hint="Instance runs onstart (apt + ffmpeg + upload). First boot can take several minutes.",
        )
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            if is_cancel_requested(rid):
                raise RuntimeError("Cancelled by user")
            sz = object_size_or_none(output_key)
            if sz is not None and sz > 256_000:
                time.sleep(5)
                sz2 = object_size_or_none(output_key)
                if sz2 is not None and sz2 >= sz:
                    meta(
                        step="output_ready",
                        progress=92,
                        vast_instance_id=int(inst_id),
                        output_bytes=sz2,
                    )
                    break
            elapsed = max_wait - (deadline - time.monotonic())
            prog = 20 + min(70, int(70 * elapsed / max_wait))
            meta(
                step="wait_output_on_s3",
                progress=prog,
                vast_instance_id=int(inst_id),
                output_bytes=sz,
            )
            time.sleep(poll_sec)
        else:
            raise RuntimeError(
                "Timeout waiting for transcoded MP4 on S3. Check the instance logs on Vast.ai "
                "(ffmpeg / network / presigned URL expiry)."
            )

        view_url = presigned_stream_url(output_key, expires=86400)
        dph = float(s.VAST_MAX_DPH_PER_HOUR)
        if film_id is not None and int(film_id) > 0:
            from core.vast_film_finalize import finalize_film_from_vast_s3_output
            from db.models import Film as FilmRow
            from db.session import SessionLocal

            finalize_film_from_vast_s3_output(int(film_id), output_key)
            dbf = SessionLocal()
            try:
                frow = dbf.get(FilmRow, int(film_id))
                if frow and frow.s3_key:
                    view_url = presigned_stream_url(frow.s3_key, expires=86400)
            finally:
                dbf.close()
        if rid:
            clear_cancel_flag(rid)
        return {
            "ok": True,
            "job_token": job_token,
            "output_key": output_key,
            "output_url": view_url,
            "offer_id": oid,
            "vast_instance_id": int(inst_id),
            "pricing_dph_usd": dph,
            "film_id": int(film_id) if film_id is not None and int(film_id) > 0 else None,
        }
    finally:
        if inst_id is not None:
            try:
                vast_ai.destroy_instance(int(inst_id))
                logger.info("vast_remote_transcode: destroyed instance %s", inst_id)
            except Exception:
                logger.exception("vast_remote_transcode: destroy_instance failed id=%s", inst_id)
        try:
            delete_object_key(input_key)
        except Exception:
            logger.warning("vast_remote_transcode: could not delete input %s", input_key)
