"""
Blender headless scale + repair for JewelForge.
Called: blender --background --python scale_and_repair.py -- input.glb output.stl output.glb [params_json]

params_json: {"jewelry_type": "ring", "us_ring_size": 7, "height_mm": null}

Scales AI-generated mesh to real-world jewelry dimensions in mm,
repairs mesh issues, and exports production-ready STL + GLB.
"""

import sys
import json
import math
import bpy
import bmesh
from mathutils import Vector


# ─── US Ring Size → Inner Diameter (mm) ───────────
US_RING_SIZES = {
    3:    14.05,  3.5:  14.45,
    4:    14.86,  4.5:  15.27,
    5:    15.70,  5.5:  16.10,
    6:    16.51,  6.5:  16.92,
    7:    17.35,  7.5:  17.75,
    8:    18.19,  8.5:  18.53,
    9:    18.89,  9.5:  19.41,
    10:   19.84,  10.5: 20.20,
    11:   20.68,  11.5: 21.08,
    12:   21.49,  12.5: 21.89,
    13:   22.33,
}

# ─── Default dimensions by jewelry type (mm) ──────
DEFAULT_DIMENSIONS = {
    "ring":     {"target_axis": "x", "target_mm": 17.35},   # US size 7 inner diameter
    "pendant":  {"target_axis": "z", "target_mm": 25.0},    # 25mm tall
    "earring":  {"target_axis": "z", "target_mm": 15.0},    # 15mm tall
    "bracelet": {"target_axis": "x", "target_mm": 65.0},    # 65mm inner diameter
    "bangle":   {"target_axis": "x", "target_mm": 65.0},
    "necklace": {"target_axis": "z", "target_mm": 30.0},    # pendant portion
    "brooch":   {"target_axis": "x", "target_mm": 35.0},
    "other":    {"target_axis": "z", "target_mm": 25.0},
}


def get_args():
    argv = sys.argv
    if "--" in argv:
        return argv[argv.index("--") + 1:]
    return []


def clear_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)


def import_glb(filepath):
    bpy.ops.import_scene.gltf(filepath=filepath)
    return [obj for obj in bpy.context.scene.objects if obj.type == 'MESH']


def get_mesh_stats(obj):
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)

    non_manifold = sum(1 for e in bm.edges if not e.is_manifold)
    boundary = sum(1 for e in bm.edges if e.is_boundary)
    is_manifold = non_manifold == 0
    is_watertight = is_manifold and boundary == 0

    dims = obj.dimensions

    volume = 0.0
    if is_watertight:
        bm.faces.ensure_lookup_table()
        for face in bm.faces:
            if len(face.verts) >= 3:
                v0 = face.verts[0].co
                for i in range(1, len(face.verts) - 1):
                    v1 = face.verts[i].co
                    v2 = face.verts[i + 1].co
                    volume += v0.dot(v1.cross(v2)) / 6.0
        volume = abs(volume)

    stats = {
        "vertices": len(mesh.vertices),
        "faces": len(mesh.polygons),
        "is_manifold": is_manifold,
        "is_watertight": is_watertight,
        "non_manifold_edges": non_manifold,
        "bounding_box_mm": {
            "x": round(dims.x, 4),
            "y": round(dims.y, 4),
            "z": round(dims.z, 4),
        },
        "volume_mm3": round(volume, 4),
    }
    bm.free()
    return stats


