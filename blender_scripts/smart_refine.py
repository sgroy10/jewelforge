"""
Smart Refine — Jewelry-aware mesh engineering via Blender modifiers.

Unlike scale_and_repair.py (which scales to target weight), this uses
SHELLING (Solidify modifier) to hollow the mesh — the same technique
jewelers use in Rhino. Weight drops naturally from wall thickness, not
from distorting proportions.

Called: blender --background --python smart_refine.py -- input.glb output.stl output.glb [params_json]

params_json:
  {
    "jewelry_type": "ring",
    "us_ring_size": 7,
    "target_weight_grams": 3.0,
    "metal_type": "gold_14k",
    "wall_thickness_mm": 0.8
  }
"""

import sys
import json
import math
import bpy
import bmesh

# ─── Constants ────────────────────────────────────

US_RING_SIZES = {
    3: 14.05, 3.5: 14.45, 4: 14.86, 4.5: 15.27,
    5: 15.70, 5.5: 16.10, 6: 16.51, 6.5: 16.92,
    7: 17.35, 7.5: 17.75, 8: 18.19, 8.5: 18.53,
    9: 18.89, 9.5: 19.41, 10: 19.84, 10.5: 20.20,
    11: 20.68, 11.5: 21.08, 12: 21.49, 12.5: 21.89,
    13: 22.33,
}

METAL_DENSITIES = {
    "gold_14k": 0.01333, "gold_18k": 0.01540, "gold_22k": 0.01760,
    "silver_925": 0.01030, "platinum_950": 0.02140,
}

DEFAULT_DIMENSIONS = {
    "ring":    {"target_mm": 17.35},
    "pendant": {"target_mm": 25.0},
    "earring": {"target_mm": 15.0},
}


# ─── Helpers ──────────────────────────────────────

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


def compute_volume_mm3(obj):
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    vol = 0.0
    for face in bm.faces:
        if len(face.verts) >= 3:
            v0 = face.verts[0].co
            for i in range(1, len(face.verts) - 1):
                v1 = face.verts[i].co
                v2 = face.verts[i + 1].co
                vol += v0.dot(v1.cross(v2)) / 6.0
    bm.free()
    return abs(vol) * 1.0e9


def get_mesh_stats(obj):
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    non_manifold = sum(1 for e in bm.edges if not e.is_manifold)
    boundary = sum(1 for e in bm.edges if e.is_boundary)
    bm.free()
    is_manifold = non_manifold == 0
    is_watertight = is_manifold and boundary == 0
    dims = obj.dimensions
    vol = compute_volume_mm3(obj)
    return {
        "vertices": len(mesh.vertices),
        "faces": len(mesh.polygons),
        "is_manifold": is_manifold,
        "is_watertight": is_watertight,
        "non_manifold_edges": non_manifold,
        "bounding_box_mm": {
            "x": round(dims.x * 1000, 2),
            "y": round(dims.y * 1000, 2),
            "z": round(dims.z * 1000, 2),
        },
        "volume_mm3": round(vol, 4),
        "estimated_weight_grams": {
            m: round(vol * d, 4) for m, d in METAL_DENSITIES.items()
        },
    }


def estimated_weight(vol_mm3, metal_type):
    d = METAL_DENSITIES.get(metal_type, 0.01333)
    return vol_mm3 * d


# ─── Core operations ─────────────────────────────

def decimate_if_needed(obj, target_faces=200000):
    face_count = len(obj.data.polygons)
    if face_count <= target_faces:
        return
    ratio = target_faces / face_count
    print(f"SmartRefine: Decimating {face_count} → ~{target_faces} faces")
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    mod = obj.modifiers.new("Decimate", type='DECIMATE')
    mod.decimate_type = 'COLLAPSE'
    mod.ratio = ratio
    mod.use_collapse_triangulate = True
    bpy.ops.object.modifier_apply(modifier=mod.name)
    print(f"SmartRefine: After decimation: {len(obj.data.polygons)} faces")


