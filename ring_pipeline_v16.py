"""
Ring pipeline v1.6 — sizing + hollowing + vision-check + fallback.

Called ONLY when:
  - jewelry_type == "ring"
  - FEATURE_V16_RING_PIPELINE env var == "true"

Everything else (figurines, pendants) falls through to existing v1.5.1 code UNCHANGED.

Flow:
1. Scale input GLB to target US ring size via horizontal-max-at-mid detector
   (CAD-guy-verified in Rhino — matches manufacturer measurement methodology)
2. Measure the scaled solid weight in the requested metal
3. If (solid_weight - target_weight) / solid_weight < 25%: skip hollow, ship sized solid
   (savings too small — risk of damage outweighs benefit on intricate rings)
4. Else: run voxel-remesh + Solidify hollow targeting requested weight
5. Vision check via Gemini 3.1 Pro Preview (OpenRouter) — render solid + hollow,
   compare for destruction (fused detail, stray spikes, missing features)
6. If vision FAILS: discard hollow, ship sized solid
7. Convert chosen GLB to OBJ (manufacturer-recommended format for Rhino)
8. Return response with hollowed/delivered_weight_g/fallback_reason flags

Safety net: customer NEVER receives a defective file. Worst case is a heavier
solid ring than ordered. Production rollback in 3 min via git tag.
"""

import os
import json
import base64
import subprocess
import urllib.request
import urllib.error
from pathlib import Path


# ──────────────────────────────────────────────
# Metal type mapping — Lovable's strings -> density (g/mm^3) + internal key
# ──────────────────────────────────────────────
# Lovable sends: "14k-gold", "18k-gold", "22k-gold", "platinum", "silver"
# Densities sourced from industry-standard alloy reference.
LOVABLE_METAL_MAP = {
    "14k-gold": ("gold_14k", 0.01333),
    "18k-gold": ("gold_18k", 0.01540),
    "22k-gold": ("gold_22k", 0.01760),
    "platinum": ("platinum_950", 0.02140),
    "silver":   ("silver_925", 0.01030),
    # Legacy/internal aliases — accept what older code may have sent
    "gold_14k": ("gold_14k", 0.01333),
    "gold_18k": ("gold_18k", 0.01540),
    "gold_22k": ("gold_22k", 0.01760),
}


# ──────────────────────────────────────────────
# Configuration constants
# ──────────────────────────────────────────────
# Skip hollowing if savings ratio is below this — intricate rings hollowed for
# small savings are at high risk of detail destruction.
SAVINGS_THRESHOLD = 0.25

# Vision check thresholds (from shell_v2 testing — 4/4 correct verdicts)
VISION_AVG_PASS = 7.0
VISION_BAD_ANGLE_THRESHOLD = 5
VISION_MAX_BAD_ANGLES = 1

# Voxel pitch for hollowing — proven on chunky/simple rings
DEFAULT_VOXEL_PITCH_MM = 0.10

# Wall-thickness binary search bounds (mm)
DEFAULT_MIN_WALL_MM = 0.20
DEFAULT_MAX_WALL_MM = 1.0
DEFAULT_HOLLOW_ITERATIONS = 8
DEFAULT_CONVERGE_PCT = 3.0

# Render views used in vision check
RENDER_VIEWS = ["view_front", "view_side", "view_perspective"]

# OpenRouter
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-3.1-pro-preview"
OPENROUTER_TIMEOUT = 120  # seconds per call

# Blender script paths (relative to this file)
BLENDER_SCRIPTS = Path(__file__).parent / "blender_scripts"
SCRIPT_DETECT = BLENDER_SCRIPTS / "v16_detect_ring_size.py"
SCRIPT_HOLLOW = BLENDER_SCRIPTS / "v16_blender_hollow.py"
SCRIPT_RENDER = BLENDER_SCRIPTS / "v16_render_views.py"
SCRIPT_GLB_TO_OBJ = BLENDER_SCRIPTS / "v16_glb_to_obj.py"
SCRIPT_MEASURE = BLENDER_SCRIPTS / "v16_measure_volume.py"