def scale_to_mm(obj, jewelry_type, us_ring_size=None, height_mm=None):
    """Scale the mesh to real-world mm dimensions.

    Hitem3D outputs in arbitrary units (~1.0-1.5 range).
    We need to scale to actual mm based on jewelry type.
    """
    dims = obj.dimensions
    print(f"JewelForge: Raw dimensions: {dims.x:.4f} x {dims.y:.4f} x {dims.z:.4f}")

    # Determine target dimension
    jtype = jewelry_type.lower() if jewelry_type else "other"
    defaults = DEFAULT_DIMENSIONS.get(jtype, DEFAULT_DIMENSIONS["other"])

    if jtype == "ring" and us_ring_size is not None:
        target_mm = US_RING_SIZES.get(float(us_ring_size), 17.35)
        target_axis = "x"  # Ring diameter is along X (widest)
        print(f"JewelForge: Ring size US {us_ring_size} → {target_mm}mm inner diameter")
    elif height_mm is not None:
        target_mm = float(height_mm)
        target_axis = defaults["target_axis"]
        print(f"JewelForge: Custom height → {target_mm}mm along {target_axis}")
    else:
        target_mm = defaults["target_mm"]
        target_axis = defaults["target_axis"]
        print(f"JewelForge: Default {jtype} → {target_mm}mm along {target_axis}")

    # Get current dimension along target axis
    axis_map = {"x": dims.x, "y": dims.y, "z": dims.z}
    current_dim = axis_map[target_axis]

    if current_dim <= 0:
        print("JewelForge: WARNING — zero dimension, skipping scale")
        return target_mm

    # Convert target_mm to meters (Blender's internal unit)
    target_m = target_mm / 1000.0
    scale_factor = target_m / current_dim

    print(f"JewelForge: Scale factor: {scale_factor:.6f} ({current_dim:.4f}m → {target_m:.4f}m = {target_mm}mm)")

    # Apply uniform scale
    obj.scale *= scale_factor
    bpy.context.view_layer.update()

    # Apply scale transform so dimensions are baked
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    new_dims = obj.dimensions
    print(f"JewelForge: Scaled dimensions: {new_dims.x*1000:.2f} x {new_dims.y*1000:.2f} x {new_dims.z*1000:.2f} mm")

    return target_mm


def light_cleanup(obj):
    """Fix minor mesh issues."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')

    bpy.ops.mesh.remove_doubles(threshold=0.00001)
    bpy.ops.mesh.delete_loose(use_verts=True, use_edges=True, use_faces=False)
    bpy.ops.mesh.normals_make_consistent(inside=False)

    bpy.ops.object.mode_set(mode='OBJECT')


def sharpen_edges(obj, angle_threshold_deg=35):
    """Mark sharp edges for prongs, settings, pave."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    angle_threshold = math.radians(angle_threshold_deg)
    sharp_count = 0
    crease_layer = bm.edges.layers.crease.verify()

    for edge in bm.edges:
        if len(edge.link_faces) == 2:
            face_angle = edge.calc_face_angle(0)
            if face_angle > angle_threshold:
                edge.smooth = False
                edge[crease_layer] = 1.0
                sharp_count += 1

    bm.to_mesh(mesh)
    bm.free()

    mesh.use_auto_smooth = True
    mesh.auto_smooth_angle = angle_threshold

    if sharp_count > 0:
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.mesh.edges_select_sharp(sharpness=angle_threshold)
        bpy.ops.object.mode_set(mode='OBJECT')

    print(f"JewelForge: Marked {sharp_count} sharp edges (threshold={angle_threshold_deg}°)")
    return sharp_count


def smooth_surface(obj):
    """Smooth shading with sharp edge preservation."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.shade_smooth()
    print("JewelForge: Applied smooth shading with sharp edge preservation")


def apply_weighted_normals(obj):
    """Weighted normals for better curved surface rendering."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        mod = obj.modifiers.new(name="WeightedNormal", type='WEIGHTED_NORMAL')
        mod.weight = 50
        mod.mode = 'FACE_AREA'
        mod.keep_sharp = True
        bpy.ops.object.modifier_apply(modifier=mod.name)
        print("JewelForge: Applied weighted normals")
    except Exception as e:
        print(f"JewelForge: Weighted normals skipped — {e}")


def decimate_if_needed(obj, target_faces=200000):
    """Decimate only if over target face count."""
    face_count = len(obj.data.polygons)
    if face_count <= target_faces:
        print(f"JewelForge: {face_count} faces, no decimation needed")
        return

    ratio = target_faces / face_count
    print(f"JewelForge: Decimating {face_count} -> ~{target_faces} faces (ratio={ratio:.4f})")

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    mod = obj.modifiers.new(name="Decimate", type='DECIMATE')
    mod.decimate_type = 'COLLAPSE'
    mod.ratio = ratio
    mod.use_collapse_triangulate = True

    bpy.ops.object.modifier_apply(modifier=mod.name)
    print(f"JewelForge: After decimation: {len(obj.data.polygons)} faces")


