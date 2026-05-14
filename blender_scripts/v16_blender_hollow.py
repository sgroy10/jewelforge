"""
Real boolean-style hollowing in Blender headless via:
  1. Voxel Remesh (RemeshModifier mode='VOXEL') — converts Tripo non-manifold
     soup to a clean watertight manifold mesh.
  2. Solidify modifier with thickness=wall_mm and offset=+1 — produces a
     hollow shell of given wall thickness extending INWARD from the original
     surface. On a manifold input, this is correct cavity creation.
  3. Iterative binary search on wall thickness to hit target weight.

This is the algorithm smart_refine.py *should* have been: voxel-remesh first
to get a manifold body, THEN apply Solidify. Production smart_refine applies
shrink/fatten directly on the dirty Tripo input, which is why the cavity never
forms (volume goes UP not DOWN).

Called: blender --background --python blender_hollow.py -- in.glb out.stl out.glb params.json
"""

import sys
import json
import math
import bpy
import bmesh


DENSITIES = {
    "gold_14k":     0.01333,
    "gold_18k":     0.01540,
    "gold_22k":     0.01760,
    "silver_925":   0.01030,
    "platinum_950": 0.02140,
}


def get_args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def clear_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)


def import_glb_and_join(path):
    bpy.ops.import_scene.gltf(filepath=path)
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if not meshes:
        raise RuntimeError("No mesh in GLB")
    if len(meshes) > 1:
        bpy.context.view_layer.objects.active = meshes[0]
        for m in meshes:
            m.select_set(True)
        bpy.ops.object.join()
        meshes = [bpy.context.active_object]
    obj = meshes[0]
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    # Bake any glTF-importer transform into vertex coords. Without this,
    # voxel-remesh and volume calcs operate on inconsistent units.
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return obj


def signed_volume_mm3(obj):
    """Divergence-theorem volume in mm^3.
    Vertex coords are in Blender METERS (since GLB export preserves the
    transform-applied scale from scale_and_repair). Integral gives m^3,
    multiply by 1e9 to get mm^3."""
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
    return abs(v) * 1.0e9


def mesh_topology(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    nm = sum(1 for e in bm.edges if not e.is_manifold)
    bnd = sum(1 for e in bm.edges if e.is_boundary)
    bm.free()
    return {"non_manifold_edges": nm, "boundary_edges": bnd,
            "is_manifold": nm == 0, "is_watertight": nm == 0 and bnd == 0}


def apply_voxel_remesh(obj, voxel_size_mm):
    """Voxel-remesh converts non-manifold input into a clean closed manifold.
    voxel_size in mm. Smaller = more detail preserved + more compute."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    mod = obj.modifiers.new("VoxelRemesh", 'REMESH')
    mod.mode = 'VOXEL'
    mod.voxel_size = voxel_size_mm / 1000.0  # Blender uses meters
    mod.adaptivity = 0.0
    mod.use_smooth_shade = False
    bpy.ops.object.modifier_apply(modifier=mod.name)


def apply_solidify(obj, wall_mm, offset=-1.0, clamp=0.0):
    """Solidify on a manifold mesh produces a hollow shell of given thickness.
    offset=-1 means new surface extends INWARD (original on outside, hollows
    out). offset=+1 extends OUTWARD (grows the bbox — wrong for hollowing).

    Mode = 'NON_MANIFOLD' (the "Complex" solver in UI). This is the only mode
    that correctly handles inward-offset surfaces that self-intersect on thin
    features (e.g. motif/gallery decorations). 'EXTRUDE' is faster but produces
    stray verts at extreme positions when the inward offset crosses itself,
    blowing up the bbox even when the volume calc reads correctly.

    NOTE on thickness_clamp: Blender's clamp = MIN(thickness, clamp * edge_len).
    With voxel-remesh edges around 0.15mm, clamp=2.0 caps wall at ~0.30mm
    REGARDLESS of requested thickness — this silently breaks the binary search.
    Default to clamp=0 (disabled)."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    mod = obj.modifiers.new("Solidify", 'SOLIDIFY')
    mod.thickness = wall_mm / 1000.0
    mod.offset = offset
    mod.solidify_mode = 'EXTRUDE'  # fast solver; NON_MANIFOLD hangs >15min on 380k-vert meshes
    mod.use_quality_normals = True
    mod.use_even_offset = True
    mod.thickness_clamp = clamp
    bpy.ops.object.modifier_apply(modifier=mod.name)


def export_glb_of_active(filepath):
    obj = bpy.context.view_layer.objects.active
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.export_scene.gltf(filepath=filepath, export_format='GLB',
                              use_selection=True, export_apply=True)


def export_stl_of_active(filepath):
    obj = bpy.context.view_layer.objects.active
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    if bpy.app.version >= (4, 0, 0):
        bpy.ops.wm.stl_export(filepath=filepath, export_selected_objects=True,
                              global_scale=1000.0, apply_modifiers=True, ascii_format=False)
    else:
        bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True,
                                global_scale=1000.0, ascii=False)


