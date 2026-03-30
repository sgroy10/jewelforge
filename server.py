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


async def gemini_generate_wax_views(image_bytes: bytes, analysis: dict) -> list[bytes]:
    """Generate blue wax carving views from jewelry image."""
    b64 = base64.b64encode(image_bytes).decode()
    views = []
    view_angles = ["front view straight on", "left side view at 90 degrees", "top-down view from above"]

    async with httpx.AsyncClient(timeout=120) as client:
        for angle in view_angles:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent?key={GEMINI_API_KEY}",
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
                                        f"Transform this jewelry image into a blue wax carving model, {angle}. "
                                        f"The wax should be uniform blue color (#4A90D9 to #2E5A8B). "
                                        f"Show it as a solid wax carving with ambient occlusion lighting. "
                                        f"No stones, no gems — just the metal structure as a wax model. "
                                        f"Clean dark background. Sharp edges, clear detail. "
                                        f"This is a {analysis.get('type', 'jewelry')} - {analysis.get('description', '')}. "
                                        f"Professional quality, high detail."
                                    )
                                },
                            ]
                        }
                    ],
                    "generationConfig": {
                        "responseModalities": ["TEXT", "IMAGE"],
                    },
                },
            )
            data = resp.json()
            for candidate in data.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    if "inlineData" in part:
                        views.append(base64.b64decode(part["inlineData"]["data"]))
                        break

    return views


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
    """Run Blender headless mesh refinement (legacy — no scaling)."""
    if not BLENDER_AVAILABLE:
        raise Exception("Blender not available")

    script_path = Path(__file__).parent / "blender_scripts" / "refine.py"
    result = subprocess.run(
        [
            "blender", "--background", "--python", str(script_path),
            "--", input_glb, output_stl, output_glb,
        ],
        capture_output=True,
        text=True,
        timeout=120,
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
    """Generate blue wax views from jewelry image."""
    image_bytes = base64.b64decode(image_base64)
    analysis = await gemini_analyze_jewelry(image_bytes)
    wax_views = await gemini_generate_wax_views(image_bytes, analysis)
    wax_b64 = [base64.b64encode(w).decode() for w in wax_views]
    return {"wax_views": wax_b64, "analysis": analysis}


@app.post("/api/generate-3d")
async def generate_3d(
    image_base64: str = Form(...),
    engine: str = Form("hitem3d"),
):
    """Generate 3D mesh from image. Returns GLB download URL."""
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

        # Read outputs
        result = {"success": True, "refined": True, "stats": stats}
        if os.path.exists(output_stl):
            stl_data = open(output_stl, "rb").read()
            if len(stl_data) > 84:  # More than just the header
                result["stl_base64"] = base64.b64encode(stl_data).decode()
                print(f"JewelForge: STL size={len(stl_data)} bytes")
            else:
                print(f"JewelForge: WARNING — STL is empty ({len(stl_data)} bytes)")
        if os.path.exists(output_glb):
            glb_data = open(output_glb, "rb").read()
            if len(glb_data) > 200:
                result["glb_base64"] = base64.b64encode(glb_data).decode()
                print(f"JewelForge: GLB size={len(glb_data)} bytes")
            else:
                print(f"JewelForge: WARNING — GLB is empty ({len(glb_data)} bytes)")

        return result

    finally:
        for f in [input_glb, output_stl, output_glb]:
            try:
                os.remove(f)
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

        result = {"success": True, "refined": True, "stats": stats}

        if os.path.exists(output_stl):
            stl_data = open(output_stl, "rb").read()
            if len(stl_data) > 84:
                result["stl_base64"] = base64.b64encode(stl_data).decode()
                print(f"JewelForge: STL size={len(stl_data)} bytes")
        if os.path.exists(output_glb):
            glb_data = open(output_glb, "rb").read()
            if len(glb_data) > 200:
                result["glb_base64"] = base64.b64encode(glb_data).decode()
                print(f"JewelForge: GLB size={len(glb_data)} bytes")

        return result

    finally:
        for f in [input_glb, output_stl, output_glb]:
            try:
                os.remove(f)
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
