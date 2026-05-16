"""Run a one-shot transcode on a Vast.ai GPU instance (onstart + S3 presigned URLs). Comments in English."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from config import get_settings

logger = logging.getLogger(__name__)

REDWOOD_VAST_NO_GPU_SENTINEL = "REDWOOD_VAST_NO_GPU=1"


def _repick_vast_bundle_offer(
    skipped_offer_ids: list[int],
    *,
    search_limit: int = 64,
) -> tuple[int, Optional[str]]:
    """Pick the next rentable bundle offer, skipping ids that already failed."""
    from core import vast_ai

    first = vast_ai.pick_first_verified_bundle_offer(
        vast_ai.default_gpu_name_list(),
        search_limit=search_limit,
        skip_offer_ids=skipped_offer_ids,
    )
    oid = int(first["id"])
    gpu = first.get("gpu_name") if isinstance(first.get("gpu_name"), str) else None
    return oid, gpu

# Env: RW_IN, RW_OUT, RW_EXT, RW_BR/MR/BF, RW_BA (AAC kbit/s), RW_GPU_WAIT, RW_FFMPEG_URL (optional BtbN tarball URL).
# Prefers a recent static FFmpeg with NVENC; sets NVIDIA_DRIVER_CAPABILITIES + LD_LIBRARY_PATH for Vast.
# Vast contracts are GPU-backed: NVENC is the intended path. Order: NVENC (decode on CPU or CUDA),
# optional distro ffmpeg NVENC if BtbN differs, then libx264 only as last resort if the NVENC stack fails
# (driver/encoder mismatch, broken build). If /dev/nvidia0 never appears after wait, onstart aborts with
# REDWOOD_VAST_NO_GPU=1 (no download); Celery destroys the contract and picks another offer.
# Large input: aria2c parallel download (S3 presigned GET supports Range); falls back to curl.
VAST_TRANSCODE_ONSTART = r"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export NVIDIA_DRIVER_CAPABILITIES="${NVIDIA_DRIVER_CAPABILITIES:-all}"
export LD_LIBRARY_PATH="/usr/local/nvidia/lib:/usr/local/nvidia/lib64:${LD_LIBRARY_PATH:-}"
apt-get update -qq
apt-get install -y -qq curl ca-certificates xz-utils ffmpeg aria2
# Vast attaches GPU device nodes to the container; they can appear shortly after boot.
GW="${RW_GPU_WAIT:-90}"
i=0
while [ "$i" -lt "$GW" ]; do
  if [ -e /dev/nvidia0 ]; then break; fi
  i=$((i + 1))
  sleep 1
done
if [ ! -e /dev/nvidia0 ]; then
  NO_GPU_MSG="REDWOOD_VAST_NO_GPU=1
rw_transcode: /dev/nvidia0 missing after ${GW}s; aborting without download."
  if [ -n "${RW_PROGRESS_PUT:-}" ]; then
    curl -sf -X PUT -H "Content-Type: text/plain; charset=utf-8" --data-binary "$NO_GPU_MSG" "${RW_PROGRESS_PUT}" || true
  fi
  echo "$NO_GPU_MSG" >&2
  exit 77
fi
IN="/tmp/in${RW_EXT}"
rm -f "$IN"
DL_CONN="${RW_DL_CONN:-16}"
DL_SPLIT="${RW_DL_SPLIT:-16}"
set +e
aria2c --disable-ipv6=true --file-allocation=none --allow-overwrite=true \
  --max-tries=8 --retry-wait=5 --timeout=120 --connect-timeout=30 \
  --max-connection-per-server="$DL_CONN" --split="$DL_SPLIT" --min-split-size=4M \
  --summary-interval=30 --console-log-level=notice \
  -d /tmp -o "in${RW_EXT}" "${RW_IN}"
AR=$?
set -e
if [ "$AR" != "0" ]; then
  rm -f "$IN"
  curl -fSL "${RW_IN}" -o "$IN"
fi
GPUFF=/usr/bin/ffmpeg
if [ -n "${RW_FFMPEG_URL:-}" ]; then
  mkdir -p /tmp/btbn
  curl -fSL "${RW_FFMPEG_URL}" -o /tmp/btbn/ff.txz
  tar -xJf /tmp/btbn/ff.txz -C /tmp/btbn
  shopt -s nullglob
  G=(/tmp/btbn/ffmpeg-*-linux64-gpl/bin/ffmpeg)
  if [ ${#G[@]} -ge 1 ]; then GPUFF="${G[0]}"; fi
  shopt -u nullglob
  chmod +x "$GPUFF"
fi
INP="/tmp/in${RW_EXT}"
RW_MAP_ARGS=""
RW_SUB_CODEC=""
RW_HAS_AUD="0"
RW_AUD_IDX=""
if ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$INP" 2>/dev/null | grep -q .; then
  RW_HAS_AUD="1"
  RW_AUD_IDX=$(python3 - "$INP" <<'PY' 2>/dev/null || true
import json, subprocess, sys
inp = sys.argv[1]
FRENCH = {"fr", "fra", "fre", "french", "français", "francais"}
HINTS = ("french", "français", "francais", " vf", "vf ", "vff", "truefrench", "version française", "version francaise", " dub", "dubbed")
def norm_lang(s):
    tags = s.get("tags") or {}
    raw = (tags.get("language") or "").strip().lower()
    return raw.replace("_", "-").split("-")[0] if raw else ""
def is_fr(s):
    lang = norm_lang(s)
    if lang in FRENCH or (lang and lang.startswith("fr")):
        return True
    t = ((s.get("tags") or {}).get("title") or "").lower()
    tc = t.strip()
    if tc in ("vf", "vff", "vfi", "truefrench"):
        return True
    return any(h in t for h in HINTS)
def rank(s):
    title = ((s.get("tags") or {}).get("title") or "").lower()
    p = 0
    if "commentary" in title or "comment" in title:
        p += 10
    if "descriptive" in title or "audio description" in title:
        p += 20
    disp = s.get("disposition") or {}
    if disp.get("default") in (1, "1", True):
        p -= 5
    idx = s.get("index")
    tie = int(idx) if isinstance(idx, int) else 9999
    return (p, tie)
try:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index:stream_tags=language:stream_tags=title",
         "-of", "json", inp],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if r.returncode != 0:
        raise SystemExit(0)
    streams = (json.loads(r.stdout or "{}").get("streams") or [])
except Exception:
    raise SystemExit(0)
if not streams:
    raise SystemExit(0)
fr = [s for s in streams if is_fr(s)]
pick = sorted(fr, key=rank)[0] if fr else streams[0]
idx = pick.get("index")
if isinstance(idx, int) and idx >= 0:
    print(idx)
PY
)
  RW_AUD_IDX=$(printf '%s' "$RW_AUD_IDX" | tr -d '\r\n')
  if [ -z "$RW_AUD_IDX" ]; then
    RW_AUD_IDX=$(ffprobe -v error -select_streams a:0 -show_entries stream=index -of csv=p=0 "$INP" 2>/dev/null | head -n1 | tr -d '\r\n' || true)
  fi
fi
SUB_MAPS=""
FFCSV=$(ffprobe -v error -select_streams s -show_entries stream=index,codec_name -of csv=p=0 "$INP" 2>/dev/null || true)
while IFS= read -r line; do
  [ -z "${line}" ] && continue
  idx="${line%%,*}"
  codec="${line##*,}"
  codec_lc=$(printf '%s' "$codec" | tr '[:upper:]' '[:lower:]')
  case "$codec_lc" in
    subrip|ass|ssa|webvtt|mov_text|srt|text)
      SUB_MAPS="${SUB_MAPS} -map 0:${idx}"
      ;;
  esac
done <<EOF
$(printf '%s\n' "$FFCSV")
EOF
RW_MAP_ARGS="-map 0:v:0"
if [ "$RW_HAS_AUD" = "1" ] && [ -n "$RW_AUD_IDX" ]; then
  RW_MAP_ARGS="${RW_MAP_ARGS} -map 0:${RW_AUD_IDX}"
fi
if [ -n "$SUB_MAPS" ]; then
  RW_MAP_ARGS="${RW_MAP_ARGS}${SUB_MAPS}"
  RW_SUB_CODEC="-c:s mov_text"
fi
RW_AUD_ARGS="-c:a aac -b:a ${RW_BA:-160}k"
if [ -n "$RW_SUB_CODEC" ] && [ "$RW_HAS_AUD" != "1" ]; then
  RW_AUD_ARGS=""
fi
nvidia-smi -L 2>/dev/null || true
rm -f /tmp/ffprog /tmp/fferr /tmp/rw_clip.txt
touch /tmp/ffprog /tmp/fferr
PROG_PUT="${RW_PROGRESS_PUT:-}"
RW_UPL_PID=""
if [ -n "${PROG_PUT:-}" ]; then
  touch /tmp/rw_ff_alive
  (
    while [ -f /tmp/rw_ff_alive ]; do
      { echo "===ffmpeg_progress==="; tail -n 60 /tmp/ffprog 2>/dev/null || true; echo "===ffmpeg_stderr==="; tail -n 50 /tmp/fferr 2>/dev/null || true; } > /tmp/rw_clip.txt || true
      if [ -s /tmp/rw_clip.txt ]; then
        curl -sf -X PUT -H "Content-Type: text/plain; charset=utf-8" --data-binary "@/tmp/rw_clip.txt" "${PROG_PUT}" || true
      fi
      sleep 4
    done
  ) &
  RW_UPL_PID=$!
fi
set +e
if [ "$GPUFF" != "/usr/bin/ffmpeg" ]; then
  # 1–2) Recent BtbN build: modern NVENC presets
  "$GPUFF" -hide_banner -nostdin -y -progress /tmp/ffprog -i "$INP" ${RW_MAP_ARGS} \
    -c:v h264_nvenc -preset p4 -tune hq -b:v "${RW_BR}" -maxrate "${RW_MR}" -bufsize "${RW_BF}" \
    -pix_fmt yuv420p ${RW_SUB_CODEC} ${RW_AUD_ARGS} -movflags +faststart /tmp/out.mp4 2>>/tmp/fferr
  RC=$?
  if [ "${RC}" != "0" ]; then
    "$GPUFF" -hide_banner -nostdin -y -progress /tmp/ffprog -hwaccel cuda -hwaccel_output_format cuda -i "$INP" ${RW_MAP_ARGS} \
      -c:v h264_nvenc -preset p4 -tune hq -b:v "${RW_BR}" -maxrate "${RW_MR}" -bufsize "${RW_BF}" \
      -pix_fmt yuv420p ${RW_SUB_CODEC} ${RW_AUD_ARGS} -movflags +faststart /tmp/out.mp4 2>>/tmp/fferr
    RC=$?
  fi
else
  # No BtbN URL: distro ffmpeg — avoid p4 (invalid on 4.x)
  "$GPUFF" -hide_banner -nostdin -y -progress /tmp/ffprog -i "$INP" ${RW_MAP_ARGS} \
    -c:v h264_nvenc -preset fast -b:v "${RW_BR}" -maxrate "${RW_MR}" -bufsize "${RW_BF}" \
    -pix_fmt yuv420p ${RW_SUB_CODEC} ${RW_AUD_ARGS} -movflags +faststart /tmp/out.mp4 2>>/tmp/fferr
  RC=$?
  if [ "${RC}" != "0" ]; then
    "$GPUFF" -hide_banner -nostdin -y -progress /tmp/ffprog -hwaccel cuda -hwaccel_output_format cuda -i "$INP" ${RW_MAP_ARGS} \
      -c:v h264_nvenc -preset fast -b:v "${RW_BR}" -maxrate "${RW_MR}" -bufsize "${RW_BF}" \
      -pix_fmt yuv420p ${RW_SUB_CODEC} ${RW_AUD_ARGS} -movflags +faststart /tmp/out.mp4 2>>/tmp/fferr
    RC=$?
  fi
fi
if [ "${RC}" != "0" ] && [ "$GPUFF" != "/usr/bin/ffmpeg" ]; then
  # 3) Distro ffmpeg + NVENC (different linkage than BtbN static; sometimes works when static fails)
  /usr/bin/ffmpeg -hide_banner -nostdin -y -progress /tmp/ffprog -i "$INP" ${RW_MAP_ARGS} \
    -c:v h264_nvenc -preset fast -b:v "${RW_BR}" -maxrate "${RW_MR}" -bufsize "${RW_BF}" \
    -pix_fmt yuv420p ${RW_SUB_CODEC} ${RW_AUD_ARGS} -movflags +faststart /tmp/out.mp4 2>>/tmp/fferr
  RC=$?
fi
if [ "${RC}" != "0" ]; then
  {
    echo "rw_transcode: all NVENC attempts failed; using libx264 CPU fallback (unexpected on Vast if GPU is healthy)."
    nvidia-smi -L 2>/dev/null || echo "rw_transcode: nvidia-smi unavailable"
  } >>/tmp/fferr
  /usr/bin/ffmpeg -hide_banner -nostdin -y -progress /tmp/ffprog -i "$INP" ${RW_MAP_ARGS} \
    -c:v libx264 -preset faster -b:v "${RW_BR}" -maxrate "${RW_MR}" -bufsize "${RW_BF}" \
    -pix_fmt yuv420p ${RW_SUB_CODEC} ${RW_AUD_ARGS} -movflags +faststart /tmp/out.mp4 2>>/tmp/fferr
  RC=$?
fi
rm -f /tmp/rw_ff_alive
if [ -n "${RW_UPL_PID:-}" ]; then wait "${RW_UPL_PID}" 2>/dev/null || true; fi
set -e
if [ "${RC}" != "0" ]; then
  echo "ffmpeg: all encode paths failed (rc=${RC})" >&2
  exit "${RC}"
fi
curl -f -X PUT -H "Content-Type: video/mp4" --upload-file /tmp/out.mp4 "${RW_OUT}"
"""

