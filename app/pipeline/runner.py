"""End-to-end bowling analysis pipeline runner.

Order (see memory-bank/systemPatterns.md):
  extract meta → pose estimation → action/release detection → scale
    → (best-effort) ball tracking → biomechanics metrics
      → slow-motion overlay video → Cloudinary upload
        → Gemma coaching narrative → Cric-Lab PDF → Cloudinary upload
          → persist MongoDB delivery.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

import numpy as np

from app.agent import ollama_agent
from app.config import get_settings
from app.db import repository as repo
from app.pdf.report import build_pdf
from app.pipeline import action as action_mod
from app.pipeline import calibrate, extract, pose as pose_mod
from app.pipeline import metrics as metrics_mod
from app.pipeline import render as render_mod
from app.pipeline import track
from app.pipeline.view import classify_camera_view, flight_geometry_ok
from app.services import cloudinary_service


async def run_analysis_job(
    *,
    job_id: str,
    video_id: str,
    video_path: Path,
    player_name: str = "Bowler",
    meters_per_pixel: float | None = None,
    reference_height_m: float | None = None,
    player_profile: dict[str, Any] | None = None,
) -> None:
    settings = get_settings()
    try:
        await repo.update_job(job_id, status="processing", progress=5, stage="extract", message="Reading video metadata")
        meta = extract.extract_video_meta(video_path)
        fps = float(meta["fps"] or 30.0)

        artifact_dir = settings.storage_path / "artifacts" / job_id
        artifact_dir.mkdir(parents=True, exist_ok=True)

        # --- Pose estimation (the measurement engine) ---
        await repo.update_job(job_id, status="processing", progress=20, stage="pose", message="Estimating bowler pose (MediaPipe)")
        pose_track = pose_mod.extract_pose_track(video_path)
        if not pose_track.get("frames"):
            raise ValueError("No bowler pose detected — use a clearer, side-on video of the delivery.")

        # --- Action / release detection ---
        await repo.update_job(job_id, status="processing", progress=42, stage="action", message="Detecting release & action phases")
        bowling_arm = (player_profile or {}).get("bowling_arm")
        action = action_mod.analyze_action(pose_track, bowling_arm=bowling_arm)

        # --- Scale from upright (tall) body frames — never crouch-biased median ---
        body_heights = [h for h in (pose_mod.body_pixel_height(f) for f in pose_track["frames"]) if h]
        body_px = calibrate.upright_body_px_height(body_heights)
        scale = calibrate.resolve_scale(meters_per_pixel, reference_height_m, body_px)

        DRAW_BALL = True
        ball_track: list[dict[str, Any]] = []
        if DRAW_BALL:
            await repo.update_job(job_id, status="processing", progress=55, stage="ball", message="Tracking ball flight")
            ball_track = _track_ball_seeded(video_path, meta, pose_track, action, scale)
            # Drop a lock that would produce a false km/h *before* it can move REL.
            view = classify_camera_view(pose_track, action)
            geo_ok, _geo_reason = flight_geometry_ok(
                ball_track,
                int(meta.get("width") or pose_track.get("width") or 1280),
                int(meta.get("height") or pose_track.get("height") or 720),
            )
            if ball_track and (not view.get("speed_ok") or not geo_ok):
                ball_track = []
            if ball_track:
                action_mod.snap_release_to_ball_leave(pose_track, action, ball_track)

        # --- Metrics ---
        await repo.update_job(job_id, status="processing", progress=60, stage="metrics", message="Calculating bowling metrics")
        metrics = metrics_mod.compute_metrics(
            fps=fps,
            pose_track=pose_track,
            action=action,
            scale=scale,
            ball_track=ball_track,
            player_profile=player_profile,
        )

        # --- Slow-motion overlay video ---
        await repo.update_job(job_id, status="processing", progress=70, stage="render", message="Rendering slow-motion overlay video")
        overlay_video_path = artifact_dir / "overlay.mp4"
        release_still_path = artifact_dir / "release.jpg"
        stills_dir = artifact_dir / "stills"
        render_info = render_mod.render_overlay_video(
            video_path=video_path,
            out_path=overlay_video_path,
            pose_track=pose_track,
            action=action,
            metrics=metrics,
            ball_track=ball_track,
            player_name=player_name,
            release_still_path=release_still_path,
            stills_dir=stills_dir,
        )

        # --- Upload processed video to Cloudinary ---
        await repo.update_job(job_id, status="processing", progress=80, stage="upload", message="Uploading processed video to Cloudinary")
        cloud: dict[str, Any] = {"configured": cloudinary_service.is_configured()}
        try:
            vres = cloudinary_service.upload_video(overlay_video_path, public_id=f"{job_id}_overlay")
            if vres:
                cloud["video"] = vres
        except Exception as e:  # never fail the whole job on upload error
            cloud["video_error"] = str(e)

        # --- Agent narrative ---
        await repo.update_job(job_id, status="analyzing", progress=86, stage="agent", message="Generating AI coaching analysis")
        previous = await repo.list_deliveries(limit=5)
        prev_metrics = [d.get("metrics") for d in previous if d.get("metrics")]
        comparison = ollama_agent.compare_deliveries(metrics, prev_metrics[:5])
        analysis = await ollama_agent.generate_report(
            metrics=metrics,
            comparison=comparison,
            player_name=player_name,
            player_profile=player_profile,
        )

        # --- PDF ---
        await repo.update_job(job_id, status="analyzing", progress=92, stage="pdf", message="Building PDF report")
        pdf_path = artifact_dir / "bowling_report.pdf"
        created = repo.utcnow()
        build_pdf(
            out_path=pdf_path,
            player_name=player_name,
            delivery_id=job_id,
            metrics=metrics,
            analysis=analysis,
            release_still=release_still_path if release_still_path.exists() else None,
            date_str=created.strftime("%b-%d-%Y"),
            chart_dir=artifact_dir,
            stills_dir=stills_dir if stills_dir.exists() else None,
        )
        try:
            pres = cloudinary_service.upload_pdf(pdf_path, public_id=f"{job_id}_report")
            if pres:
                cloud["pdf"] = pres
        except Exception as e:
            cloud["pdf_error"] = str(e)

        # --- Persist ---
        delivery_id = repo.new_id("del")
        delivery = {
            "_id": delivery_id,
            "job_id": job_id,
            "video_id": video_id,
            "player_name": player_name,
            "player_profile": player_profile,
            "created_at": created,
            "meta": meta,
            "action": _strip_series(action),
            "release": {"frame": action.get("release_frame"), **(metrics.get("release_point") or {})},
            "metrics": _strip_metric_series(metrics),
            "analysis": analysis,
            "render": render_info,
            "cloudinary": cloud,
            "artifacts": {
                "release_still": str(release_still_path),
                "overlay_video": str(overlay_video_path),
                "stills_dir": str(stills_dir),
                "pdf": str(pdf_path),
                "cloudinary_video_url": (cloud.get("video") or {}).get("playback_url"),
                "cloudinary_pdf_url": (cloud.get("pdf") or {}).get("secure_url"),
            },
        }
        await repo.insert_delivery(delivery)

        await repo.update_job(
            job_id, status="completed", progress=100, stage="done", message="Analysis complete",
            delivery_id=delivery_id,
            result={
                "delivery_id": delivery_id,
                "cloudinary_video_url": (cloud.get("video") or {}).get("playback_url"),
                "cloudinary_pdf_url": (cloud.get("pdf") or {}).get("secure_url"),
            },
        )
    except Exception as e:
        await repo.update_job(
            job_id, status="failed",
            message=str(e), error=traceback.format_exc(),
        )


def _strip_series(action: dict[str, Any]) -> dict[str, Any]:
    """Keep the action summary small in Mongo (drop the per-frame speed series)."""
    a = dict(action)
    a.pop("wrist_speed_series", None)
    return a


def _strip_metric_series(metrics: dict[str, Any]) -> dict[str, Any]:
    """rotation_series only feeds the PDF sequencing chart, which is already
    built by now — hundreds of rows per 200 fps clip don't belong in every
    history-listing response."""
    m = dict(metrics)
    m.pop("rotation_series", None)
    return m


def _track_ball_seeded(
    video_path: Path,
    meta: dict[str, Any],
    pose_track: dict[str, Any],
    action: dict[str, Any],
    scale: dict[str, Any],
) -> list[dict[str, Any]]:
    """Track the ball leaving the bowling wrist at the detected release frame."""
    try:
        release = action.get("release_frame")
        side = action.get("throwing_side")
        if release is None or not side:
            return []
        rel = action_mod.frame_by_index(pose_track.get("frames") or [], release)
        wr = pose_mod.point(rel, f"{side}_wrist") if rel is not None else None
        if wr is None:
            return []
        throw_dir = None
        el = pose_mod.point(rel, f"{side}_elbow") if rel is not None else None
        elbow_dir = None
        if el is not None:
            elbow_dir = (float(wr[0] - el[0]), float(wr[1] - el[1]))
            throw_dir = elbow_dir
        prev = action_mod.frame_by_index(pose_track.get("frames") or [], int(release) - 4)
        if prev is not None:
            wr0 = pose_mod.point(prev, f"{side}_wrist")
            if wr0 is not None:
                vel = (float(wr[0] - wr0[0]), float(wr[1] - wr0[1]))
                # Wrist delta wins only if the hand is actually moving into the air
                # (image Y decreases). Otherwise keep elbow→wrist.
                if float(np.hypot(vel[0], vel[1])) > 8 and vel[1] < 4:
                    throw_dir = vel
                elif elbow_dir is not None:
                    throw_dir = elbow_dir
        fps = float(meta.get("fps") or pose_track.get("fps") or 30.0)
        return track.track_ball_from_release(
            video_path,
            release_frame=int(release),
            wrist_xy=(float(wr[0]), float(wr[1])),
            fps=fps,
            meters_per_pixel=scale.get("meters_per_pixel"),
            frame_w=int(meta.get("width") or pose_track.get("width") or 1280),
            frame_h=int(meta.get("height") or pose_track.get("height") or 720),
            throw_dir=throw_dir,
        )
    except Exception:
        return []