def scale_to_ring_size(obj, us_ring_size):
    target_mm = US_RING_SIZES.get(float(us_ring_size), 17.35)
    dims = obj.dimensions
    dim_axes = sorted(
        [("x", dims.x), ("y", dims.y), ("z", dims.z)],
        key=lambda p: p[1],
    )
    target_axis = dim_axes[1][0]
    axis_map = {"x": dims.x, "y": dims.y, "z": dims.z}
    current_dim = axis_map[target_axis]
    if current_dim <= 0:
        print("SmartRefine: WARNING — zero dimension, skipping ring scale")
        return target_mm
    target_m = target_mm / 1000.0
    scale_factor = target_m / current_dim
    print(f"SmartRefine: Ring US {us_ring_size} → {target_mm}mm, "
          f"scaling {target_axis}-axis by {scale_factor:.4f}")
    obj.scale *= scale_factor
    bpy.context.view_layer.update()
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    new_dims = obj.dimensions
    print(f"SmartRefine: Scaled dims: {new_dims.x*1000:.2f} x "
          f"{new_dims.y*1000:.2f} x {new_dims.z*1000:.2f} mm")
    return target_mm


def light_cleanup(obj):
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.remove_doubles(threshold=0.00001)
    bpy.ops.mesh.delete_loose(use_verts=True, use_edges=True, use_faces=False)
    try:
        bpy.ops.mesh.fill_holes(sides=0)
    except RuntimeError:
        pass
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode='OBJECT')


def recalc_normals(obj):
    """Aggressively recalculate all normals outward before shelling.

    Solidify pushes geometry in the normal direction. If normals are
    flipped on some faces, the shell extrudes outward instead of inward,
    ballooning the bbox. This runs recalculate-outside on the entire mesh.
    """
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.mesh.set_normals_from_faces()
    bpy.ops.object.mode_set(mode='OBJECT')
    print("SmartRefine: Normals recalculated outward")


