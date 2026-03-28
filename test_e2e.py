"""Real end-to-end test."""
import httpx, base64, json

BASE = "https://jewelforge-production.up.railway.app"

print("1. Generating image...")
r = httpx.post(f"{BASE}/api/generate-image", data={"prompt": "simple plain gold ring band"}, timeout=120)
assert r.status_code == 200, f"Failed: {r.text[:200]}"
img = r.json()["image_base64"]
print(f"   Image OK: {len(img)} chars")

print("2. Generating 3D mesh...")
r = httpx.post(f"{BASE}/api/generate-3d", data={"image_base64": img, "engine": "hitem3d"}, timeout=600)
assert r.status_code == 200, f"Failed: {r.text[:200]}"
url = r.json()["url"]
print(f"   Mesh URL OK: {url[:80]}...")

print("3. Refining with Blender...")
r = httpx.post(f"{BASE}/api/refine", data={"glb_url": url}, timeout=300)
assert r.status_code == 200, f"Failed: {r.text[:200]}"
d = r.json()
print(f"   success={d.get('success')} refined={d.get('refined')}")
print(f"   stats={json.dumps(d.get('stats',{}), indent=2)}")

stl = d.get("stl_base64", "")
glb = d.get("glb_base64", "")
print(f"   STL b64: {len(stl)} chars")
print(f"   GLB b64: {len(glb)} chars")

if stl:
    s = base64.b64decode(stl)
    print(f"   STL: {len(s)} bytes")
    open("FINAL_test.stl", "wb").write(s)
    print("   SAVED: FINAL_test.stl")
if glb:
    g = base64.b64decode(glb)
    print(f"   GLB: {len(g)} bytes, magic={g[:4]}")
    open("FINAL_test.glb", "wb").write(g)
    print("   SAVED: FINAL_test.glb")

print("\nDONE!")
