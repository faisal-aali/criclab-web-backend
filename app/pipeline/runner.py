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
from app.pipeline import timebase, track
from app.pipeline.view import flight_geometry_ok
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

        frame_w = int(meta.get("width") or pose_track.get("width") or 1280)
        frame_h = int(meta.get("height") or pose_track.get("height") or 720)

        await repo.update_job(job_id, status="processing", progress=55, stage="ball", message="Tracking ball flight")
        ball_track = _track_ball_seeded(video_path, meta, pose_track, action, scale)

        # --- Timebase: is the clip slow motion? ---
        # Every phase window ("the front foot plants 60-600 ms before release")
        # is cut in frames from fps, so a wrong fps mis-detects the events
        # themselves. The ball's own fall says what the capture rate really was.
        tb = timebase.measure_capture_fps(ball_track, scale.get("meters_per_pixel"), fps)

        # The ball path is indexed by frame, so it does not change with the
        # timebase and is never re-tracked here. What it does give us is the frame
        # the ball left the hand — a measurement, where release detection from
        # wrist speed alone is a heuristic. Re-run the action pass keyed to that
        # frame (and to the corrected rate) so FFC, BFC and MER are searched
        # relative to a real release rather than an estimated one.
        leave = action_mod.ball_leave_frame(
            pose_track, action.get("throwing_side"), ball_track, action.get("release_frame")
        )
        if tb.get("slow_motion"):
            fps = float(tb["fps"])
            pose_track["fps"] = fps
            # Per-point km/h was baked in at the container rate while tracking.
            # Leaving it would make the two ball-speed estimators disagree by
            # exactly the slow-motion factor, and the disagreement guard would
            # then refuse a speed we can measure perfectly well.
            ball_track = track.annotate_frame_motion(
                ball_track, fps, scale.get("meters_per_pixel")
            )
        if tb.get("slow_motion") or leave is not None:
            action = action_mod.analyze_action(
                pose_track, bowling_arm=bowling_arm, release_override=leave
            )

        # Keep a lock that would produce a false km/h from ever moving REL.
        geo_ok, _geo_reason = flight_geometry_ok(ball_track, frame_w, frame_h)
        # A flight that measurably crosses the image *is* the evidence that this
        # delivery happens in the image plane — stronger than the pose-based view
        # guess, which reads a mixed action as front-on. Pose-only clips still
        # defer to the view classifier inside metrics.
        if ball_track and not geo_ok:
            ball_track = []
            if leave is not None:  # release was pinned to a flight we just rejected
                action = action_mod.analyze_action(pose_track, bowling_arm=bowling_arm)
        elif ball_track:
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
            timebase_info=tb,
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
            capture_fps=fps,
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
        previous = await repo.list_deliveries(limit=8, player_name=player_name)
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
            "timebase": tb,
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
                "overlay_video_url": f"/artifacts/{job_id}/overlay.mp4",
                "pdf_url": f"/artifacts/{job_id}/bowling_report.pdf",
                "release_still_url": f"/artifacts/{job_id}/release.jpg",
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
        # Pitch direction (lead ankle − trail ankle) is where the ball actually
        # goes. Elbow→wrist at cocking points at the sky and would filter the
        # in-air white ball as "not downrange".
        lead = "left" if side == "right" else "right"
        la = pose_mod.point(rel, f"{lead}_ankle") if rel is not None else None
        ta = pose_mod.point(rel, f"{side}_ankle") if rel is not None else None
        if la is not None and ta is not None:
            pitch = (float(la[0] - ta[0]), float(la[1] - ta[1]))
            if float(np.hypot(pitch[0], pitch[1])) > 12:
                throw_dir = (pitch[0], min(0.0, pitch[1]) * 0.35)
        prev = action_mod.frame_by_index(pose_track.get("frames") or [], int(release) - 4)
        if prev is not None:
            wr0 = pose_mod.point(prev, f"{side}_wrist")
            if wr0 is not None:
                vel = (float(wr[0] - wr0[0]), float(wr[1] - wr0[1]))
                # Wrist delta wins only if the hand is actually moving into the air
                # (image Y decreases) *and* has a pitch-wise component.
                if float(np.hypot(vel[0], vel[1])) > 8 and vel[1] < 4 and abs(vel[0]) > abs(vel[1]) * 0.35:
                    throw_dir = vel
                elif throw_dir is None and elbow_dir is not None:
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