def apply_shell(obj, thickness_mm):
    """Hollow the mesh via Boolean subtraction of a scaled-down copy.

    Solidify fails on non-manifold AI meshes (bbox balloons from normals
    issues). Boolean DIFFERENCE is more robust: we subtract a uniformly
    smaller copy from the original. Outer surface stays exactly in place.
    """
    thickness_m = thickness_mm / 1000.0
    dims = obj.dimensions

    # Compute per-axis scale for the inner copy: (dim - 2*thickness) / dim
    def inner_scale(dim):
        if dim <= 0:
            return 0.9
        s = (dim - 2 * thickness_m) / dim
        return max(0.1, min(0.99, s))

    sx = inner_scale(dims.x)
    sy = inner_scale(dims.y)
    sz = inner_scale(dims.z)
    print(f"SmartRefine: Boolean shell — wall {thickness_mm}mm, "
          f"inner scale ({sx:.3f}, {sy:.3f}, {sz:.3f})")

    # Duplicate mesh for inner cutter
    inner = obj.copy()
    inner.data = obj.data.copy()
    bpy.context.scene.collection.objects.link(inner)
    inner.scale = (sx, sy, sz)
    bpy.context.view_layer.update()
    bpy.ops.object.select_all(action='DESELECT')
    inner.select_set(True)
    bpy.context.view_layer.objects.active = inner
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    # Boolean DIFFERENCE: original minus inner
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    mod = obj.modifiers.new("Shell", type='BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.solver = 'EXACT'
    mod.object = inner

    try:
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(inner, do_unlink=True)
        new_dims = obj.dimensions
        print(f"SmartRefine: Boolean shell applied — "
              f"bbox {new_dims.x*1000:.2f} x {new_dims.y*1000:.2f} x "
              f"{new_dims.z*1000:.2f} mm")
        return True
    except RuntimeError as e:
        print(f"SmartRefine: WARNING — Boolean shell failed: {e}")
        try:
            obj.modifiers.remove(mod)
        except:
            pass
        bpy.data.objects.remove(inner, do_unlink=True)
        return False


def shell_to_target_weight(obj, target_g, metal_type, min_wall=0.3, max_wall=2.0):
    """Iterate wall thickness until weight matches target (±10%).

    Starts at 0.8mm, adjusts up/down based on computed weight.
    Uses binary search for fast convergence.
    """
    density = METAL_DENSITIES.get(metal_type, 0.01333)

    # Save original mesh for re-shelling
    orig_mesh = obj.data.copy()

    lo, hi = min_wall, max_wall
    best_wall = 0.8
    best_delta = 100.0
    iterations = 0
    max_iters = 5

    for i in range(max_iters):
        iterations = i + 1
        wall = (lo + hi) / 2.0

        # Reset to original mesh before re-shelling
        obj.data = orig_mesh.copy()
        bpy.context.view_layer.update()

        success = apply_shell(obj, wall)
        if not success:
            print(f"SmartRefine: Shell failed at {wall:.2f}mm, trying thicker")
            lo = wall
            continue

        vol = compute_volume_mm3(obj)
        weight = vol * density
        delta_pct = (weight - target_g) / target_g * 100.0
        print(f"SmartRefine: Iter {iterations}: wall={wall:.3f}mm, "
              f"vol={vol:.1f}mm³, weight={weight:.2f}g ({delta_pct:+.1f}%)")

        if abs(delta_pct) < abs(best_delta):
            best_wall = wall
            best_delta = delta_pct

        if abs(delta_pct) <= 10.0:
            break

        if weight > target_g:
            hi = wall
        else:
            lo = wall

    # Final shell at best wall thickness
    if abs(best_delta) > 10.0 or iterations > 1:
        obj.data = orig_mesh.copy()
        bpy.context.view_layer.update()
        apply_shell(obj, best_wall)

    # Clean up the copy
    bpy.data.meshes.remove(orig_mesh)

    final_vol = compute_volume_mm3(obj)
    final_weight = final_vol * density
    final_delta = (final_weight - target_g) / target_g * 100.0

    print(f"SmartRefine: Final — wall={best_wall:.3f}mm, "
          f"weight={final_weight:.2f}g, delta={final_delta:+.1f}%")

    return {
        "shell_applied": True,
        "wall_thickness_mm": round(best_wall, 3),
        "iterations": iterations,
        "weight_delta_percent": round(final_delta, 2),
    }


def export_stl(filepath):
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


# ─── Main ─────────────────────────────────────────

def main():
    args = get_args()
    if len(args) < 3:
        print("Usage: blender --background --python smart_refine.py -- "
              "input.glb output.stl output.glb [params_json]")
        sys.exit(1)

    input_path = args[0]
    output_stl = args[1]
    output_glb = args[2]

    params = {}
    if len(args) >= 4:
        try:
            params = json.loads(args[3])
        except json.JSONDecodeError as e:
            print(f"SmartRefine: WARNING — bad params: {e}")

    jewelry_type = params.get("jewelry_type", "ring")
    us_ring_size = params.get("us_ring_size")
    target_weight = params.get("target_weight_grams")
    metal_type = params.get("metal_type", "gold_14k")
    wall_thickness = params.get("wall_thickness_mm")

    print(f"SmartRefine: Processing {input_path}")
    print(f"SmartRefine: Params — type={jewelry_type}, size={us_ring_size}, "
          f"target={target_weight}g, metal={metal_type}, wall={wall_thickness}mm")

    # Import
    clear_scene()
    mesh_objects = import_glb(input_path)
    if not mesh_objects:
        print("SmartRefine: ERROR — no mesh in GLB")
        sys.exit(1)

    # Join all meshes
    if len(mesh_objects) > 1:
        bpy.context.view_layer.objects.active = mesh_objects[0]
        for o in mesh_objects:
            o.select_set(True)
        bpy.ops.object.join()
        mesh_objects = [bpy.context.active_object]

    obj = mesh_objects[0]
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    # Early decimate
    if len(obj.data.polygons) > 200000:
        print(f"SmartRefine: Early decimate {len(obj.data.polygons)} → 200k")
        decimate_if_needed(obj, target_faces=200000)

    # Step 1: Ring sizing (before stats so input_stats show real-world mm)
    target_mm = None
    if jewelry_type.lower() == "ring" and us_ring_size is not None:
        target_mm = scale_to_ring_size(obj, us_ring_size)

    # Step 2: Light cleanup + fill holes
    light_cleanup(obj)

    # Input stats (after sizing + cleanup so volumes are in mm³ not m³)
    input_stats = get_mesh_stats(obj)
    print(f"SmartRefine: Input — {input_stats['vertices']} verts, "
          f"{input_stats['faces']} faces, vol={input_stats['volume_mm3']:.1f}mm³, "
          f"~{input_stats['estimated_weight_grams'].get(metal_type, 0):.1f}g {metal_type}")

    # Step 3: SHELL to target weight (the key operation)
    shell_stats = {"shell_applied": False}
    if target_weight is not None and float(target_weight) > 0:
        if wall_thickness is not None:
            # Fixed wall thickness — just apply it
            success = apply_shell(obj, float(wall_thickness))
            shell_stats = {
                "shell_applied": success,
                "wall_thickness_mm": float(wall_thickness),
                "iterations": 1,
                "weight_delta_percent": None,
            }
        else:
            # Auto-find wall thickness to hit target weight
            shell_stats = shell_to_target_weight(
                obj, float(target_weight), metal_type,
            )
    elif wall_thickness is not None:
        success = apply_shell(obj, float(wall_thickness))
        shell_stats = {
            "shell_applied": success,
            "wall_thickness_mm": float(wall_thickness),
            "iterations": 1,
        }

    # Step 4: Post-shell cleanup
    light_cleanup(obj)

    # Step 5: Smooth + sharpen
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.shade_smooth()
    mesh = obj.data
    mesh.use_auto_smooth = True
    mesh.auto_smooth_angle = math.radians(35)

    # Final stats
    final_stats = get_mesh_stats(obj)
    print(f"SmartRefine: Output — {final_stats['vertices']} verts, "
          f"{final_stats['faces']} faces, vol={final_stats['volume_mm3']:.1f}mm³")
    dims = obj.dimensions
    print(f"SmartRefine: Final size — {dims.x*1000:.2f} x "
          f"{dims.y*1000:.2f} x {dims.z*1000:.2f} mm")

    # Export
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    export_stl(output_stl)
    export_glb(output_glb)
    print(f"SmartRefine: Exported STL + GLB")

    # Stats JSON
    stats = {
        "input_vertices": input_stats["vertices"],
        "input_faces": input_stats["faces"],
        "input_volume_mm3": input_stats["volume_mm3"],
        "input_weight_grams": input_stats["estimated_weight_grams"],
        "output_vertices": final_stats["vertices"],
        "output_faces": final_stats["faces"],
        "output_volume_mm3": final_stats["volume_mm3"],
        "output_weight_grams": final_stats["estimated_weight_grams"],
        "is_manifold": final_stats["is_manifold"],
        "is_watertight": final_stats["is_watertight"],
        "bounding_box_mm": final_stats["bounding_box_mm"],
        "jewelry_type": jewelry_type,
        "target_mm": target_mm,
        "us_ring_size": us_ring_size,
        "target_weight_grams": target_weight,
        "metal_type": metal_type,
    }
    stats.update(shell_stats)
    print(f"SMARTREFINE_STATS:{json.dumps(stats)}")
    print("SmartRefine: Done!")


if __name__ == "__main__":
    main()