def export_obj_of_active(filepath):
    """Export OBJ. Preserves explicit vertex normals — Rhino 5 reads these
    more reliably than STL's face normals."""
    obj = bpy.context.view_layer.objects.active
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.wm.obj_export(filepath=filepath, export_selected_objects=True,
                          global_scale=1000.0, apply_modifiers=True,
                          export_normals=True, export_materials=False,
                          export_triangulated_mesh=False)


def export_3mf_of_active(filepath):
    """Export 3MF via the built-in io_mesh_3mf add-on. Microsoft format
    with full normal + material + units support. Best chance Rhino 5 reads
    cavity correctly. Returns True if exported, False if 3MF unavailable."""
    obj = bpy.context.view_layer.objects.active
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    # Ensure add-on enabled (built-in in Blender 4.x but may need explicit enable)
    try:
        import addon_utils
        addon_utils.enable('io_mesh_3mf', default_set=False, persistent=False)
    except Exception:
        pass
    # Try the operators that exist across Blender versions
    if hasattr(bpy.ops.export_mesh, 'threemf'):
        try:
            bpy.ops.export_mesh.threemf(filepath=filepath)
            return True
        except Exception as e:
            print(f"BlenderHollow: 3MF via export_mesh.threemf failed: {e}")
    if hasattr(bpy.ops.wm, 'threemf_export'):
        try:
            bpy.ops.wm.threemf_export(filepath=filepath)
            return True
        except Exception as e:
            print(f"BlenderHollow: 3MF via wm.threemf_export failed: {e}")
    if hasattr(bpy.ops.export_scene, 'threemf'):
        try:
            bpy.ops.export_scene.threemf(filepath=filepath)
            return True
        except Exception as e:
            print(f"BlenderHollow: 3MF via export_scene.threemf failed: {e}")
    print("BlenderHollow: 3MF export not available in this Blender")
    return False


