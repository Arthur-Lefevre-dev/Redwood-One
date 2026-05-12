"""Unified film processing: ffprobe → transcode decision → TMDB → S3."""

import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from config import get_settings
from core.ffprobe import (
    FFprobeError,
    probe,
    probe_has_audio_stream,
    summarize,
    text_subtitle_stream_indices_from_probe,
)
from core.gpu_detect import get_encoder
from core.logging_json import log_event
from core.s3 import build_object_key, presigned_stream_url, upload_file
from core.tmdb import enrich_from_filename
from db.models import ContentKind, Film, FilmStatut, FilmTraitement

logger = logging.getLogger(__name__)


def decide_processing(path: str, meta: Dict[str, Any]) -> Tuple[FilmTraitement, bool]:
    """
    Returns (traitement, needs_transcode).
    needs_transcode False means upload source file as-is.
    """
    suf = Path(path).suffix.lower()
    if suf != ".mp4":
        return FilmTraitement.transcode, True
    return FilmTraitement.direct, False


def _build_ffmpeg_cmd(
    input_path: str,
    output_path: str,
    use_h265: bool,
    *,
    subtitle_stream_indices: Optional[list[int]] = None,
    has_audio: bool = True,
) -> list[str]:
    enc = get_encoder()
    cmd: list[str] = ["ffmpeg", "-y"]
    if enc.get("hwaccel") == "-hwaccel" and enc.get("hwaccel_device") == "cuda":
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    elif enc.get("hwaccel") == "-hwaccel" and enc.get("hwaccel_device") == "qsv":
        cmd += ["-hwaccel", "qsv"]
    elif enc.get("hwaccel") == "vaapi" and enc.get("hwaccel_device"):
        cmd += ["-hwaccel", "vaapi", "-hwaccel_device", str(enc["hwaccel_device"])]
    cmd += ["-i", input_path]
    subs = [int(x) for x in (subtitle_stream_indices or [])]
    if subs:
        cmd += ["-map", "0:v:0"]
        if has_audio:
            cmd += ["-map", "0:a:0"]
        for idx in subs:
            cmd += ["-map", f"0:{idx}"]
        cmd += ["-c:s", "mov_text"]
    vcodec = enc["h265"] if use_h265 else enc["h264"]
    cmd += ["-c:v", vcodec]
    s = get_settings()
    br = f"{int(s.TRANSCODE_VIDEO_BITRATE_KBPS)}k"
    maxr = f"{int(s.TRANSCODE_VIDEO_MAXRATE_KBPS)}k"
    buf = f"{int(s.TRANSCODE_VIDEO_BUFSIZE_KBPS)}k"
    audio_k = max(64, min(512, int(getattr(s, "TRANSCODE_AUDIO_BITRATE_KBPS", 160) or 160)))
    ab = f"{audio_k}k"
    # Target average bitrate (VBV). No scale / no -r: resolution and fps follow the source.
    cmd += ["-b:v", br, "-maxrate", maxr, "-bufsize", buf]
    # CPU libx264/libx265: force 8-bit 4:2:0 so 10-bit BluRay/HDR-like sources mux cleanly to MP4.
    if enc.get("vendor") == "cpu":
        cmd += ["-pix_fmt", "yuv420p"]
    if enc["vendor"] != "cpu" and "_vaapi" not in vcodec:
        if "_nvenc" in vcodec or "_amf" in vcodec:
            cmd += ["-rc:v", "vbr"]
    # FFmpeg 4.x (e.g. Ubuntu 22.04) has no -fps_mode; -vsync 0 passes timestamps like passthrough.
    if subs and not has_audio:
        cmd += ["-vsync", "0", str(output_path)]
    else:
        cmd += ["-c:a", "aac", "-b:a", ab, "-vsync", "0", str(output_path)]
    return cmd


_FFMPEG_TIME = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")


