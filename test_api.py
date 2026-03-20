"""End-to-end test of the JewelForge API — find out what's really happening."""
import httpx
import base64
import json
import sys

BASE = "https://jewelforge-production.up.railway.app"

print("=" * 60)
print("JEWELFORGE END-TO-END API TEST")
print("=" * 60)

# Step 1: Generate image
print("\n[Step 1] Generating image from prompt...")
try:
    r = httpx.post(
        f"{BASE}/api/generate-image",
        data={"prompt": "simple plain gold ring band"},
        timeout=120,
    )
    print(f"  HTTP Status: {r.status_code}")
    if r.status_code != 200:
        print(f"  ERROR: {r.text[:500]}")
        sys.exit(1)
    data = r.json()
    img_b64 = data.get("image_base64", "")
    print(f"  Image base64 length: {len(img_b64)}")
    analysis = data.get("analysis", {})
    print(f"  Analysis: {json.dumps(analysis)[:200]}")
    if not img_b64:
        print("  FATAL: No image returned!")
        sys.exit(1)
except Exception as e:
    print(f"  EXCEPTION: {e}")
    sys.exit(1)

# Step 2: Generate 3D mesh
print("\n[Step 2] Generating 3D mesh (this takes 1-5 min)...")
try:
    r = httpx.post(
        f"{BASE}/api/generate-3d",
        data={"image_base64": img_b64, "engine": "hitem3d"},
        timeout=600,
    )
    print(f"  HTTP Status: {r.status_code}")
    if r.status_code != 200:
        print(f"  ERROR: {r.text[:500]}")
        sys.exit(1)
    mesh_data = r.json()
    print(f"  Engine: {mesh_data.get('engine')}")
    mesh_url = mesh_data.get("url", "")
    print(f"  Mesh URL: {mesh_url[:120]}")
    if not mesh_url:
        print("  FATAL: No mesh URL!")
        sys.exit(1)
except Exception as e:
    print(f"  EXCEPTION: {e}")
    sys.exit(1)

# Step 2b: Download raw GLB to verify
print("\n[Step 2b] Downloading raw GLB to verify...")
try:
    glb_r = httpx.get(mesh_url, timeout=60, follow_redirects=True)
    print(f"  Download status: {glb_r.status_code}")
    print(f"  Content-Type: {glb_r.headers.get('content-type', 'unknown')}")
    raw_glb = glb_r.content
    print(f"  Size: {len(raw_glb)} bytes")
    magic = raw_glb[:4]
    print(f"  Magic bytes: {magic}")
    print(f"  Is valid glTF: {magic == b'glTF'}")
    with open("test_raw.glb", "wb") as f:
        f.write(raw_glb)
    print("  Saved to test_raw.glb")
except Exception as e:
    print(f"  EXCEPTION: {e}")

# Step 3: Refine with Blender
print("\n[Step 3] Sending to Blender refine endpoint...")
try:
    r = httpx.post(
        f"{BASE}/api/refine",
        data={"glb_url": mesh_url},
        timeout=180,
    )
    print(f"  HTTP Status: {r.status_code}")
    if r.status_code != 200:
        print(f"  ERROR: {r.text[:500]}")
        sys.exit(1)
    refine = r.json()
    print(f"  success: {refine.get('success')}")
    print(f"  refined: {refine.get('refined')}")
    print(f"  stats: {json.dumps(refine.get('stats', {}), indent=2)}")

    stl_b64 = refine.get("stl_base64", "")
    glb_b64 = refine.get("glb_base64", "")
    print(f"  stl_base64 length: {len(stl_b64)}")
    print(f"  glb_base64 length: {len(glb_b64)}")

    if stl_b64:
        stl_bytes = base64.b64decode(stl_b64)
        print(f"  STL decoded size: {len(stl_bytes)} bytes")
        print(f"  STL first 80 bytes: {stl_bytes[:80]}")
        with open("test_output.stl", "wb") as f:
            f.write(stl_bytes)
        print("  Saved to test_output.stl")
    else:
        print("  NO STL OUTPUT!")

    if glb_b64:
        glb_bytes = base64.b64decode(glb_b64)
        print(f"  GLB decoded size: {len(glb_bytes)} bytes")
        print(f"  GLB magic: {glb_bytes[:4]}")
        print(f"  Is valid glTF: {glb_bytes[:4] == b'glTF'}")
        with open("test_output.glb", "wb") as f:
            f.write(glb_bytes)
        print("  Saved to test_output.glb")
    else:
        print("  NO GLB OUTPUT!")

except Exception as e:
    print(f"  EXCEPTION: {e}")

print("\n" + "=" * 60)
print("TEST COMPLETE — check test_raw.glb, test_output.stl, test_output.glb")
print("=" * 60)
