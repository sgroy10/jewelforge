"""JewelForge — AI Jewelry to Production-Ready 3D STL"""

import os
import uuid
import time
import asyncio
import base64
import json
import tempfile
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

app = FastAPI(title="JewelForge", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Config
PORT = int(os.environ.get("PORT", 8080))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
HITEM3D_ACCESS_KEY = os.environ.get("HITEM3D_ACCESS_KEY", "")
HITEM3D_SECRET_KEY = os.environ.get("HITEM3D_SECRET_KEY", "")
RODIN_API_KEY = os.environ.get("RODIN_API_KEY", "")
REMESHY_API_KEY = os.environ.get("REMESHY_API_KEY", "")
JEWELFORGE_API_KEY = os.environ.get("JEWELFORGE_API_KEY", "")

TEMP_DIR = Path(tempfile.gettempdir()) / "jewelforge"
TEMP_DIR.mkdir(exist_ok=True)

# ──────────────────────────────────────────────
# Hitem3D API
# ──────────────────────────────────────────────
async def hitem3d_get_token(client: httpx.AsyncClient) -> str:
    """Get Hitem3D access token using AK/SK."""
    credentials = base64.b64encode(
        f"{HITEM3D_ACCESS_KEY}:{HITEM3D_SECRET_KEY}".encode()
    ).decode()
    resp = await client.post(
        "https://api.hitem3d.ai/open-api/v1/auth/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        },
    )
    data = resp.json()
    if str(data.get("code")) != "200":
        raise Exception(f"Hitem3D auth failed: {data}")
    return data["data"]["accessToken"]


async def hitem3d_generate(image_bytes: bytes, filename: str = "jewelry.png") -> dict:
    """Submit image to Hitem3D and poll until done. Returns model URL."""
    async with httpx.AsyncClient(timeout=300) as client:
        token = await hitem3d_get_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        # Submit task — max quality for jewelry
        files = {
            "images": (filename, image_bytes, "image/png"),
        }
        form_data = {
            "request_type": "1",       # geometry only (v2.0 reliable)
            "model": "hitem3dv2.0",    # latest model
            "resolution": "1536pro",   # highest quality — sharpest geometry
            "face": "2000000",         # max 2M faces — critical for prong/pave detail
            "format": "2",             # GLB
        }
        resp = await client.post(
            "https://api.hitem3d.ai/open-api/v1/submit-task",
            headers=headers,
            files=files,
            data=form_data,
        )
        result = resp.json()
        print(f"JewelForge: Hitem3D submit response: {result}")
        if str(result.get("code")) != "200":
            raise Exception(f"Hitem3D submit failed: {result}")

        task_id = result["data"]["task_id"]
        print(f"JewelForge: Hitem3D task_id={task_id}, polling...")

        # Poll for completion — 15 min max for 1536pro
        last_state = ""
        for i in range(180):  # 15 min max
            await asyncio.sleep(5)
            resp = await client.get(
                f"https://api.hitem3d.ai/open-api/v1/query-task?task_id={task_id}",
                headers=headers,
            )
            status = resp.json()
            state = status.get("data", {}).get("state", "")
            if state != last_state:
                print(f"JewelForge: Hitem3D [{i*5}s] state={state}")
                last_state = state
            if state == "success":
                url = status["data"]["url"]
                print(f"JewelForge: Hitem3D done! URL={url[:80]}...")
                return {
                    "url": url,
                    "cover_url": status["data"].get("cover_url", ""),
                    "engine": "hitem3d",
                }
            elif state == "failed":
                print(f"JewelForge: Hitem3D FAILED: {status}")
                raise Exception(f"Hitem3D task failed: {status}")

        raise Exception(f"Hitem3D timeout after 15 minutes, last state={last_state}")


# ──────────────────────────────────────────────
# Rodin API (Fallback)
# ──────────────────────────────────────────────
async def rodin_generate(image_bytes: bytes, filename: str = "jewelry.png") -> dict:
    """Submit image to Rodin and poll until done. Returns model download info."""
    async with httpx.AsyncClient(timeout=300) as client:
        headers = {"Authorization": f"Bearer {RODIN_API_KEY}"}

        # Submit
        files = {
            "images": (filename, image_bytes, "image/png"),
            "tier": (None, "Regular"),
        }
        resp = await client.post(
            "https://api.hyper3d.com/api/v2/rodin",
            headers=headers,
            files=files,
        )
        result = resp.json()
        task_uuid = result["uuid"]
        sub_key = result["jobs"]["subscription_key"]

        # Poll
        for _ in range(120):
            await asyncio.sleep(5)
            resp = await client.post(
                "https://api.hyper3d.com/api/v2/status",
                headers=headers,
                json={"subscription_key": sub_key},
            )
            jobs = resp.json().get("jobs", [])
            if all(j["status"] in ("Done", "Failed") for j in jobs):
                if any(j["status"] == "Failed" for j in jobs):
                    raise Exception("Rodin job failed")
                break
        else:
            raise Exception("Rodin timeout")

        # Download
        resp = await client.post(
            "https://api.hyper3d.com/api/v2/download",
            headers=headers,
            json={"task_uuid": task_uuid},
        )
        dl = resp.json()
        glb_item = next(
            (item for item in dl.get("list", []) if item["name"].endswith(".glb")),
            None,
        )
        if not glb_item:
            raise Exception("No GLB in Rodin output")
        return {"url": glb_item["url"], "engine": "rodin"}


# ──────────────────────────────────────────────
# Gemini API
# ──────────────────────────────────────────────
async def gemini_generate_image(prompt: str) -> bytes:
    """Generate a jewelry image from text prompt using Gemini."""
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": [
                    {
                        "parts": [
                            {
                                "text": (
                                    f"Generate a photorealistic image of this jewelry design on a pure white background, "
                                    f"studio lighting, high detail, sharp focus, professional product photography: {prompt}"
                                )
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    "responseModalities": ["TEXT", "IMAGE"],
                },
            },
        )
        data = resp.json()
        if "error" in data:
            raise Exception(f"Gemini error: {data['error'].get('message', data['error'])}")
        # Extract image from response
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                if "inlineData" in part:
                    return base64.b64decode(part["inlineData"]["data"])
        raise Exception(f"Gemini did not return an image. Response keys: {list(data.keys())}")


async def gemini_analyze_jewelry(image_bytes: bytes) -> dict:
    """Analyze a jewelry image using Gemini Vision."""
    b64 = base64.b64encode(image_bytes).decode()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": [
                    {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": b64,
                                }
                            },
                            {
                                "text": (
                                    "Analyze this jewelry image. Return JSON with: "
                                    '{"type": "ring/pendant/earring/bracelet/other", '
                                    '"category": "solitaire/stud/motif-ring/motif-pendant/figurine/other", '
                                    '"description": "brief description", '
                                    '"metal_type": "gold/silver/platinum/rose-gold", '
                                    '"has_stones": true/false, '
                                    '"stone_shape": "round/oval/cushion/princess/emerald/pear/marquise/other/none", '
                                    '"setting_style": "prong/bezel/tension/channel/pave/other/none", '
                                    '"complexity": "simple/moderate/complex"}. '
                                    "Return ONLY valid JSON, no markdown."
                                )
                            },
                        ]
                    }
                ],
            },
        )
        data = resp.json()
        text = ""
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                if "text" in part:
                    text += part["text"]
        try:
            return json.loads(text.strip().strip("`").strip("json").strip())
        except json.JSONDecodeError:
            return {"description": text, "category": "other"}


