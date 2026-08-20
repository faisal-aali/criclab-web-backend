# Product Brief — Cric-Lab

## Purpose

Cric-Lab is an **AI Cricket Bowling Laboratory**: upload a bowling video, get measurable bowling insights on the page, and download a professional PDF analysis report. Inspired by SpinLab AI’s video-analysis experience, focused on cricket bowling for v1.

**Core promise:** Two film modes. **Action** (side-on) → pose mechanics + honest 2D estimates. **Ball flight** (behind-bowler + both stumps) → pitch-plane speed, line, length. Gemma coaches and picks catalog drills — it never invents km/h.

Like **SpinLab AI** for quarterbacks (slowed video with overlaid analysis + a
biomechanics PDF), but for **cricket bowling**.

## Users

- Individual bowlers / athletes analyzing their own action
- Coaches reviewing bowling sessions
- Single-user or small coaching workflows first (auth can be light for MVP)

## Core Features (v1 — bowling only)

1. **Action upload** — side-on bowling clip (mechanics lab)
2. **Pose pipeline** — MediaPipe BlazePose landmarks per frame (measurement engine)
3. **Release & phases** — leave-hand release; back-foot → front-foot →
   arm-horizontal → release → follow-through (omit if not seen)
4. **Metrics dashboard** — **ball speed** only with an in-air lock (image-plane + height; not a gun), leave-hand arm/hand
   speed, release height/angle/time, joint angles, stride, rotation proxies
   (estimated), action scores from `status === ok` metrics only
5. **Slow-motion overlay clip** — grayscale footage with SpinLab-style HUD; tiles match JSON
6. **AI analysis** — Gemma (Ollama) coaches from structured metrics only; picks YouTube drills from a **closed catalog**
7. **PDF report** — SpinLab-style cricket report + drill URLs as text
8. **Ball flight** — behind-bowler clip + stump calibration → ICC-style speed/line/length (pitch-plane, not radar)
9. **Train** — catalog library + per-delivery DrillShelf iframes
10. **History** — store analyses in MongoDB; compare future deliveries with past sessions

## Success Metrics

| Metric | Target |
|--------|--------|
| End-to-end: upload → results page → PDF | Works on controlled bowling videos |
| Release frame identified | Detectable on side-on / clear deliveries |
| Metrics shown with confidence | Always (estimates labeled as estimates) |
| Analysis persisted | MongoDB document per session/delivery |
| PDF downloadable | After analysis completes |
| AI summary grounded in metrics | No hallucinated radar-grade claims |

## Out of Scope (v1)

- Batting, fielding, wicket-keeping analysis
- Multi-camera synchronized capture
- Claiming radar-gun accuracy without calibration + ground-truth validation
- Full multi-tenant SaaS / team billing
- Frame-by-frame measurement by the LLM

## Product Principles

- **CV + physics first, LLM second** — Gemma coaches and selects catalog drills; it does not measure
- **Estimates until calibrated** — Action km/h is 2D + height; Ball flight is stump pitch-plane, still not a radar gun
- **Two modes, two truths** — never merge stump speed into a pose job
- **Bowling-only MVP** — reliable workflow over every cricket scenario
- **Modular pipeline** — new metrics / sports later without rewriting the whole system
- **SpinLab-like clarity** — clean results page: video, metric cards, charts, AI summary, PDF