def export_stl(filepath):
    """Export STL in mm (global_scale=1000 converts Blender meters to mm)."""
    bpy.ops.export_mesh.stl(
        filepath=filepath,
        use_selection=True,
        global_scale=1000.0,
        ascii=False,
    )


def export_glb(filepath):
    bpy.ops.export_scene.gltf(
        filepath=filepath,
        export_format='GLB',
        use_selection=True,
        export_apply=True,
    )


def main():
    args = get_args()
    if len(args) < 3:
        print("Usage: blender --background --python scale_and_repair.py -- input.glb output.stl output.glb [params_json]")
        sys.exit(1)

    input_path = args[0]
    output_stl = args[1]
    output_glb = args[2]

    # Parse optional params
    params = {}
    if len(args) >= 4:
        try:
            params = json.loads(args[3])
        except json.JSONDecodeError as e:
            print(f"JewelForge: WARNING — could not parse params: {e}")

    jewelry_type = params.get("jewelry_type", "ring")
    us_ring_size = params.get("us_ring_size")
    height_mm = params.get("height_mm")

    print(f"JewelForge: Processing {input_path}")
    print(f"JewelForge: Params — type={jewelry_type}, ring_size={us_ring_size}, height_mm={height_mm}")

    # Clear and import
    clear_scene()
    mesh_objects = import_glb(input_path)

    if not mesh_objects:
        print("JewelForge: ERROR - No mesh found in GLB!")
        sys.exit(1)

    # Join all meshes
    if len(mesh_objects) > 1:
        bpy.context.view_layer.objects.active = mesh_objects[0]
        for obj in mesh_objects:
            obj.select_set(True)
        bpy.ops.object.join()
        mesh_objects = [bpy.context.active_object]

    obj = mesh_objects[0]
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    # Stats before
    initial_stats = get_mesh_stats(obj)
    print(f"JewelForge: Input — {initial_stats['vertices']} verts, {initial_stats['faces']} faces, "
          f"manifold={initial_stats['is_manifold']}, watertight={initial_stats['is_watertight']}")

    # Step 1: Scale to real-world mm
    target_mm = scale_to_mm(obj, jewelry_type, us_ring_size, height_mm)

    # Step 2: Light cleanup
    light_cleanup(obj)

    # Step 3: Edge sharpening
    sharpen_edges(obj, angle_threshold_deg=35)

    # Step 4: Smooth shading
    smooth_surface(obj)

    # Step 5: Weighted normals
    apply_weighted_normals(obj)

    # Step 6: Decimate if needed
    decimate_if_needed(obj, target_faces=200000)

    # Step 7: Final stats
    final_stats = get_mesh_stats(obj)
    # Stats are in Blender meters — convert to mm for output
    final_dims = obj.dimensions
    final_stats["bounding_box_mm"] = {
        "x": round(final_dims.x * 1000, 2),
        "y": round(final_dims.y * 1000, 2),
        "z": round(final_dims.z * 1000, 2),
    }
    print(f"JewelForge: Output — {final_stats['vertices']} verts, {final_stats['faces']} faces")
    print(f"JewelForge: Final size — {final_dims.x*1000:.2f} x {final_dims.y*1000:.2f} x {final_dims.z*1000:.2f} mm")

    # Step 8: Export
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    export_stl(output_stl)
    print(f"JewelForge: STL exported to {output_stl}")

    export_glb(output_glb)
    print(f"JewelForge: GLB exported to {output_glb}")

    # Output stats JSON
    stats = {
        "input_vertices": initial_stats["vertices"],
        "input_faces": initial_stats["faces"],
        "output_vertices": final_stats["vertices"],
        "output_faces": final_stats["faces"],
        "is_manifold": final_stats["is_manifold"],
        "is_watertight": final_stats["is_watertight"],
        "bounding_box_mm": final_stats["bounding_box_mm"],
        "volume_mm3": final_stats["volume_mm3"],
        "jewelry_type": jewelry_type,
        "target_mm": target_mm,
        "us_ring_size": us_ring_size,
    }
    print(f"JEWELFORGE_STATS:{json.dumps(stats)}")
    print("JewelForge: Done!")


if __name__ == "__main__":
    main()