# ──────────────────────────────────────────────
# JewelCraft Grounding Pattern — Visual Context Chain
# Each step sees the PREVIOUS step's output image.
# Photo → Pencil Sketch → Gold Render → Wax Views
# ──────────────────────────────────────────────

async def _gemini_image_transform(client: httpx.AsyncClient, input_image_b64: str, prompt: str) -> bytes:
    """Core helper: send image + prompt to Gemini, get image back."""
    resp = await client.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent?key={GEMINI_API_KEY}",
        json={
            "contents": [{"parts": [
                {"inlineData": {"mimeType": "image/png", "data": input_image_b64}},
                {"text": prompt},
            ]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        },
    )
    data = resp.json()
    if "error" in data:
        raise Exception(f"Gemini error: {data['error'].get('message', data['error'])}")
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            if "inlineData" in part:
                return base64.b64decode(part["inlineData"]["data"])
    raise Exception("Gemini did not return an image")


async def _gemini_audit_stones(client: httpx.AsyncClient, image_b64: str) -> bool:
    """Audit: does this image contain visible stones/gems? Returns True if CLEAN (no stones)."""
    resp = await client.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
        json={
            "contents": [{"parts": [
                {"inlineData": {"mimeType": "image/png", "data": image_b64}},
                {"text": (
                    "Look at this jewelry image carefully. "
                    "Are there ANY visible stones, diamonds, gems, or crystals in this image? "
                    "Answer with ONLY the word 'CLEAN' if there are NO stones/gems visible, "
                    "or 'STONES' if there ARE stones/gems visible. One word only."
                )},
            ]}],
        },
    )
    data = resp.json()
    text = ""
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            if "text" in part:
                text += part["text"]
    result = text.strip().upper()
    is_clean = "CLEAN" in result
    print(f"JewelForge: Audit result = '{result}' → {'PASS' if is_clean else 'FAIL'}")
    return is_clean