_REMOTE_LOG_MAX = 7800


def _trim_remote_log(text: str, *, max_len: int = _REMOTE_LOG_MAX) -> str:
    t = text.strip()
    if len(t) <= max_len:
        return t
    return t[-max_len:]


def _last_out_time_ms_from_remote(snippet: str) -> Optional[str]:
    for line in reversed(snippet.splitlines()):
        s = line.strip()
        if s.startswith("out_time_ms="):
            return s.split("=", 1)[-1].strip()
    return None


def run_vast_transcode_test(
    task_self: Any,
    job_token: str,
    src_ext: str,
    offer_id: Optional[int] = None,
    film_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Pick a Vast offer, create an instance whose onstart downloads the input from S3 (presigned GET),
    transcodes with NVENC on the Vast GPU (libx264 only if every NVENC path fails), uploads MP4 via presigned PUT;
    poll S3; destroy instance. If remote progress reports REDWOOD_VAST_NO_GPU=1 (no /dev/nvidia0), destroys
    the contract and auto-picks another offer (unless offer_id was fixed).
    """
    from core import vast_ai
    from core.s3 import (
        delete_object_key,
        get_object_text_if_small,
        object_size_or_none,
        presigned_put_url,
        presigned_stream_url,
    )
    from core.vast_transcode_cancel import clear_cancel_flag, is_cancel_requested

    s = get_settings()
    rid = str(getattr(task_self.request, "id", "") or "").strip() or None
    input_key = f"vast-test/{job_token}/input{src_ext}"
    output_key = f"vast-test/{job_token}/output.mp4"
    progress_key = f"vast-test/{job_token}/remote_progress.txt"
    ttl = int(s.VAST_TRANSCODE_URL_TTL_SEC)
    poll_sec = max(5, int(s.VAST_TRANSCODE_POLL_INTERVAL_SEC))
    max_wait = max(120, int(s.VAST_TRANSCODE_MAX_WAIT_SEC))
    instance_check_sec = max(30, int(getattr(s, "VAST_TRANSCODE_INSTANCE_CHECK_SEC", 60) or 60))
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
            progress_key=progress_key,
            src_ext=src_ext,
            celery_task_id=rid,
        )
        if is_cancel_requested(rid):
            raise RuntimeError("Cancelled by user")
        get_url = presigned_stream_url(input_key, expires=ttl)
        put_url = presigned_put_url(output_key, expires=ttl, content_type="video/mp4")
        progress_put_url = presigned_put_url(
            progress_key,
            expires=ttl,
            content_type="text/plain; charset=utf-8",
        )
        for stale in (output_key, progress_key):
            try:
                delete_object_key(stale)
            except Exception:
                pass

        meta(step="pick_offer", progress=10)
        if is_cancel_requested(rid):
            raise RuntimeError("Cancelled by user")
        picked_gpu_name: Optional[str] = None
        explicit_offer = offer_id is not None and int(offer_id) > 0
        skipped_offer_ids: list[int] = []
        max_no_gpu_rounds = max(
            1,
            min(25, int(getattr(s, "VAST_TRANSCODE_NO_GPU_MAX_RETRIES", 6) or 6)),
        )
        if explicit_offer:
            oid = int(offer_id)
        else:
            first = vast_ai.pick_first_verified_bundle_offer(
                vast_ai.default_gpu_name_list(),
                search_limit=64,
                skip_offer_ids=skipped_offer_ids,
            )
            oid = int(first["id"])
            picked_gpu_name = first.get("gpu_name") if isinstance(first.get("gpu_name"), str) else None

        br = f"{int(s.TRANSCODE_VIDEO_BITRATE_KBPS)}k"
        mr = f"{int(s.TRANSCODE_VIDEO_MAXRATE_KBPS)}k"
        bf = f"{int(s.TRANSCODE_VIDEO_BUFSIZE_KBPS)}k"
        audio_k = max(64, min(512, int(getattr(s, "TRANSCODE_AUDIO_BITRATE_KBPS", 160) or 160)))
        gpu_wait = max(5, int(s.VAST_TRANSCODE_GPU_DEVICE_WAIT_SEC))
        caps = (getattr(s, "VAST_TRANSCODE_NVIDIA_DRIVER_CAPABILITIES", None) or "all").strip() or "all"
        vis_dev = (getattr(s, "VAST_TRANSCODE_NVIDIA_VISIBLE_DEVICES", None) or "0").strip() or "0"
        ff_url = (getattr(s, "VAST_TRANSCODE_BTBH_FFMPEG_URL", None) or "").strip()
        aria_conn = max(1, min(32, int(getattr(s, "VAST_TRANSCODE_INPUT_ARIA2_CONN", 16) or 16)))
        aria_split = max(1, min(32, int(getattr(s, "VAST_TRANSCODE_INPUT_ARIA2_SPLIT", 16) or 16)))
        env = {
            "RW_IN": get_url,
            "RW_OUT": put_url,
            "RW_EXT": src_ext,
            "RW_BR": br,
            "RW_MR": mr,
            "RW_BF": bf,
            "RW_BA": str(audio_k),
            "RW_GPU_WAIT": str(gpu_wait),
            "NVIDIA_VISIBLE_DEVICES": vis_dev,
            "NVIDIA_DRIVER_CAPABILITIES": caps,
            "RW_DL_CONN": str(aria_conn),
            "RW_DL_SPLIT": str(aria_split),
            "RW_PROGRESS_PUT": progress_put_url,
        }
        if ff_url:
            env["RW_FFMPEG_URL"] = ff_url
        image = (s.VAST_TRANSCODE_DOCKER_IMAGE or "nvidia/cuda:12.3.1-runtime-ubuntu22.04").strip()
        disk = max(16, int(s.VAST_TRANSCODE_DISK_GB))

        output_ready = False
        for gpu_round in range(max_no_gpu_rounds):
            meta_kw: Dict[str, Any] = {
                "step": "create_vast_instance",
                "progress": 15,
                "offer_id": oid,
                "vast_image": image,
                "progress_key": progress_key,
                "vast_instance_round": gpu_round,
                "vast_max_no_gpu_rounds": max_no_gpu_rounds,
            }
            if picked_gpu_name:
                meta_kw["picked_gpu_name"] = picked_gpu_name
            meta(**meta_kw)
            if is_cancel_requested(rid):
                raise RuntimeError("Cancelled by user")
            raw: Optional[Dict[str, Any]] = None
            last_create_err: Optional[RuntimeError] = None
            max_create_attempts = max(
                3,
                min(
                    20,
                    int(getattr(s, "VAST_CREATE_INSTANCE_MAX_RETRIES", 10) or 10),
                ),
            )
            create_retry_delay = max(
                0.0,
                min(
                    5.0,
                    float(getattr(s, "VAST_CREATE_INSTANCE_RETRY_DELAY_SEC", 0.5) or 0.5),
                ),
            )
            for attempt in range(max_create_attempts):
                try:
                    raw = vast_ai.create_instance(
                        oid,
                        image=image,
                        disk_gb=disk,
                        runtype="ssh_direct",
                        label=f"redwood-vast-tx-{job_token[:10]}",
                        env=env,
                        onstart=VAST_TRANSCODE_ONSTART,
                    )
                    last_create_err = None
                    break
                except RuntimeError as e:
                    last_create_err = e
                    if not vast_ai.is_no_such_ask_error(e):
                        raise
                    if attempt >= max_create_attempts - 1:
                        raise RuntimeError(
                            f"Vast create_instance failed after {max_create_attempts} attempts "
                            f"(offers expire quickly; skipped offer ids: {skipped_offer_ids}). "
                            f"Last error: {e}"
                        ) from e
                    skipped_offer_ids.append(oid)
                    search_lim = min(128, 48 + (attempt + 1) * 16)
                    oid, picked_gpu_name = _repick_vast_bundle_offer(
                        skipped_offer_ids,
                        search_limit=search_lim,
                    )
                    if explicit_offer:
                        explicit_offer = False
                    logger.warning(
                        "vast create_instance stale offer (attempt %s/%s); "
                        "repicked offer_id=%s gpu=%s skipped=%s",
                        attempt + 1,
                        max_create_attempts,
                        oid,
                        picked_gpu_name or "?",
                        skipped_offer_ids,
                    )
                    meta_kw["offer_id"] = oid
                    if picked_gpu_name:
                        meta_kw["picked_gpu_name"] = picked_gpu_name
                    meta(**meta_kw)
                    if is_cancel_requested(rid):
                        raise RuntimeError("Cancelled by user")
                    if create_retry_delay > 0:
                        time.sleep(create_retry_delay)
            if raw is None:
                raise last_create_err or RuntimeError("Vast create_instance failed without response")
            raw_nid = raw.get("new_contract") if isinstance(raw, dict) else None
            if raw_nid is None:
                raise RuntimeError(f"Vast create_instance returned no new_contract: {raw!r}")
            inst_id = int(raw_nid)
            logger.info(
                "vast_transcode Celery task_id=%s vast_instance_id=%s offer_id=%s round=%s",
                rid,
                inst_id,
                oid,
                gpu_round,
            )

            meta(
                step="wait_output_on_s3",
                progress=20,
                vast_instance_id=inst_id,
                offer_id=oid,
                vast_instance_round=gpu_round,
                input_key=input_key,
                output_key=output_key,
                progress_key=progress_key,
                job_token=job_token,
                src_ext=src_ext,
                hint="Instance runs onstart (apt + ffmpeg + upload). First boot can take several minutes.",
            )
            deadline = time.monotonic() + max_wait
            no_gpu_abort = False
            last_instance_check = time.monotonic()
            while time.monotonic() < deadline:
                if is_cancel_requested(rid):
                    raise RuntimeError("Cancelled by user")
                if time.monotonic() - last_instance_check >= instance_check_sec:
                    last_instance_check = time.monotonic()
                    try:
                        inst_row = vast_ai.get_instance(inst_id)
                    except Exception:
                        logger.warning(
                            "vast_remote_transcode: get_instance failed id=%s (transient API?); retry next interval",
                            inst_id,
                            exc_info=True,
                        )
                    else:
                        if inst_row is None:
                            raise RuntimeError(
                                f"Vast instance {inst_id} no longer exists (API 404). "
                                "The contract was destroyed or is unavailable; transcoding cannot continue."
                            )
                        st = str(inst_row.get("actual_status") or "").strip().lower()
                        if st in ("exited", "offline"):
                            raise RuntimeError(
                                f"Vast instance {inst_id} is not usable (actual_status={st!r}). "
                                "The container stopped or the host went offline; transcoding cannot continue."
                            )
                sz = object_size_or_none(output_key)
                if sz is not None and sz > 256_000:
                    time.sleep(5)
                    sz2 = object_size_or_none(output_key)
                    if sz2 is not None and sz2 >= sz:
                        meta(
                            step="output_ready",
                            progress=92,
                            vast_instance_id=inst_id,
                            output_bytes=sz2,
                            input_key=input_key,
                            output_key=output_key,
                            progress_key=progress_key,
                            job_token=job_token,
                            src_ext=src_ext,
                        )
                        output_ready = True
                        break
                elapsed = max_wait - (deadline - time.monotonic())
                prog = 20 + min(70, int(70 * elapsed / max_wait))
                remote_snippet: Optional[str] = None
                raw_remote: Optional[str] = None
                try:
                    raw_remote = get_object_text_if_small(progress_key, max_bytes=65536)
                    if raw_remote and raw_remote.strip():
                        remote_snippet = _trim_remote_log(raw_remote)
                        if REDWOOD_VAST_NO_GPU_SENTINEL in raw_remote:
                            no_gpu_abort = True
                            break
                except Exception:
                    logger.debug("vast remote_progress fetch failed", exc_info=True)
                mwait: Dict[str, Any] = {
                    "step": "wait_output_on_s3",
                    "progress": prog,
                    "vast_instance_id": inst_id,
                    "output_bytes": sz,
                    "input_key": input_key,
                    "output_key": output_key,
                    "progress_key": progress_key,
                    "job_token": job_token,
                    "src_ext": src_ext,
                    "vast_instance_round": gpu_round,
                }
                if remote_snippet:
                    mwait["remote_log"] = remote_snippet
                    otm = _last_out_time_ms_from_remote(remote_snippet)
                    if otm:
                        mwait["remote_out_time_ms"] = otm
                meta(**mwait)
                time.sleep(poll_sec)

            if output_ready:
                break

            if no_gpu_abort:
                destroy_id = int(inst_id)
                try:
                    vast_ai.destroy_instance(destroy_id)
                    logger.warning(
                        "vast_remote_transcode: destroyed instance %s (no /dev/nvidia0 per remote progress)",
                        destroy_id,
                    )
                except Exception:
                    logger.exception(
                        "vast_remote_transcode: destroy_instance (no gpu) failed id=%s",
                        destroy_id,
                    )
                inst_id = None
                for k in (output_key, progress_key):
                    try:
                        delete_object_key(k)
                    except Exception:
                        pass
                if explicit_offer:
                    raise RuntimeError(
                        f"Vast instance had no GPU (/dev/nvidia0) for offer_id={oid}. "
                        "Pick another offer or retry later."
                    )
                if gpu_round >= max_no_gpu_rounds - 1:
                    raise RuntimeError(
                        f"No Vast instance exposed a GPU after {max_no_gpu_rounds} attempt(s). "
                        "See REDWOOD_VAST_NO_GPU=1 in remote progress; widen GPU filters or increase "
                        "VAST_TRANSCODE_GPU_DEVICE_WAIT_SEC."
                    )
                skipped_offer_ids.append(oid)
                oid, picked_gpu_name = _repick_vast_bundle_offer(
                    skipped_offer_ids,
                    search_limit=96,
                )
                logger.warning(
                    "vast_remote_transcode: repicking after no GPU; skipped_offer_ids=%s new_offer_id=%s",
                    skipped_offer_ids,
                    oid,
                )
                continue

            raise RuntimeError(
                "Timeout waiting for transcoded MP4 on S3. Check the instance logs on Vast.ai "
                "(ffmpeg / network / presigned URL expiry)."
            )

        if not output_ready:
            raise RuntimeError("vast_remote_transcode: internal state (no S3 output after rounds)")

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
        for k in (input_key, progress_key):
            try:
                delete_object_key(k)
            except Exception:
                logger.warning("vast_remote_transcode: could not delete %s", k)
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
