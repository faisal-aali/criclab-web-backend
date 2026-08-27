from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

import numpy as np

from app.balltrack import cloud, repo
from app.balltrack.calibrate import homography_from_boxes
from app.balltrack.detect import collect_candidates
from app.balltrack.metrics import analyze_delivery
from app.balltrack.render import write_clip, write_overlay, write_pitch_map
from app.balltrack.split import split_deliveries
from app.balltrack.track import build_tracks
from app.balltrack.validate import reject_reason
from app.config import get_settings
from app.pipeline.cv_vision import validate_ball_path_on_video


async def run_balltrack_job(*, job_id: str, session_id: str, video_path: Path, calibration: dict[str, Any]) -> None:
    settings = get_settings()
    try:
        await repo.update_job(job_id, status="processing", progress=8, stage="calibrate", message="Building pitch homography")
        art = settings.storage_path / "balltrack" / job_id
        art.mkdir(parents=True, exist_ok=True)

        await repo.update_job(job_id, progress=18, stage="detect", message="Finding the ball")
        frames, meta = collect_candidates(video_path)
        fps = float(meta["fps"] or 30.0)
        w, h = int(meta["width"]), int(meta["height"])
        if w < 16 or h < 16:
            raise ValueError("Could not read video dimensions")

        cal = homography_from_boxes(
            calibration.get("bowler") or {},
            calibration.get("batter") or {},
            w,
            h,
            pitch_length_m=float(calibration.get("pitch_length_m") or 20.12),
        )
        H = np.array(cal["H"], dtype=np.float64)

        await repo.update_job(job_id, progress=40, stage="track", message="Fitting ball trajectories")
        tracks = build_tracks(frames, w, h, fps=fps)
        deliveries_pts = split_deliveries(tracks, fps)
        if not deliveries_pts:
            raise ValueError(
                "No cricket ball detected. Film a real delivery down the pitch — empty or walking clips will not produce a speed."
            )

        await repo.update_job(job_id, progress=58, stage="metrics", message=f"Checking {len(deliveries_pts)} candidate paths")
        analyzed: list[dict[str, Any]] = []
        delivery_ids: list[str] = []
        for pts in deliveries_pts:
            metrics = analyze_delivery(
                pts,
                H,
                fps,
                cal["pitch_length_m"],
                cal["pitch_width_m"],
            )
            why = reject_reason(metrics, fps, h, cal["pitch_length_m"], cal["pitch_width_m"])
            if why:
                continue
            flow_ok, _flow_why, _flow = validate_ball_path_on_video(video_path, pts, w, h)
            if not flow_ok:
                continue
            i = len(analyzed)
            did = repo.new_id("btd")
            clip_path = art / f"ball_{i + 1}.mp4"
            write_clip(
                video_path,
                clip_path,
                metrics["start_frame"],
                metrics["end_frame"],
                fps,
                delivery=metrics,
                H_inv=np.array(cal["H_inv"], dtype=np.float64),
                pitch_length_m=cal["pitch_length_m"],
                stump_width_m=float(cal.get("stump_width_m") or 0.2286),
                bowler_box=calibration.get("bowler"),
                batter_box=calibration.get("batter"),
            )
            clip_cloud = cloud.upload_video(clip_path, f"{job_id}_ball_{i + 1}")
            clip_url = (clip_cloud or {}).get("playback_url") or f"/balltrack/media/{job_id}/ball_{i + 1}.mp4"
            bounce = metrics.get("bounce") or {}
            doc = {
                "_id": did,
                "session_id": session_id,
                "job_id": job_id,
                "index": i + 1,
                "created_at": repo.utcnow(),
                "metrics": {
                    "speed_kmh": metrics["speed_kmh"],
                    "line_m": metrics["line_m"],
                    "length_m": metrics["length_m"],
                },
                "bounce": {
                    "length_m": bounce.get("length_m"),
                    "width_m": bounce.get("width_m"),
                    "frame": bounce.get("frame"),
                },
                "n_points": metrics["n_points"],
                "start_frame": metrics["start_frame"],
                "end_frame": metrics["end_frame"],
                "artifacts": {
                    "clip_url": clip_url,
                    "clip_path": str(clip_path),
                    "cloudinary_clip_url": (clip_cloud or {}).get("playback_url"),
                },
            }
            await repo.insert_delivery(doc)
            delivery_ids.append(did)
            analyzed.append({**metrics, "index": i + 1, "bounce": bounce})

        if not analyzed:
            raise ValueError(
                "No cricket ball detected. Nothing in this clip looked like a delivery (speed, bounce, and path toward the batter must all check out)."
            )

        await repo.update_job(job_id, progress=78, stage="render", message="Rendering overlay and pitch map")
        overlay_path = art / "overlay.mp4"
        H_inv = np.array(cal["H_inv"], dtype=np.float64)
        write_overlay(
            video_path,
            overlay_path,
            analyzed,
            fps,
            H_inv=H_inv,
            pitch_length_m=cal["pitch_length_m"],
            pitch_width_m=cal["pitch_width_m"],
            stump_width_m=float(cal.get("stump_width_m") or 0.2286),
            bowler_box=calibration.get("bowler"),
            batter_box=calibration.get("batter"),
        )
        map_path = art / "pitch_map.png"
        write_pitch_map(map_path, analyzed, cal["pitch_length_m"], cal["pitch_width_m"])
        ov_cloud = cloud.upload_video(overlay_path, f"{job_id}_overlay")
        map_cloud = cloud.upload_image(map_path, f"{job_id}_pitchmap")

        artifacts = {
            "overlay_url": (ov_cloud or {}).get("playback_url") or f"/balltrack/media/{job_id}/overlay.mp4",
            "pitch_map_url": (map_cloud or {}).get("secure_url") or f"/balltrack/media/{job_id}/pitch_map.png",
            "cloudinary_overlay_url": (ov_cloud or {}).get("playback_url"),
            "cloudinary_pitch_map_url": (map_cloud or {}).get("secure_url"),
        }

        await repo.update_job(job_id, progress=90, stage="agent", message="Coaching drills from measured line and length")
        from app.agent import ollama_agent
        from app.coaching.recommend import balltrack_tags

        first = analyzed[0]
        flight_metrics = {
            "ball_speed_kmh": first.get("speed_kmh"),
            "line_m": first.get("line_m"),
            "length_m": first.get("length_m"),
            "scale": {"calibrated": True, "method": "stump_homography", "note": "Pitch-plane from both stump sets"},
            "quality": {
                "camera_view": "behind_bowler",
                "camera_view_note": "Stump homography — pitch-plane speed, not a radar gun",
                "speed_view_ok": True,
                "calibrated": True,
            },
        }
        tags = balltrack_tags(analyzed)
        analysis = await ollama_agent.generate_report(
            metrics=flight_metrics,
            candidate_tags=tags,
            player_name="Bowler",
        )

        await repo.update_session(
            session_id,
            status="completed",
            delivery_ids=delivery_ids,
            delivery_count=len(delivery_ids),
            calibration_used=cal,
            artifacts=artifacts,
            analysis=analysis,
            meta=meta,
        )
        await repo.update_job(
            job_id,
            status="completed",
            progress=100,
            stage="done",
            message=f"Tracked {len(delivery_ids)} deliveries",
            session_id=session_id,
        )
    except Exception as exc:
        await repo.update_job(
            job_id,
            status="failed",
            progress=100,
            stage="failed",
            message=str(exc),
            error=traceback.format_exc()[-1500:],
        )
        await repo.update_session(session_id, status="failed", error=str(exc))
