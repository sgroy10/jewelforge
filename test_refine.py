"""Test just the refine endpoint with a known GLB to verify Blender output."""
import httpx
import base64
import json

BASE = "https://jewelforge-production.up.railway.app"

# First test: just call analyze with a test image to verify API works
print("Testing /api/health...")
r = httpx.get(f"{BASE}/api/health", timeout=30)
print(f"  {r.json()}")

# Test: check what the previous refine actually returned
# Let's call refine with a known good test - a tiny GLB
# First generate a minimal valid GLB binary
import struct

def make_tiny_glb():
    """Create a minimal valid GLB with a triangle."""
    # Minimal glTF JSON
    gltf_json = json.dumps({
        "asset": {"version": "2.0", "generator": "test"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
             "max": [1.0, 1.0, 0.0], "min": [-1.0, -1.0, 0.0]},
            {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR",
             "max": [2], "min": [0]}
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": 36},
            {"buffer": 0, "byteOffset": 36, "byteLength": 6}
        ],
        "buffers": [{"byteLength": 44}]
    }).encode()

    # Pad JSON to 4-byte alignment
    while len(gltf_json) % 4 != 0:
        gltf_json += b' '

    # Binary data: 3 vertices (VEC3) + 3 indices (UNSIGNED_SHORT)
    import struct
    vertices = struct.pack('<9f', -1.0, -1.0, 0.0, 1.0, -1.0, 0.0, 0.0, 1.0, 0.0)
    indices = struct.pack('<3H', 0, 1, 2)
    padding = b'\x00\x00'  # pad to 4 bytes
    bin_data = vertices + indices + padding

    # GLB header
    total_len = 12 + 8 + len(gltf_json) + 8 + len(bin_data)
    header = struct.pack('<III', 0x46546C67, 2, total_len)  # magic, version, length
    json_chunk = struct.pack('<II', len(gltf_json), 0x4E4F534A) + gltf_json  # JSON chunk
    bin_chunk = struct.pack('<II', len(bin_data), 0x004E4942) + bin_data  # BIN chunk

    return header + json_chunk + bin_chunk

print("\nCreating test GLB...")
test_glb = make_tiny_glb()
print(f"  GLB size: {len(test_glb)} bytes")
print(f"  Magic: {test_glb[:4]}")

glb_b64 = base64.b64encode(test_glb).decode()

print("\nTesting /api/refine with test GLB...")
r = httpx.post(
    f"{BASE}/api/refine",
    data={"glb_base64": glb_b64},
    timeout=180,
)
print(f"  Status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    print(f"  success: {data.get('success')}")
    print(f"  refined: {data.get('refined')}")
    print(f"  stats: {json.dumps(data.get('stats', {}), indent=2)}")
    stl = data.get('stl_base64', '')
    glb = data.get('glb_base64', '')
    print(f"  STL b64 len: {len(stl)}")
    print(f"  GLB b64 len: {len(glb)}")
    if stl:
        stl_bytes = base64.b64decode(stl)
        print(f"  STL decoded: {len(stl_bytes)} bytes")
        print(f"  STL first 20 bytes: {stl_bytes[:20]}")
        with open("test_refine_output.stl", "wb") as f:
            f.write(stl_bytes)
        print("  Saved test_refine_output.stl")
    if glb:
        glb_bytes = base64.b64decode(glb)
        print(f"  GLB decoded: {len(glb_bytes)} bytes")
        print(f"  GLB magic: {glb_bytes[:4]}")
else:
    print(f"  Error: {r.text[:500]}")

print("\nDone!")