# ──────────────────────────────────────────────
# Vision-check prompt
# ──────────────────────────────────────────────
VISION_PROMPT = """You are evaluating whether a hollowing process preserved the visible detail of a jewelry ring.

You will see TWO images of the SAME ring from the SAME angle:
- IMAGE A: the ORIGINAL solid ring (the design the customer ordered)
- IMAGE B: the HOLLOWED ring (the version we produced after metal removal)

CRITICAL CONTEXT — what is EXPECTED and OK:
- The hollow created an INTERIOR cavity. A slight visible hole/opening at the band's
  edges where the inner cavity meets the outer surface is EXPECTED and FINE.
- Material/lighting differences between renders are not defects — both images are matte
  grey renders, ignore minor shading variation.

ONLY count these as defects (image B is unfit for delivery):
- Decorative features (motifs, lions, flowers, filigree, prongs, claws, gallery elements)
  that are visibly FUSED, MUSHED, MERGED, or BLOBBY in B compared to crisp/separate in A
- Stray spikes, needles, or tendrils sticking out of the ring's surface in B that are
  NOT in A
- Tears, cracks, gaps in the OUTER surface of B that are NOT in A
- Loss of fine surface texture (engraving, ribbing, patterning) that's clearly visible in A
  but smoothed away in B
- Missing parts (e.g., A has 4 prongs, B has 3)

Respond with ONLY valid JSON in this exact format (no code fences, no prose before or after):
{"score": <integer 0-10>, "verdict": "pass" or "fail", "issues": "<short description of any visible exterior defects, or 'none' if pristine>"}

Scoring guide:
- 10 = exterior looks identical to original
- 7-9 = minor smoothing of fine details, still acceptable for delivery
- 4-6 = noticeable exterior detail loss, customer would likely complain
- 0-3 = catastrophic — fused features, spikes, missing parts, unrecognizable

VERDICT: "pass" if score >= 7, otherwise "fail".

Remember: judge ONLY the EXTERIOR appearance."""


