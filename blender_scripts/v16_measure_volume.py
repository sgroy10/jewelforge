"""
Tiny Blender script: import GLB, measure mesh volume + bbox, print JSON.

Used by ring_pipeline_v16 to compute the solid weight of a scaled mesh
without running the full voxel-remesh + Solidify hollowing pipeline
(which is slow and unnecessary for a volume read).

Usage:
    blender --background --python v16_measure_volume.py -- /abs/in.glb

Output:
    MEASURE_VOLUME_RESULT:{"volume_mm3": ..., "bbox_x_mm": ..., ...}
"""

import sys
import json
import bpy
import bmesh


def get_args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def main():
    args = get_args()
    if not args:
        print("Usage: -- /abs/in.glb")
        sys.exit(1)
    in_glb = args[0]

    # Clear scene
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    # Import GLB and bake importer transform
    bpy.ops.import_scene.gltf(filepath=in_glb)
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if not meshes:
        print('MEASURE_VOLUME_RESULT:{"error": "no_mesh"}')
        sys.exit(2)
    if len(meshes) > 1:
        bpy.context.view_layer.objects.active = meshes[0]
        for m in meshes:
            m.select_set(True)
        bpy.ops.object.join()
        meshes = [bpy.context.active_object]
    obj = meshes[0]
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    # Divergence-theorem signed volume (same formula as blender_hollow.py)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    v = 0.0
    for f in bm.faces:
        if len(f.verts) >= 3:
            v0 = f.verts[0].co
            for i in range(1, len(f.verts) - 1):
                v1 = f.verts[i].co
                v2 = f.verts[i + 1].co
                v += v0.dot(v1.cross(v2)) / 6.0
    bm.free()

    # Blender coords in meters; *1e9 to convert m^3 -> mm^3
    volume_mm3 = abs(v) * 1.0e9
    dims = obj.dimensions
    result = {
        "volume_mm3": round(volume_mm3, 3),
        "bbox_mm": {
            "x": round(dims.x * 1000, 3),
            "y": round(dims.y * 1000, 3),
            "z": round(dims.z * 1000, 3),
        },
        "vertex_count": len(obj.data.vertices),
        "face_count": len(obj.data.polygons),
    }
    print(f"MEASURE_VOLUME_RESULT:{json.dumps(result)}")


if __name__ == "__main__":
    main()