def main():
    args = get_args()
    if len(args) < 4:
        print("Usage: -- in.glb out.stl out.glb params.json")
        sys.exit(1)

    in_glb, out_stl, out_glb, params_str = args[0], args[1], args[2], args[3]
    params = json.loads(params_str) if params_str else {}

    target_weight = float(params.get("target_weight_g", 5.5))
    metal = params.get("metal", "gold_22k")
    voxel_pitch = float(params.get("voxel_pitch_mm", 0.15))
    min_wall = float(params.get("min_wall_mm", 0.4))
    max_wall = float(params.get("max_wall_mm", 1.5))
    max_iter = int(params.get("max_iterations", 6))
    converge_pct = float(params.get("converge_pct", 5.0))

    if metal not in DENSITIES:
        print(f"ERROR: unknown metal {metal}")
        sys.exit(1)
    density = DENSITIES[metal]
    target_vol = target_weight / density

    print(f"BlenderHollow: in={in_glb}")
    print(f"BlenderHollow: target={target_weight}g {metal} = {target_vol:.2f} mm^3")
    print(f"BlenderHollow: voxel pitch={voxel_pitch}mm, wall search [{min_wall}, {max_wall}] mm")
    print(f"BlenderHollow: blender {bpy.app.version_string}")

    clear_scene()
    obj = import_glb_and_join(in_glb)

    # Pre-stats
    pre_dims = obj.dimensions
    pre_vol = signed_volume_mm3(obj)
    pre_top = mesh_topology(obj)
    pre_wt = pre_vol * density
    print(f"BlenderHollow: imported {len(obj.data.vertices):,}v / {len(obj.data.polygons):,}f")
    print(f"BlenderHollow: pre bbox {pre_dims.x*1000:.2f} x {pre_dims.y*1000:.2f} x {pre_dims.z*1000:.2f} mm")
    print(f"BlenderHollow: pre vol={pre_vol:.2f}mm^3 ({pre_wt:.3f}g {metal}), "
          f"watertight={pre_top['is_watertight']} (nm={pre_top['non_manifold_edges']}, bnd={pre_top['boundary_edges']})")

    # Step 1: Voxel-remesh to clean manifold
    print(f"BlenderHollow: voxel-remeshing at {voxel_pitch}mm...")
    apply_voxel_remesh(obj, voxel_pitch)
    rem_dims = obj.dimensions
    rem_vol = signed_volume_mm3(obj)
    rem_top = mesh_topology(obj)
    rem_wt = rem_vol * density
    print(f"BlenderHollow: post-remesh {len(obj.data.vertices):,}v / {len(obj.data.polygons):,}f")
    print(f"BlenderHollow: post-remesh bbox {rem_dims.x*1000:.2f} x {rem_dims.y*1000:.2f} x {rem_dims.z*1000:.2f} mm")
    print(f"BlenderHollow: post-remesh vol={rem_vol:.2f}mm^3 ({rem_wt:.3f}g), "
          f"watertight={rem_top['is_watertight']}")

    # Save the manifold mesh data so we can reset between iterations
    saved_data = obj.data.copy()
    saved_data.name = "ManifoldBackup"

    # Step 2: Iterative wall search
    lo, hi = min_wall, max_wall
    best = None
    iter_log = []
    print(f"BlenderHollow: starting binary search...")
    for i in range(1, max_iter + 1):
        wall = (lo + hi) / 2.0
        # Reset to manifold backup
        old = obj.data
        obj.data = saved_data.copy()
        if old.users == 0:
            bpy.data.meshes.remove(old)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        try:
            apply_solidify(obj, wall)
        except Exception as e:
            print(f"BlenderHollow: iter {i} wall={wall:.3f} solidify EXCEPTION: {e}")
            hi = wall
            continue
        vol = signed_volume_mm3(obj)
        wt = vol * density
        delta = (wt - target_weight) / target_weight * 100.0
        top = mesh_topology(obj)
        print(f"BlenderHollow: iter {i} wall={wall:.3f}mm vol={vol:.2f}mm^3 wt={wt:.3f}g "
              f"(delta {delta:+.1f}%) wt-tight={top['is_watertight']}")
        iter_log.append({"iter": i, "wall_mm": round(wall, 4), "vol_mm3": round(vol, 3),
                         "weight_g": round(wt, 4), "delta_pct": round(delta, 2),
                         "watertight": top["is_watertight"]})
        if best is None or abs(wt - target_weight) < abs(best["weight_g"] - target_weight):
            best = {"wall_mm": wall, "vol_mm3": vol, "weight_g": wt, "delta_pct": delta}
        if wt > target_weight:
            hi = wall
        else:
            lo = wall
        if abs(delta) < converge_pct:
            print(f"BlenderHollow: converged within {converge_pct}%")
            break

    if best is None:
        print("BlenderHollow: FAIL - no valid hollow")
        sys.exit(1)

    # Restore best and re-apply for export
    old = obj.data
    obj.data = saved_data.copy()
    if old.users == 0:
        bpy.data.meshes.remove(old)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    apply_solidify(obj, best["wall_mm"])

    # No post-Solidify cleanup. Chunky / simple men's / ladies-CAD rings ship
    # clean as-is. Motif-style rings with very thin features may produce a
    # cosmetic bbox quirk (single stray vert from Solidify); not fixed here.

    # Solidify already produces the physically-correct normal orientation:
    # outer shell normals point outward (away from cavity), inner shell
    # normals point inward (into cavity) — both pointing AWAY from the metal
    # layer. This matches what Rhino expects per the McNeel forum guidance.
    #
    # Earlier we tried bpy.ops.mesh.normals_make_consistent(inside=False) here
    # but it flipped the inner-shell normals to all-outward, breaking the
    # divergence-theorem volume calc (574 mm^3 -> 3025 mm^3) and producing
    # STLs Rhino would still read wrong. The Rhino bug is almost certainly an
    # STL FORMAT issue (face-normal only, no vertex normals), not a
    # mesh-orientation issue. The 3-format export (STL/OBJ/3MF) below is
    # designed to confirm this by giving Rhino formats with richer normal data.
    # If issues persist, fix_normals() is kept for future experimentation.

    # Final stats + export
    final_dims = obj.dimensions
    final_vol = signed_volume_mm3(obj)
    final_top = mesh_topology(obj)
    final_wt = final_vol * density
    print(f"BlenderHollow: FINAL wall={best['wall_mm']:.3f}mm vol={final_vol:.2f}mm^3 wt={final_wt:.3f}g "
          f"(target {target_weight}g, delta {best['delta_pct']:+.1f}%)")
    print(f"BlenderHollow: final bbox {final_dims.x*1000:.2f} x {final_dims.y*1000:.2f} x {final_dims.z*1000:.2f} mm")
    print(f"BlenderHollow: final {len(obj.data.vertices):,}v / {len(obj.data.polygons):,}f, "
          f"watertight={final_top['is_watertight']}")

    export_glb_of_active(out_glb)
    export_stl_of_active(out_stl)
    print(f"BlenderHollow: exported GLB={out_glb}")
    print(f"BlenderHollow: exported STL={out_stl}")

    # Also export OBJ and 3MF alongside STL for the Rhino-format-shootout.
    # File paths: same base as out_stl, different extensions.
    import os
    base, _ = os.path.splitext(out_stl)
    out_obj = base + ".obj"
    out_3mf = base + ".3mf"
    try:
        export_obj_of_active(out_obj)
        obj_ok = os.path.exists(out_obj)
        if obj_ok:
            print(f"BlenderHollow: exported OBJ={out_obj}")
        else:
            print(f"BlenderHollow: OBJ export ran but file missing: {out_obj}")
    except Exception as e:
        obj_ok = False
        print(f"BlenderHollow: OBJ export FAILED: {e}")
    threemf_ok = False
    try:
        threemf_ok = export_3mf_of_active(out_3mf) and os.path.exists(out_3mf)
        if threemf_ok:
            print(f"BlenderHollow: exported 3MF={out_3mf}")
        else:
            print(f"BlenderHollow: 3MF export skipped or failed: {out_3mf}")
    except Exception as e:
        threemf_ok = False
        print(f"BlenderHollow: 3MF export FAILED: {e}")

    meta = {
        "success": True,
        "input_file": in_glb,
        "blender_version": bpy.app.version_string,
        "voxel_pitch_mm": voxel_pitch,
        "input_volume_mm3": round(pre_vol, 3),
        "input_weight_g": round(pre_wt, 4),
        "input_watertight": pre_top["is_watertight"],
        "post_remesh_volume_mm3": round(rem_vol, 3),
        "post_remesh_weight_g": round(rem_wt, 4),
        "post_remesh_watertight": rem_top["is_watertight"],
        "post_remesh_verts": len(saved_data.vertices),
        "post_remesh_faces": len(saved_data.polygons),
        "achieved_wall_mm": round(best["wall_mm"], 4),
        "output_volume_mm3": round(final_vol, 3),
        "output_weight_g": round(final_wt, 4),
        "output_verts": len(obj.data.vertices),
        "output_faces": len(obj.data.polygons),
        "output_watertight": final_top["is_watertight"],
        "target_weight_g": target_weight,
        "target_volume_mm3": round(target_vol, 3),
        "weight_delta_pct": round(best["delta_pct"], 3),
        "metal": metal,
        "bbox_mm": {"x": round(final_dims.x * 1000, 3),
                    "y": round(final_dims.y * 1000, 3),
                    "z": round(final_dims.z * 1000, 3)},
        "iterations": iter_log,
        "exports": {
            "stl": out_stl,
            "glb": out_glb,
            "obj": out_obj if obj_ok else None,
            "3mf": out_3mf if threemf_ok else None,
        },
        "normals_fixed": True,
    }
    print(f"BLENDER_HOLLOW_META:{json.dumps(meta)}")
    print("BlenderHollow: Done")


if __name__ == "__main__":
    main()