def _ffmpeg_time_to_sec(s: str) -> float:
    m = _FFMPEG_TIME.search(s)
    if not m:
        return -1.0
    h, mi, se = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + se


def transcode_to_mp4(
    input_path: str,
    output_path: str,
    use_h265: bool = True,
    duration_sec: float = 0.0,
    progress_frac: Optional[Callable[[float], None]] = None,
    input_probe: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Run ffmpeg; optional progress_frac(0..1) from stderr time= vs duration_sec.
    progress_frac may be invoked from a side thread — caller must be thread-safe.
    When input_probe is set, text subtitle streams may be muxed as mov_text for browser captions.
    """
    probe_in = input_probe if input_probe is not None else probe(input_path)
    sub_idx = text_subtitle_stream_indices_from_probe(probe_in)
    has_audio = probe_has_audio_stream(probe_in)

    def run_ffmpeg(cmd: list[str]) -> None:
        log_event(logger, "ffmpeg_start", cmd=" ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert proc.stderr is not None
        last_emit = [0.0, -1.0]  # monotonic time, last emitted fraction
        err_tail: deque[str] = deque(maxlen=120)

        def emit(frac: float) -> None:
            if not progress_frac:
                return
            f = max(0.0, min(1.0, frac))
            f = max(f, last_emit[1])
            now = time.monotonic()
            if f < 0.999 and abs(f - last_emit[1]) < 0.012 and (now - last_emit[0]) < 1.5:
                return
            last_emit[0] = now
            last_emit[1] = f
            progress_frac(f)

        def read_stderr() -> None:
            dur = float(duration_sec or 0.0)
            best = 0.0
            try:
                for line in iter(proc.stderr.readline, ""):
                    if not line:
                        break
                    err_tail.append(line.rstrip("\n\r"))
                    t = _ffmpeg_time_to_sec(line)
                    if t < 0:
                        continue
                    best = max(best, t)
                    if dur > 1.0:
                        emit(best / dur)
                    elif progress_frac:
                        emit(min(1.0, best / 300.0))
            finally:
                try:
                    proc.stderr.close()
                except OSError:
                    pass

        th = threading.Thread(target=read_stderr, name="ffmpeg-stderr", daemon=True)
        th.start()
        try:
            rc = proc.wait(timeout=86400)
        finally:
            th.join(timeout=12)
        if rc != 0:
            tail = "\n".join(err_tail).strip()
            snippet = tail[-4000:] if tail else "(no stderr lines captured)"
            log_event(logger, "ffmpeg_failed", returncode=rc, stderr_tail=snippet[-2000:])
            raise RuntimeError(f"ffmpeg exited with code {rc}\n{snippet}")
        if progress_frac:
            progress_frac(1.0)
        log_event(logger, "ffmpeg_done", output=output_path)

    cmd_with_subs = _build_ffmpeg_cmd(
        input_path,
        output_path,
        use_h265,
        subtitle_stream_indices=sub_idx if sub_idx else None,
        has_audio=has_audio,
    )
    try:
        run_ffmpeg(cmd_with_subs)
    except RuntimeError:
        if not sub_idx:
            raise
        log_event(
            logger,
            "ffmpeg_subtitles_fallback",
            input_path=input_path,
            subtitle_indices=sub_idx,
            message="retry transcode without subtitle mux",
        )
        cmd_plain = _build_ffmpeg_cmd(
            input_path,
            output_path,
            use_h265,
            subtitle_stream_indices=None,
            has_audio=has_audio,
        )
        run_ffmpeg(cmd_plain)


ProgressCb = Optional[Callable[[int], None]]


def process_film_file(
    db: Session,
    film: Film,
    local_path: str,
    progress: ProgressCb = None,
) -> None:
    """Run full pipeline for an existing Film row."""
    try:
        if progress:
            progress(5)
        data = probe(local_path)
        meta = summarize(data)
        film.codec_video = meta.get("codec_video")
        film.codec_audio = meta.get("codec_audio")
        film.resolution = meta.get("resolution")
        film.bitrate_kbps = meta.get("bitrate_kbps")
        film.taille_octets = meta.get("size_bytes")
        film.duree_min = meta.get("duration_min")
        db.commit()

        traitement, needs_tx = decide_processing(local_path, meta)
        film.traitement = traitement
        db.commit()
        log_event(
            logger,
            "pipeline_decision",
            film_id=film.id,
            traitement=traitement.value,
            needs_transcode=needs_tx,
        )
        if progress:
            progress(25)

        enrich = enrich_from_filename(Path(local_path).name, film.content_kind)
        for k, v in enrich.items():
            if hasattr(film, k) and v is not None:
                setattr(film, k, v)
        db.commit()
        if progress:
            progress(45)

        work_path = local_path
        tmp_out: Optional[str] = None
        if needs_tx:
            tmp_out = str(
                Path("/tmp/redwood/uploads") / f"{film.id}_out_{Path(local_path).stem}.mp4"
            )
            use_h265 = True
            dur = float(meta.get("duration_sec") or 0.0)

            def tx_progress(frac: float) -> None:
                if progress:
                    progress(45 + int(max(0.0, min(1.0, frac)) * 30))

            transcode_to_mp4(
                local_path,
                tmp_out,
                use_h265=use_h265,
                duration_sec=dur,
                progress_frac=tx_progress,
                input_probe=data,
            )
            work_path = tmp_out
            out_data = probe(work_path)
            out_meta = summarize(out_data)
            film.codec_video = out_meta.get("codec_video")
            film.codec_audio = out_meta.get("codec_audio")
            film.resolution = out_meta.get("resolution")
            film.bitrate_kbps = out_meta.get("bitrate_kbps")
            sz = int(out_meta.get("size_bytes") or 0)
            if sz <= 0:
                sz = int(Path(work_path).stat().st_size)
            film.taille_octets = sz if sz > 0 else None
            film.duree_min = out_meta.get("duration_min")
            db.commit()
            log_event(
                logger,
                "pipeline_post_transcode_probe",
                film_id=film.id,
                taille_octets=film.taille_octets,
            )
            if progress:
                progress(75)

        key = build_object_key(film.id, Path(work_path).name)
        base_pg = 75 if needs_tx else 45
        span_pg = 24 if needs_tx else 54
        last_s3_pct = [-1]

        def s3_upload_progress(transferred: int, total: int) -> None:
            if not progress:
                return
            tot = max(1, int(total))
            frac = min(1.0, float(transferred) / float(tot))
            p = base_pg + int(frac * span_pg)
            p = min(99, max(base_pg, p))
            if p <= last_s3_pct[0]:
                return
            last_s3_pct[0] = p
            progress(p)

        upload_file(work_path, key, progress_callback=s3_upload_progress)
        film.s3_key = key
        from config import get_settings

        film.s3_bucket = get_settings().S3_BUCKET_NAME
        film.url_streaming = presigned_stream_url(key, expires=86400)
        film.statut = FilmStatut.disponible
        film.erreur_message = None
        film.pipeline_progress = 100
        db.commit()
        log_event(logger, "pipeline_complete", film_id=film.id, s3_key=key)

        try:
            os.remove(local_path)
        except OSError:
            pass
        if tmp_out and tmp_out != local_path:
            try:
                os.remove(tmp_out)
            except OSError:
                pass
    except FFprobeError as e:
        _fail(db, film, str(e))
    except Exception as e:
        logger.exception("pipeline error film_id=%s", film.id)
        _fail(db, film, str(e))


def _fail(db: Session, film: Film, message: str) -> None:
    film.statut = FilmStatut.erreur
    film.erreur_message = message[:8000]
    film.pipeline_progress = None
    db.commit()
    log_event(logger, "pipeline_error", film_id=film.id, error=message)