async def grounding_pipeline(image_bytes: bytes, analysis: dict) -> dict:
    """JewelCraft Grounding Pattern: Photo → Sketch → Gold → Wax with visual chain.

    Each step feeds its output image to the next step.
    Audit inspector checks each stage for stone removal.
    Returns dict with all stage images and the final clean wax for Hitem3D.
    """
    input_b64 = base64.b64encode(image_bytes).decode()
    jewelry_type = analysis.get("type", "jewelry")
    description = analysis.get("description", "")

    async with httpx.AsyncClient(timeout=180) as client:

        # ─── Stage 1: Pencil Sketch ─────────────────
        print("JewelForge: [STAGE 1] Generating pencil sketch...")
        sketch_bytes = await _gemini_image_transform(client, input_b64, (
            f"Create a detailed pencil sketch / technical drawing of this {jewelry_type} as a SEMI-MOUNT — metal framework only, absolutely NO stones. "
            f"CRITICAL: Where every stone exists in the original, draw an OPEN THROUGH-HOLE — a clean empty circle that you can see through to the white background behind it. "
            f"These holes represent drilled stone seats in the metal. Each hole must be OPEN and EMPTY — you should see white paper through each circle. "
            f"The prongs should be tiny dots or small bumps BETWEEN the holes, not covering them. "
            f"The center stone seat must be a large open circle/bore — completely empty, see-through. "
            f"Halo stones = ring of small open circles around the center hole. "
            f"Pave band stones = row of small open circles along the band. "
            f"White background, precise technical jewelry CAD drawing style. "
            f"Think of a Rhino/Matrix CAD semi-mount rendering — all stone positions are clean drilled through-holes."
        ))
        sketch_b64 = base64.b64encode(sketch_bytes).decode()
        print(f"JewelForge: [STAGE 1] Pencil sketch done ({len(sketch_bytes)} bytes)")

        # ─── Stage 2: Gold Render (from sketch) ─────
        print("JewelForge: [STAGE 2] Generating gold render from sketch...")
        gold_bytes = await _gemini_image_transform(client, sketch_b64, (
            f"Transform this pencil sketch into a photorealistic 18K polished gold semi-mount render. "
            f"This is a {jewelry_type}. CRITICAL: Keep ALL the open through-holes from the sketch EXACTLY as they are. "
            f"Every circular hole in the sketch must remain as an OPEN DRILLED HOLE in the gold — you must see through each hole to the background. "
            f"Do NOT fill in any holes. Do NOT add stones. Do NOT close any openings. "
            f"The center hole stays open. The halo holes stay open. The band holes stay open. "
            f"Render the METAL ONLY in polished 18K gold with these clean drilled bores. "
            f"Small prong beads visible between holes. Studio lighting, white background. "
            f"This is a semi-mount — ready for a stone setter to place stones into the open holes."
        ))
        gold_b64 = base64.b64encode(gold_bytes).decode()
        print(f"JewelForge: [STAGE 2] Gold render done ({len(gold_bytes)} bytes)")

        # ─── Stage 3: Wax Views (from gold render) ──
        print("JewelForge: [STAGE 3] Generating wax views from gold render...")
        wax_views = []
        view_angles = [
            "front view straight on",
            "left side view at 90 degrees",
            "three-quarter angle view from slightly above and to the right",
        ]

        for i, angle in enumerate(view_angles):
            # Each wax view is generated from the GOLD render (not original photo)
            wax_bytes = await _gemini_image_transform(client, gold_b64, (
                f"Transform this gold semi-mount render into a blue wax carving model, {angle}. "
                f"CRITICAL: This must be an EXACT CLONE of the gold render — same shape, same proportions, same structure. "
                f"The ONLY change is material: gold → uniform matte blue wax (#3A7BC8). "
                f"ALL open through-holes from the gold render MUST remain as open through-holes in the wax. "
                f"Do NOT fill any holes. Do NOT close any openings. Do NOT add stones. "
                f"Every drilled bore must stay open — you should see the dark background through each hole. "
                f"Smooth blue wax surface. Dark background. Soft ambient occlusion lighting. "
                f"This is an exact material swap — nothing else changes."
            ))
            wax_views.append(wax_bytes)
            print(f"JewelForge: [STAGE 3] Wax view {i+1}/3 done ({len(wax_bytes)} bytes)")

        # ─── Stage 4: Audit — check wax views for stones ──
        print("JewelForge: [AUDIT] Checking wax views for stone contamination...")
        best_wax_idx = -1
        for i, wv in enumerate(wax_views):
            wv_b64 = base64.b64encode(wv).decode()
            is_clean = await _gemini_audit_stones(client, wv_b64)
            print(f"JewelForge: [AUDIT] Wax view {i+1}: {'CLEAN ✓' if is_clean else 'HAS STONES ✗'}")
            if is_clean and best_wax_idx == -1:
                best_wax_idx = i

        # If no wax passed audit, use gold render directly (it's usually cleaner)
        if best_wax_idx == -1:
            print("JewelForge: [AUDIT] No clean wax found — checking gold render...")
            is_gold_clean = await _gemini_audit_stones(client, gold_b64)
            if is_gold_clean:
                print("JewelForge: [AUDIT] Gold render is clean — using it for 3D")
                best_image = gold_bytes
            else:
                print("JewelForge: [AUDIT] Gold render also has stones — using sketch for 3D")
                best_image = sketch_bytes
        else:
            print(f"JewelForge: [AUDIT] Using wax view {best_wax_idx + 1} for 3D generation")
            best_image = wax_views[best_wax_idx]

    return {
        "sketch": sketch_bytes,
        "gold_render": gold_bytes,
        "wax_views": wax_views,
        "best_for_3d": best_image,
        "best_wax_idx": best_wax_idx,
        "audit_passed": best_wax_idx >= 0,
    }


async def gemini_generate_wax_views(image_bytes: bytes, analysis: dict) -> list[bytes]:
    """Legacy wrapper — runs grounding pipeline, returns wax views."""
    result = await grounding_pipeline(image_bytes, analysis)
    return result["wax_views"]


# ──────────────────────────────────────────────
# Blender Headless Processing
# ──────────────────────────────────────────────
BLENDER_AVAILABLE = False