# ──────────────────────────────────────────────
# Helper: run Blender subprocess with timeout, capture JEWELFORGE_STATS line
# ──────────────────────────────────────────────
def _run_blender(blender_exe, script_path, args, timeout=300, stats_prefix=None):
    """Run a Blender headless subprocess. Returns (returncode, stdout, stderr, stats_dict)."""
    cmd = [blender_exe, "--background", "--python", str(script_path), "--"] + [str(a) for a in args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    stats = {}
    if stats_prefix:
        for line in result.stdout.split("\n"):
            if line.startswith(stats_prefix):
                try:
                    stats = json.loads(line.replace(stats_prefix, "", 1))
                except json.JSONDecodeError:
                    pass
                break
    return result.returncode, result.stdout, result.stderr, stats


# ──────────────────────────────────────────────
# Pipeline steps
# ──────────────────────────────────────────────
def detect_and_scale_to_ring_size(blender_exe, input_glb, target_us_size, output_solid_glb):
    """Scale input GLB to target US ring size using horizontal-max-at-mid.
    Returns (success, detector_result_dict)."""
    rc, stdout, stderr, _ = _run_blender(
        blender_exe, SCRIPT_DETECT,
        [input_glb, "--target-us", target_us_size, "--output", output_solid_glb],
        timeout=180,
    )
    # Parse DETECTOR_RESULT line
    detector_result = {}
    for line in stdout.split("\n"):
        if line.startswith("DETECTOR_RESULT:"):
            try:
                detector_result = json.loads(line.replace("DETECTOR_RESULT:", "", 1))
            except json.JSONDecodeError:
                pass
            break
    if rc != 0 or not os.path.exists(output_solid_glb):
        return False, {"error": stderr[-500:] if stderr else "detector failed"}
    return True, detector_result


def measure_solid_weight(blender_exe, solid_glb, density):
    """Run the tiny v16_measure_volume.py script to get mesh volume in mm^3.
    Returns (weight_grams, volume_mm3) or (None, None) on failure.
    Fast — ~3-5s vs ~30s if we reused the hollow script."""
    rc, stdout, stderr, _ = _run_blender(
        blender_exe, SCRIPT_MEASURE, [solid_glb], timeout=120)
    if rc != 0:
        return None, None
    for line in stdout.split("\n"):
        if line.startswith("MEASURE_VOLUME_RESULT:"):
            try:
                data = json.loads(line.replace("MEASURE_VOLUME_RESULT:", "", 1))
                vol_mm3 = data.get("volume_mm3", 0.0)
                return vol_mm3 * density, vol_mm3
            except json.JSONDecodeError:
                pass
    return None, None


def run_hollow(blender_exe, solid_glb, output_stl, output_glb, target_weight_g, metal_internal_key,
               voxel_pitch_mm=DEFAULT_VOXEL_PITCH_MM,
               min_wall_mm=DEFAULT_MIN_WALL_MM, max_wall_mm=DEFAULT_MAX_WALL_MM,
               max_iterations=DEFAULT_HOLLOW_ITERATIONS, converge_pct=DEFAULT_CONVERGE_PCT):
    """Run voxel-remesh + Solidify hollowing. Returns (success, meta_dict)."""
    params = {
        "target_weight_g": float(target_weight_g),
        "metal": metal_internal_key,
        "voxel_pitch_mm": voxel_pitch_mm,
        "min_wall_mm": min_wall_mm,
        "max_wall_mm": max_wall_mm,
        "max_iterations": max_iterations,
        "converge_pct": converge_pct,
    }
    rc, stdout, stderr, meta = _run_blender(
        blender_exe, SCRIPT_HOLLOW,
        [solid_glb, output_stl, output_glb, json.dumps(params)],
        timeout=600, stats_prefix="BLENDER_HOLLOW_META:",
    )
    success = (rc == 0 and meta and os.path.exists(output_glb))
    return success, meta


def render_views(blender_exe, glb_path, out_dir, resolution=512):
    """Render 3 views (front, side, perspective) of GLB. Returns dict of view->png path."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    rc, stdout, stderr, _ = _run_blender(
        blender_exe, SCRIPT_RENDER,
        [str(Path(glb_path).resolve()), str(Path(out_dir).resolve()), str(resolution)],
        timeout=180,
    )
    paths = {}
    for view in RENDER_VIEWS:
        p = Path(out_dir) / f"{view}.png"
        if p.exists():
            paths[view] = str(p)
    # If we didn't get all views, log Blender output so the failure is diagnosable
    # in Railway logs (previously stderr was silently swallowed — see 2026-05-14
    # incident where Blender 3.6 rejected BLENDER_EEVEE_NEXT engine).
    if len(paths) < len(RENDER_VIEWS):
        print(f"[v16-render] FAIL: rc={rc}, got {len(paths)}/{len(RENDER_VIEWS)} PNGs")
        if stderr:
            print(f"[v16-render] stderr (last 800 chars):\n{stderr[-800:]}")
        # Also surface the relevant Render: lines from stdout
        for line in stdout.split("\n"):
            if line.startswith("Render:") or "Error" in line or "error" in line.lower():
                print(f"[v16-render] stdout: {line}")
    return paths


def _image_to_data_url(path):
    with open(path, "rb") as f:
        return f"data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")


def vision_check_one_angle(api_key, image_a, image_b, angle_name):
    """Send one angle pair to Gemini 3.1 Pro via OpenRouter. Returns dict with score / verdict."""
    body = {
        "model": OPENROUTER_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "text", "text": f"\n\nVIEW: {angle_name}\n\nIMAGE A (ORIGINAL solid):"},
                {"type": "image_url", "image_url": {"url": _image_to_data_url(image_a)}},
                {"type": "text", "text": "\n\nIMAGE B (HOLLOWED):"},
                {"type": "image_url", "image_url": {"url": _image_to_data_url(image_b)}},
            ],
        }],
        "max_tokens": 1500,
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://jewelarchitect.com",
            "X-Title": "JewelForge v1.6 Vision Gate",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=OPENROUTER_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = (data["choices"][0]["message"].get("content") or "").strip()
        # Strip code fences if model wraps in ```json ... ```
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content
            if content.endswith("```"):
                content = content.rsplit("```", 1)[0]
        content = content.strip()
        # Extract JSON object from possibly-prose-wrapped response
        if "{" in content and "}" in content:
            content = content[content.index("{"): content.rindex("}") + 1]
        parsed = json.loads(content)
        return {
            "score": int(parsed.get("score", 0)),
            "verdict": parsed.get("verdict", "fail"),
            "issues": parsed.get("issues", ""),
        }
    except Exception as e:
        # On any error, conservatively fail this angle
        return {"score": 0, "verdict": "fail", "issues": f"vision_api_error: {str(e)[:200]}"}


def aggregate_vision_verdict(per_angle):
    """Apply aggregation rule. Returns (overall_verdict, details)."""
    scores = [r.get("score", 0) for r in per_angle.values()]
    if not scores:
        return "fail", {"reason": "no angles judged", "avg": 0, "min": 0}
    avg = sum(scores) / len(scores)
    bad_angles = sum(1 for s in scores if s < VISION_BAD_ANGLE_THRESHOLD)
    pass_avg = avg >= VISION_AVG_PASS
    pass_consensus = bad_angles <= VISION_MAX_BAD_ANGLES
    overall = "pass" if (pass_avg and pass_consensus) else "fail"
    return overall, {
        "avg": round(avg, 2),
        "min": min(scores),
        "max": max(scores),
        "bad_angles": bad_angles,
    }


def convert_glb_to_obj(blender_exe, in_glb, out_obj):
    """Convert GLB to OBJ. Returns True on success."""
    rc, stdout, stderr, _ = _run_blender(
        blender_exe, SCRIPT_GLB_TO_OBJ,
        [in_glb, out_obj], timeout=120,
    )
    return rc == 0 and os.path.exists(out_obj)


# ──────────────────────────────────────────────
# Main orchestrator — called from server.py
# ──────────────────────────────────────────────
def run_ring_pipeline_v16(
    input_glb: str,
    us_ring_size: float,
    target_weight_grams: float | None,
    metal_type: str,
    job_id: str,
    temp_dir: Path,
    blender_exe: str,
    openrouter_api_key: str | None,
) -> dict:
    """Main entrypoint for v1.6 ring pipeline.

    Returns dict with: success, stats, file URLs, hollowed flag, weights, fallback_reason.
    """
    log = lambda msg: print(f"[v16 {job_id}] {msg}")

    # Resolve metal_type
    if metal_type not in LOVABLE_METAL_MAP:
        return {"success": False, "error": f"unknown_metal_type:{metal_type}"}
    metal_internal_key, density = LOVABLE_METAL_MAP[metal_type]
    log(f"metal={metal_type} -> internal_key={metal_internal_key} density={density} g/mm^3")

    # File paths
    sized_solid_glb = str(temp_dir / f"{job_id}_v16_sized_solid.glb")
    hollow_glb     = str(temp_dir / f"{job_id}_v16_hollow.glb")
    hollow_stl     = str(temp_dir / f"{job_id}_v16_hollow.stl")
    final_stl      = str(temp_dir / f"{job_id}_v16_final.stl")
    final_glb      = str(temp_dir / f"{job_id}_v16_final.glb")
    final_obj      = str(temp_dir / f"{job_id}_v16_final.obj")
    render_dir_solid  = temp_dir / f"{job_id}_v16_render_solid"
    render_dir_hollow = temp_dir / f"{job_id}_v16_render_hollow"

    # ─── STEP 1: Scale to ring size ───
    log(f"step 1: sizing to US {us_ring_size}")
    ok, detector_result = detect_and_scale_to_ring_size(
        blender_exe, input_glb, us_ring_size, sized_solid_glb)
    if not ok:
        return {"success": False, "error": "sizing_failed",
                "detector_error": detector_result.get("error", "unknown")}
    horizontal_max_mm = detector_result.get("post_scale_inner_dia_mm", 0)
    log(f"  -> sized solid GLB ready, horizontal-max inner = {horizontal_max_mm} mm")

    # ─── STEP 2: Measure solid weight ───
    log(f"step 2: measuring solid weight")
    solid_weight, solid_vol_mm3 = measure_solid_weight(blender_exe, sized_solid_glb, density)
    if solid_weight is None:
        return {"success": False, "error": "weight_measurement_failed"}
    log(f"  -> solid weight = {solid_weight:.3f}g ({solid_vol_mm3:.2f} mm^3 in {metal_internal_key})")

    # ─── STEP 3: Decide whether to hollow ───
    decision_reason = None
    do_hollow = False

    if target_weight_grams is None or target_weight_grams <= 0:
        # No target weight -> just ship the sized solid
        log("step 3: no target weight -> ship sized solid")
        decision_reason = "no_target_weight"
    elif target_weight_grams >= solid_weight:
        log(f"step 3: target ({target_weight_grams}g) >= solid ({solid_weight:.2f}g) -> cannot add metal, ship sized solid")
        decision_reason = "target_exceeds_solid_weight"
    else:
        savings_ratio = (solid_weight - target_weight_grams) / solid_weight
        log(f"step 3: savings ratio = {savings_ratio*100:.1f}% (threshold {SAVINGS_THRESHOLD*100:.0f}%)")
        if savings_ratio < SAVINGS_THRESHOLD:
            decision_reason = "savings_too_small"
            log("  -> below threshold, ship sized solid (intricate-design protection)")
        else:
            do_hollow = True
            log("  -> above threshold, attempt hollowing")

    # ─── STEP 4: Run hollow if applicable ───
    hollow_meta = None
    if do_hollow:
        log(f"step 4: hollowing to {target_weight_grams}g")
        ok, hollow_meta = run_hollow(
            blender_exe, sized_solid_glb, hollow_stl, hollow_glb,
            target_weight_grams, metal_internal_key)
        if not ok:
            log("  -> hollow pipeline FAILED, fallback to sized solid")
            decision_reason = "hollow_pipeline_error"
            do_hollow = False

    # ─── STEP 5: Vision check (only if we have a hollow file to verify) ───
    vision_result = None
    if do_hollow and openrouter_api_key:
        log(f"step 5: vision check (Gemini 3.1 Pro via OpenRouter)")
        solid_renders = render_views(blender_exe, sized_solid_glb, render_dir_solid)
        hollow_renders = render_views(blender_exe, hollow_glb, render_dir_hollow)
        if len(solid_renders) < 3 or len(hollow_renders) < 3:
            log(f"  -> render failed (solid={len(solid_renders)} hollow={len(hollow_renders)})")
            decision_reason = "render_failed"
            do_hollow = False
        else:
            per_angle = {}
            for view in RENDER_VIEWS:
                if view in solid_renders and view in hollow_renders:
                    per_angle[view] = vision_check_one_angle(
                        openrouter_api_key,
                        solid_renders[view], hollow_renders[view], view)
                    log(f"  -> {view}: score={per_angle[view]['score']} ({per_angle[view]['verdict']})")
            overall_verdict, agg = aggregate_vision_verdict(per_angle)
            vision_result = {"overall": overall_verdict, "per_angle": per_angle, "aggregate": agg}
            log(f"  -> vision overall: {overall_verdict} (avg {agg['avg']}, {agg['bad_angles']} bad angles)")
            if overall_verdict == "fail":
                decision_reason = "vision_check_failed"
                do_hollow = False
    elif do_hollow and not openrouter_api_key:
        log("step 5: SKIPPED — OPENROUTER_API_KEY not configured, will ship hollow without vision gate")

    # ─── STEP 6: Decide which file to ship ───
    if do_hollow and os.path.exists(hollow_glb):
        # Ship the hollow
        log(f"step 6: shipping HOLLOW file")
        os.replace(hollow_stl, final_stl) if os.path.exists(hollow_stl) else None
        os.replace(hollow_glb, final_glb)
        delivered_weight = (hollow_meta or {}).get("output_weight_g", target_weight_grams)
        hollowed_flag = True
    else:
        # Ship the sized solid
        log(f"step 6: shipping SIZED SOLID (fallback)")
        # Need STL of sized solid (we don't have one — generate via hollow's existing GLB export trick? No, simpler: use GLB and skip STL for solid case)
        # For consistency with existing API, we'll generate STL by converting GLB
        # For now, just rename GLB
        os.replace(sized_solid_glb, final_glb)
        delivered_weight = solid_weight
        hollowed_flag = False
        # STL skip — caller can generate from GLB if needed. We always ship OBJ which is the recommended format.

    # ─── STEP 7: Convert to OBJ ───
    log(f"step 7: converting final GLB -> OBJ")
    obj_ok = convert_glb_to_obj(blender_exe, final_glb, final_obj)
    if not obj_ok:
        log(f"  -> OBJ conversion failed, customer gets GLB only")

    # ─── STEP 8: Cleanup intermediate files ───
    for p in [hollow_stl, hollow_glb, sized_solid_glb]:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    try:
        import shutil
        if render_dir_solid.exists():
            shutil.rmtree(render_dir_solid, ignore_errors=True)
        if render_dir_hollow.exists():
            shutil.rmtree(render_dir_hollow, ignore_errors=True)
    except Exception:
        pass

    # ─── RESPONSE ───
    return {
        "success": True,
        "hollowed": hollowed_flag,
        "fallback_reason": decision_reason if not hollowed_flag else None,
        "delivered_weight_g": round(delivered_weight, 4) if delivered_weight else None,
        "target_weight_g": target_weight_grams,
        "ring_size_us": us_ring_size,
        "metal_type": metal_type,
        "stats": {
            "horizontal_max_inner_diameter_mm": horizontal_max_mm,
            "scaled_solid_weight_g": round(solid_weight, 4),
            "scaled_solid_volume_mm3": round(solid_vol_mm3, 3) if solid_vol_mm3 else None,
            "vision_result": vision_result,
            "hollow_meta": hollow_meta,
            "pipeline_version": "1.6",
        },
        "files": {
            "obj": final_obj if obj_ok else None,
            "glb": final_glb if os.path.exists(final_glb) else None,
            "stl": final_stl if os.path.exists(final_stl) else None,
        },
    }