def check_blender():
    """Check if Blender is available at startup."""
    global BLENDER_AVAILABLE
    try:
        r = subprocess.run(["blender", "--version"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            BLENDER_AVAILABLE = True
            print(f"JewelForge: Blender found — {r.stdout.strip().split(chr(10))[0]}")
        else:
            print(f"JewelForge: Blender not working — {r.stderr[:200]}")
    except Exception as e:
        print(f"JewelForge: Blender not available — {e}")

check_blender()


def run_blender_refine(input_glb: str, output_stl: str, output_glb: str) -> dict:
    """Run Blender headless mesh refinement (defaults to ring, US size 7)."""
    if not BLENDER_AVAILABLE:
        raise Exception("Blender not available")

    script_path = Path(__file__).parent / "blender_scripts" / "refine.py"
    result = subprocess.run(
        [
            "blender", "--background", "--python", str(script_path),
            "--", input_glb, output_stl, output_glb, "ring", "17.35",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    stats = {}
    for line in result.stdout.split("\n"):
        if line.startswith("JEWELFORGE_STATS:"):
            try:
                stats = json.loads(line.replace("JEWELFORGE_STATS:", ""))
            except json.JSONDecodeError:
                pass
    if result.returncode != 0 and not os.path.exists(output_stl):
        raise Exception(f"Blender failed: {result.stderr[-500:]}")
    return stats


def run_blender_pave_cleanup(
    input_glb: str,
    output_stl: str,
    output_glb: str,
    min_stone_radius: float = 0.3,
    max_stone_radius: float = 1.5,
    seat_depth: float = 0.6,
    detection_threshold: float = 0.15,
) -> dict:
    """Run Blender pave stone cleanup — detect bumps, cut clean seats."""
    if not BLENDER_AVAILABLE:
        raise Exception("Blender not available")

    params = {
        "min_stone_radius": min_stone_radius,
        "max_stone_radius": max_stone_radius,
        "seat_depth": seat_depth,
        "detection_threshold": detection_threshold,
    }
    params_json = json.dumps(params)

    script_path = Path(__file__).parent / "blender_scripts" / "pave_cleanup.py"
    result = subprocess.run(
        [
            "blender", "--background", "--python", str(script_path),
            "--", input_glb, output_stl, output_glb, params_json,
        ],
        capture_output=True,
        text=True,
        timeout=300,  # 5 min — boolean ops on dense mesh take time
    )

    for line in result.stdout.split("\n"):
        if line.startswith("JewelForge:"):
            print(line)

    stats = {}
    for line in result.stdout.split("\n"):
        if line.startswith("JEWELFORGE_STATS:"):
            try:
                stats = json.loads(line.replace("JEWELFORGE_STATS:", ""))
            except json.JSONDecodeError:
                pass

    if result.returncode != 0 and not os.path.exists(output_stl):
        stderr_tail = result.stderr[-500:] if result.stderr else "no stderr"
        raise Exception(f"Blender pave_cleanup failed: {stderr_tail}")
    return stats


def run_blender_scale_and_repair(
    input_glb: str,
    output_stl: str,
    output_glb: str,
    jewelry_type: str = "ring",
    us_ring_size: float = None,
    height_mm: float = None,
) -> dict:
    """Run Blender headless with proper mm scaling + repair."""
    if not BLENDER_AVAILABLE:
        raise Exception("Blender not available")

    params = {
        "jewelry_type": jewelry_type,
        "us_ring_size": us_ring_size,
        "height_mm": height_mm,
    }
    params_json = json.dumps(params)

    script_path = Path(__file__).parent / "blender_scripts" / "scale_and_repair.py"
    result = subprocess.run(
        [
            "blender", "--background", "--python", str(script_path),
            "--", input_glb, output_stl, output_glb, params_json,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )

    # Log Blender output for debugging
    for line in result.stdout.split("\n"):
        if line.startswith("JewelForge:"):
            print(line)

    stats = {}
    for line in result.stdout.split("\n"):
        if line.startswith("JEWELFORGE_STATS:"):
            try:
                stats = json.loads(line.replace("JEWELFORGE_STATS:", ""))
            except json.JSONDecodeError:
                pass

    if result.returncode != 0 and not os.path.exists(output_stl):
        raise Exception(f"Blender scale_and_repair failed: {result.stderr[-500:]}")
    return stats


# ──────────────────────────────────────────────
# Smart Refine (shelling-based, separate from scale_and_repair)
# ──────────────────────────────────────────────
def run_blender_smart_refine(
    input_glb: str, output_stl: str, output_glb: str,
    jewelry_type: str = "ring", us_ring_size: float = None,
    target_weight_grams: float = None, metal_type: str = None,
    wall_thickness_mm: float = None,
) -> dict:
    if not BLENDER_AVAILABLE:
        raise Exception("Blender not available")
    params = {
        "jewelry_type": jewelry_type,
        "us_ring_size": us_ring_size,
        "target_weight_grams": target_weight_grams,
        "metal_type": metal_type,
        "wall_thickness_mm": wall_thickness_mm,
    }
    script_path = Path(__file__).parent / "blender_scripts" / "smart_refine.py"
    result = subprocess.run(
        ["blender", "--background", "--python", str(script_path),
         "--", input_glb, output_stl, output_glb, json.dumps(params)],
        capture_output=True, text=True, timeout=300,
    )
    for line in result.stdout.split("\n"):
        if line.startswith("SmartRefine:"):
            print(line)
    stats = {}
    for line in result.stdout.split("\n"):
        if line.startswith("SMARTREFINE_STATS:"):
            try:
                stats = json.loads(line.replace("SMARTREFINE_STATS:", ""))
            except json.JSONDecodeError:
                pass
    if result.returncode != 0 and not os.path.exists(output_stl):
        raise Exception(f"smart_refine failed: {result.stderr[-500:]}")
    return stats


@app.post("/api/smart-refine")
async def smart_refine(
    glb_url: str = Form(None),
    glb_base64: str = Form(None),
    jewelry_type: str = Form("ring"),
    us_ring_size: float = Form(None),
    target_weight_grams: float = Form(None),
    metal_type: str = Form("gold_14k"),
    wall_thickness_mm: float = Form(None),
):
    """Shell-based jewelry refine. Hollows the mesh to hit target weight
    without distorting proportions. Separate from /api/scale-and-repair."""
    job_id = str(uuid.uuid4())[:8]
    input_glb = str(TEMP_DIR / f"{job_id}_input.glb")
    output_stl = str(TEMP_DIR / f"{job_id}_output.stl")
    output_glb = str(TEMP_DIR / f"{job_id}_output.glb")

    try:
        if glb_url:
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.get(glb_url)
                    resp.raise_for_status()
                    with open(input_glb, "wb") as f:
                        f.write(resp.content)
            except httpx.HTTPStatusError as e:
                raise HTTPException(status_code=422, detail={
                    "error": "glb_url_unreachable",
                    "message": f"Failed to download GLB: upstream {e.response.status_code}",
                    "url": glb_url,
                })
        elif glb_base64:
            with open(input_glb, "wb") as f:
                f.write(base64.b64decode(glb_base64))
        else:
            raise HTTPException(status_code=400, detail="Provide glb_url or glb_base64")

        stats = run_blender_smart_refine(
            input_glb, output_stl, output_glb,
            jewelry_type=jewelry_type, us_ring_size=us_ring_size,
            target_weight_grams=target_weight_grams, metal_type=metal_type,
            wall_thickness_mm=wall_thickness_mm,
        )

        # Post-Blender manifold repair via PyMeshFix
        repaired_stl = str(TEMP_DIR / f"{job_id}_repaired.stl")
        repaired_glb = str(TEMP_DIR / f"{job_id}_repaired.glb")
        try:
            import pymeshfix
            import trimesh as _trimesh
            import numpy as _np

            if os.path.exists(output_stl) and os.path.getsize(output_stl) > 84:
                mesh = _trimesh.load(output_stl)
                verts = _np.array(mesh.vertices, dtype=_np.float64)
                faces = _np.array(mesh.faces, dtype=_np.int32)
                print(f"SmartRefine: PyMeshFix input — {len(verts)} verts, {len(faces)} faces")

                mfix = pymeshfix.MeshFix(verts, faces)
                mfix.repair(verbose=True)

                repaired_mesh = _trimesh.Trimesh(
                    vertices=mfix.v, faces=mfix.f, process=False)

                repaired_mesh.export(repaired_stl)
                repaired_mesh.export(repaired_glb)

                stats["pymeshfix_applied"] = True
                stats["pymeshfix_verts"] = len(mfix.v)
                stats["pymeshfix_faces"] = len(mfix.f)
                stats["pymeshfix_watertight"] = bool(repaired_mesh.is_watertight)
                stats["pymeshfix_volume_mm3"] = round(float(abs(repaired_mesh.volume)) * 1e9, 4) if repaired_mesh.is_watertight else None
                print(f"SmartRefine: PyMeshFix output — {len(mfix.v)} verts, "
                      f"{len(mfix.f)} faces, watertight={repaired_mesh.is_watertight}")

                # Use repaired files instead of raw Blender output
                output_stl = repaired_stl
                output_glb = repaired_glb
        except ImportError:
            print("SmartRefine: PyMeshFix not installed, skipping manifold repair")
            stats["pymeshfix_applied"] = False
        except Exception as e:
            print(f"SmartRefine: PyMeshFix failed: {e}")
            stats["pymeshfix_applied"] = False
            stats["pymeshfix_error"] = str(e)

        result = {"success": True, "stats": stats}
        if os.path.exists(output_stl) and os.path.getsize(output_stl) > 84:
            persist = str(TEMP_DIR / f"{job_id}_smart.stl")
            if os.path.exists(persist):
                os.remove(persist)
            os.rename(output_stl, persist)
            result["stl_url"] = f"/api/files/{job_id}_smart.stl"
        if os.path.exists(output_glb) and os.path.getsize(output_glb) > 200:
            persist = str(TEMP_DIR / f"{job_id}_smart.glb")
            if os.path.exists(persist):
                os.remove(persist)
            os.rename(output_glb, persist)
            result["glb_url"] = f"/api/files/{job_id}_smart.glb"
        return result

    finally:
        try:
            os.remove(input_glb)
        except OSError:
            pass


# ──────────────────────────────────────────────
# API Endpoints
# ──────────────────────────────────────────────

@app.get("/api/health")
async def health():
    blender_version = "not available"
    if BLENDER_AVAILABLE:
        try:
            r = subprocess.run(["blender", "--version"], capture_output=True, text=True, timeout=10)
            blender_version = r.stdout.strip().split("\n")[0]
        except Exception:
            pass
    return {
        "status": "ok",
        "version": "1.0.0",
        "blender": blender_version,
        "blender_available": BLENDER_AVAILABLE,
        "engines": ["hitem3d", "rodin"],
    }


@app.post("/api/analyze")
async def analyze_image(image: UploadFile = File(...)):
    """Analyze uploaded jewelry image."""
    image_bytes = await image.read()
    analysis = await gemini_analyze_jewelry(image_bytes)
    # Return image as base64 too for frontend display
    b64 = base64.b64encode(image_bytes).decode()
    return {"analysis": analysis, "image_base64": b64}


@app.post("/api/generate-image")
async def generate_image(prompt: str = Form(...)):
    """Generate jewelry image from text prompt."""
    image_bytes = await gemini_generate_image(prompt)
    b64 = base64.b64encode(image_bytes).decode()
    analysis = await gemini_analyze_jewelry(image_bytes)
    return {"image_base64": b64, "analysis": analysis}


@app.post("/api/generate-wax")
async def generate_wax(image_base64: str = Form(...)):
    """Legacy: Generate blue wax views from jewelry image."""
    image_bytes = base64.b64decode(image_base64)
    analysis = await gemini_analyze_jewelry(image_bytes)
    wax_views = await gemini_generate_wax_views(image_bytes, analysis)
    wax_b64 = [base64.b64encode(w).decode() for w in wax_views]
    return {"wax_views": wax_b64, "analysis": analysis}


@app.post("/api/grounding-pipeline")
async def grounding_pipeline_endpoint(image_base64: str = Form(...)):
    """JewelCraft Grounding Pattern: Photo → Sketch → Gold → Wax.

    Visual context chain — each step sees the previous step's output.
    Includes audit inspector to verify stone removal.
    Returns all stages + the best clean image for Hitem3D.
    """
    image_bytes = base64.b64decode(image_base64)
    analysis = await gemini_analyze_jewelry(image_bytes)
    result = await grounding_pipeline(image_bytes, analysis)

    response = {
        "analysis": analysis,
        "sketch_base64": base64.b64encode(result["sketch"]).decode(),
        "gold_render_base64": base64.b64encode(result["gold_render"]).decode(),
        "wax_views_base64": [base64.b64encode(w).decode() for w in result["wax_views"]],
        "best_for_3d_base64": base64.b64encode(result["best_for_3d"]).decode(),
        "best_wax_idx": result["best_wax_idx"],
        "audit_passed": result["audit_passed"],
    }
    return response


@app.post("/api/generate-3d")
async def generate_3d(
    image_base64: str = Form(...),
    engine: str = Form("hitem3d"),
):
    """Generate 3D mesh from image. Returns GLB download URL.
    WARNING: This blocks for 5-15 min with 1536pro. Use /api/generate-3d/submit + /api/generate-3d/poll instead.
    """
    image_bytes = base64.b64decode(image_base64)

    if engine == "hitem3d" and HITEM3D_ACCESS_KEY:
        try:
            result = await hitem3d_generate(image_bytes)
            return result
        except Exception as e:
            print(f"Hitem3D failed, falling back to Rodin: {e}")

    if RODIN_API_KEY:
        result = await rodin_generate(image_bytes)
        return result

    raise HTTPException(status_code=500, detail="No 3D engine available")


@app.post("/api/generate-3d/submit")
async def generate_3d_submit(
    image_base64: str = Form(...),
    image_base64_side: str = Form(None),
    image_base64_top: str = Form(None),
    engine: str = Form("hitem3d"),
):
    """Submit 3D generation task. Supports single-view and multi-view.

    Multi-view mapping to Hitem3D 4-view bitmask (front/back/left/right):
      image_base64      → front  (required)
      image_base64_top  → back   (optional, top view as back substitute)
      image_base64_side → left   (optional, side view as left)

    Bitmask: 3 views = "1110", 2 views = varies, 1 view = single-image mode.
    """
    front_bytes = base64.b64decode(image_base64)
    side_bytes = base64.b64decode(image_base64_side) if image_base64_side else None
    top_bytes = base64.b64decode(image_base64_top) if image_base64_top else None

    if engine == "hitem3d" and HITEM3D_ACCESS_KEY:
        async with httpx.AsyncClient(timeout=300) as client:
            token = await hitem3d_get_token(client)
            headers = {"Authorization": f"Bearer {token}"}

            form_data = {
                "request_type": "1",
                "model": "hitem3dv2.0",
                "resolution": "1536pro",
                "face": "2000000",
                "format": "2",
            }

            # Multi-view if side or top provided
            if side_bytes or top_bytes:
                # Build multi_images in order: front, back(top), left(side)
                files = [("multi_images", ("front.png", front_bytes, "image/png"))]
                bit = "1"  # front

                if top_bytes:
                    files.append(("multi_images", ("back.png", top_bytes, "image/png")))
                    bit += "1"  # back
                else:
                    bit += "0"

                if side_bytes:
                    files.append(("multi_images", ("left.png", side_bytes, "image/png")))
                    bit += "1"  # left
                else:
                    bit += "0"

                bit += "0"  # right — not used
                form_data["multi_images_bit"] = bit

                views_sent = len(files)
                print(f"JewelForge: Hitem3D multi-view submit — {views_sent} views, bit={bit}")
            else:
                # Single-view fallback (backward compatible)
                files = {"images": ("jewelry.png", front_bytes, "image/png")}
                views_sent = 1
                print("JewelForge: Hitem3D single-view submit")

            resp = await client.post(
                "https://api.hitem3d.ai/open-api/v1/submit-task",
                headers=headers, files=files, data=form_data,
            )
            result = resp.json()
            print(f"JewelForge: Hitem3D submit response: {result}")
            if str(result.get("code")) != "200":
                raise HTTPException(status_code=500, detail=f"Hitem3D submit failed: {result}")
            return {
                "task_id": result["data"]["task_id"],
                "engine": "hitem3d",
                "views_sent": views_sent,
                "status": "submitted",
            }

    raise HTTPException(status_code=500, detail="No 3D engine available")


@app.get("/api/generate-3d/poll/{task_id}")
async def generate_3d_poll(task_id: str):
    """Poll Hitem3D task status. Returns state + URL when done."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await hitem3d_get_token(client)
        resp = await client.get(
            f"https://api.hitem3d.ai/open-api/v1/query-task?task_id={task_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        status = resp.json()
        data = status.get("data", {})
        state = data.get("state", "unknown")

        result = {"task_id": task_id, "state": state}
        if state == "success":
            result["url"] = data["url"]
            result["cover_url"] = data.get("cover_url", "")
            result["engine"] = "hitem3d"
        elif state == "failed":
            result["error"] = str(status)

        return result


@app.post("/api/refine")
async def refine_mesh(
    glb_url: str = Form(None),
    glb_base64: str = Form(None),
):
    """Refine a GLB mesh using Blender. Returns STL + GLB."""
    job_id = str(uuid.uuid4())[:8]
    input_glb = str(TEMP_DIR / f"{job_id}_input.glb")
    output_stl = str(TEMP_DIR / f"{job_id}_output.stl")
    output_glb = str(TEMP_DIR / f"{job_id}_output.glb")

    try:
        # Download or decode the GLB
        if glb_url:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(glb_url)
                resp.raise_for_status()
                with open(input_glb, "wb") as f:
                    f.write(resp.content)
        elif glb_base64:
            with open(input_glb, "wb") as f:
                f.write(base64.b64decode(glb_base64))
        else:
            raise HTTPException(status_code=400, detail="Provide glb_url or glb_base64")

        # Run Blender
        stats = run_blender_refine(input_glb, output_stl, output_glb)

        # Save outputs as files and return URLs (never base64 — browser chokes on large JSON)
        result = {"success": True, "refined": True, "stats": stats}

        if os.path.exists(output_stl):
            stl_size = os.path.getsize(output_stl)
            if stl_size > 84:
                # Move to a persistent name so /api/files/ can serve it
                persist_stl = str(TEMP_DIR / f"{job_id}_final.stl")
                os.rename(output_stl, persist_stl)
                result["stl_url"] = f"/api/files/{job_id}_final.stl"
                print(f"JewelForge: STL size={stl_size} bytes → {result['stl_url']}")
            else:
                print(f"JewelForge: WARNING — STL is empty ({stl_size} bytes)")

        if os.path.exists(output_glb):
            glb_size = os.path.getsize(output_glb)
            if glb_size > 200:
                persist_glb = str(TEMP_DIR / f"{job_id}_final.glb")
                os.rename(output_glb, persist_glb)
                result["glb_url"] = f"/api/files/{job_id}_final.glb"
                print(f"JewelForge: GLB size={glb_size} bytes → {result['glb_url']}")
            else:
                print(f"JewelForge: WARNING — GLB is empty ({glb_size} bytes)")

        return result

    finally:
        # Only clean up the input — outputs are served via /api/files/
        try:
            os.remove(input_glb)
        except OSError:
            pass


@app.post("/api/scale-and-repair")
async def scale_and_repair(
    glb_url: str = Form(None),
    glb_base64: str = Form(None),
    jewelry_type: str = Form("ring"),
    us_ring_size: float = Form(None),
    height_mm: float = Form(None),
):
    """Scale a GLB mesh to real-world mm dimensions + repair + export STL.

    For rings: pass jewelry_type=ring and us_ring_size (3-13).
    For other types: pass jewelry_type and optionally height_mm.
    Accepts glb_url (from Hitem3D/Rodin) or glb_base64.
    """
    job_id = str(uuid.uuid4())[:8]
    input_glb = str(TEMP_DIR / f"{job_id}_input.glb")
    output_stl = str(TEMP_DIR / f"{job_id}_output.stl")
    output_glb = str(TEMP_DIR / f"{job_id}_output.glb")

    try:
        # Download or decode the GLB
        if glb_url:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(glb_url)
                resp.raise_for_status()
                with open(input_glb, "wb") as f:
                    f.write(resp.content)
        elif glb_base64:
            with open(input_glb, "wb") as f:
                f.write(base64.b64decode(glb_base64))
        else:
            raise HTTPException(status_code=400, detail="Provide glb_url or glb_base64")

        # Run Blender with scaling
        stats = run_blender_scale_and_repair(
            input_glb, output_stl, output_glb,
            jewelry_type=jewelry_type,
            us_ring_size=us_ring_size,
            height_mm=height_mm,
        )

        # Save outputs as files and return URLs (same pattern as /api/refine)
        result = {"success": True, "refined": True, "stats": stats}

        if os.path.exists(output_stl):
            stl_size = os.path.getsize(output_stl)
            if stl_size > 84:
                persist_stl = str(TEMP_DIR / f"{job_id}_final.stl")
                os.rename(output_stl, persist_stl)
                result["stl_url"] = f"/api/files/{job_id}_final.stl"
                print(f"JewelForge: STL size={stl_size} bytes → {result['stl_url']}")

        if os.path.exists(output_glb):
            glb_size = os.path.getsize(output_glb)
            if glb_size > 200:
                persist_glb = str(TEMP_DIR / f"{job_id}_final.glb")
                os.rename(output_glb, persist_glb)
                result["glb_url"] = f"/api/files/{job_id}_final.glb"
                print(f"JewelForge: GLB size={glb_size} bytes → {result['glb_url']}")

        return result

    finally:
        # Only clean up the input — outputs are served via /api/files/
        try:
            os.remove(input_glb)
        except OSError:
            pass


@app.post("/api/full-pipeline")
async def full_pipeline(
    image: UploadFile = File(None),
    image_url: str = Form(None),
    prompt: str = Form(None),
    engine: str = Form("hitem3d"),
    skip_wax: bool = Form(False),
    jewelry_type: str = Form(None),
    us_ring_size: float = Form(None),
    height_mm: float = Form(None),
):
    """Full pipeline: image/prompt/URL → analysis → wax → 3D → scaled STL.

    Image input (pick one):
    - image: file upload (multipart)
    - image_url: URL to download image from (e.g. Supabase storage URL)
    - prompt: text description to generate image via Gemini

    Scaling params (optional):
    - jewelry_type: ring/pendant/earring/bracelet/other
    - us_ring_size: 3-13 (for rings only)
    - height_mm: target height in mm (for non-ring types)
    """
    # Step 1: Get the jewelry image
    if image:
        image_bytes = await image.read()
    elif image_url:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(image_url)
            resp.raise_for_status()
            image_bytes = resp.content
        print(f"JewelForge: Downloaded image from URL ({len(image_bytes)} bytes)")
    elif prompt:
        image_bytes = await gemini_generate_image(prompt)
    else:
        raise HTTPException(status_code=400, detail="Provide image, image_url, or prompt")

    image_b64 = base64.b64encode(image_bytes).decode()

    # Step 2: Analyze
    analysis = await gemini_analyze_jewelry(image_bytes)

    # Auto-detect jewelry_type from analysis if not provided
    effective_type = jewelry_type or analysis.get("type", "ring")

    # Step 3: Generate wax views (optional but improves quality)
    wax_b64 = []
    if not skip_wax:
        try:
            wax_views = await gemini_generate_wax_views(image_bytes, analysis)
            wax_b64 = [base64.b64encode(w).decode() for w in wax_views]
            # Wax views are for display only — always use original image for 3D generation
        except Exception as e:
            print(f"Wax generation failed, using original image: {e}")

    # Step 4: Generate 3D
    mesh_result = None
    if engine == "hitem3d" and HITEM3D_ACCESS_KEY:
        try:
            mesh_result = await hitem3d_generate(image_bytes)
        except Exception as e:
            print(f"Hitem3D failed: {e}")

    if not mesh_result and RODIN_API_KEY:
        try:
            mesh_result = await rodin_generate(image_bytes)
        except Exception as e:
            print(f"Rodin failed: {e}")

    if not mesh_result:
        raise HTTPException(status_code=500, detail="All 3D engines failed")

    # Step 5: Download and refine with Blender
    job_id = str(uuid.uuid4())[:8]
    input_glb = str(TEMP_DIR / f"{job_id}_input.glb")
    output_stl = str(TEMP_DIR / f"{job_id}_output.stl")
    output_glb = str(TEMP_DIR / f"{job_id}_output.glb")

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(mesh_result["url"])
            with open(input_glb, "wb") as f:
                f.write(resp.content)

        raw_glb_bytes = resp.content

        # Blender refinement — use scale_and_repair if type is known
        stats = {}
        refined = False
        if BLENDER_AVAILABLE:
            try:
                use_scaling = jewelry_type or us_ring_size or height_mm
                if use_scaling:
                    stats = run_blender_scale_and_repair(
                        input_glb, output_stl, output_glb,
                        jewelry_type=effective_type,
                        us_ring_size=us_ring_size,
                        height_mm=height_mm,
                    )
                else:
                    stats = run_blender_scale_and_repair(
                        input_glb, output_stl, output_glb,
                        jewelry_type=effective_type,
                    )
                refined = True
            except Exception as e:
                print(f"Blender refinement failed, serving raw: {e}")

        result = {
            "success": True,
            "image_base64": image_b64,
            "analysis": analysis,
            "wax_views": wax_b64,
            "engine": mesh_result.get("engine", "unknown"),
            "refined": refined,
            "stats": stats,
            "jewelry_type": effective_type,
        }

        if refined and os.path.exists(output_stl):
            with open(output_stl, "rb") as f:
                result["stl_base64"] = base64.b64encode(f.read()).decode()
        if refined and os.path.exists(output_glb):
            with open(output_glb, "rb") as f:
                result["glb_base64"] = base64.b64encode(f.read()).decode()
        else:
            result["glb_base64"] = base64.b64encode(raw_glb_bytes).decode()

        return result

    finally:
        for f in [input_glb, output_stl, output_glb]:
            try:
                os.remove(f)
            except OSError:
                pass


@app.post("/api/pave-cleanup")
async def pave_cleanup(
    glb_url: str = Form(None),
    glb_base64: str = Form(None),
    min_stone_radius: float = Form(0.3),
    max_stone_radius: float = Form(1.5),
    seat_depth: float = Form(0.6),
    detection_threshold: float = Form(0.15),
):
    """Detect pave stone bumps and cut clean hemispherical stone seats.

    Input: GLB mesh (already scaled to mm) from /api/refine or /api/scale-and-repair.
    Process: Detect bumps → cluster into stone positions → boolean-cut seats → sharpen edges.
    Output: Cleaned STL + GLB with production-ready stone seats.

    Params:
    - min_stone_radius: 0.3mm (melee) to filter noise
    - max_stone_radius: 1.5mm (small stones) to filter non-pave features
    - seat_depth: 0.6 = 60% of stone radius depth
    - detection_threshold: 0.15mm protrusion to count as a bump peak
    """
    job_id = str(uuid.uuid4())[:8]
    input_glb = str(TEMP_DIR / f"{job_id}_pave_input.glb")
    output_stl = str(TEMP_DIR / f"{job_id}_pave_output.stl")
    output_glb = str(TEMP_DIR / f"{job_id}_pave_output.glb")

    try:
        if glb_url:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(glb_url)
                resp.raise_for_status()
                with open(input_glb, "wb") as f:
                    f.write(resp.content)
        elif glb_base64:
            with open(input_glb, "wb") as f:
                f.write(base64.b64decode(glb_base64))
        else:
            raise HTTPException(status_code=400, detail="Provide glb_url or glb_base64")

        stats = run_blender_pave_cleanup(
            input_glb, output_stl, output_glb,
            min_stone_radius=min_stone_radius,
            max_stone_radius=max_stone_radius,
            seat_depth=seat_depth,
            detection_threshold=detection_threshold,
        )

        result = {"success": True, "stats": stats}

        if os.path.exists(output_stl):
            stl_data = open(output_stl, "rb").read()
            if len(stl_data) > 84:
                result["stl_base64"] = base64.b64encode(stl_data).decode()
                print(f"JewelForge: Pave STL size={len(stl_data)} bytes")
        if os.path.exists(output_glb):
            glb_data = open(output_glb, "rb").read()
            if len(glb_data) > 200:
                result["glb_base64"] = base64.b64encode(glb_data).decode()
                print(f"JewelForge: Pave GLB size={len(glb_data)} bytes")

        return result

    finally:
        for f in [input_glb, output_stl, output_glb]:
            try:
                os.remove(f)
            except OSError:
                pass


# Serve refined files from temp dir
@app.get("/api/files/{filename}")
async def serve_file(filename: str):
    """Serve temp files (STL/GLB) by job ID."""
    filepath = TEMP_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    media = "model/gltf-binary" if filename.endswith(".glb") else "application/octet-stream"
    return FileResponse(str(filepath), media_type=media, filename=filename)


# Serve static frontend
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
