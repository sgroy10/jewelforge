"""
Blender headless mesh refinement for JewelForge.
Called: blender --background --python refine.py -- input.glb output.stl output.glb jewelry_type target_mm

Steps: import → join → scale to real mm → center → clean → remesh → subdivide →
       sharp edges → weighted normals → decimate → gold material → export
"""

import sys
import json
import math
import bpy
import bmesh
from mathutils import Vector


# US ring size → inner diameter in mm
RING_SIZE_MM = {
    3: 14.05, 3.5: 14.45, 4: 14.86, 4.5: 15.27, 5: 15.7,
    5.5: 16.10, 6: 16.45, 6.5: 16.92, 7: 17.35, 7.5: 17.75,
    8: 18.19, 8.5: 18.53, 9: 19.41, 9.5: 19.62, 10: 19.84,
    10.5: 20.20, 11: 20.68, 11.5: 21.08, 12: 21.49,
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
            "x": round(dims.x, 2),
            "y": round(dims.y, 2),
            "z": round(dims.z, 2),
        },
        "volume_mm3": round(volume, 2),
    }
    bm.free()
    return stats


def scale_to_real_size(obj, jewelry_type, target_mm):
    """Scale the mesh to real-world mm dimensions.

    AI models output at arbitrary scale (often ~1400mm bounding box).
    We scale to actual jewelry dimensions.
    """
    dims = obj.dimensions
    dx, dy, dz = dims.x, dims.y, dims.z
    print(f"JewelForge: Raw dimensions: {dx:.2f} x {dy:.2f} x {dz:.2f}")

    if jewelry_type == "ring":
        # Ring: shortest bbox axis = finger hole axis (the thin dimension)
        # Scale so that dimension matches the target inner diameter
        shortest = min(dx, dy, dz)
        if shortest <= 0:
            print("JewelForge: WARNING — degenerate mesh, skipping scale")
            return
        scale_factor = target_mm / shortest
        print(f"JewelForge: Ring scaling — shortest axis={shortest:.2f}, "
              f"target={target_mm:.2f}mm, factor={scale_factor:.4f}")
    else:
        # Pendant/earring/figurine: scale by tallest dimension to target height
        tallest = max(dx, dy, dz)
        if tallest <= 0:
            print("JewelForge: WARNING — degenerate mesh, skipping scale")
            return
        scale_factor = target_mm / tallest
        print(f"JewelForge: {jewelry_type} scaling — tallest axis={tallest:.2f}, "
              f"target={target_mm:.2f}mm, factor={scale_factor:.4f}")

    obj.scale *= scale_factor
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    new_dims = obj.dimensions
    print(f"JewelForge: Scaled dimensions: {new_dims.x:.2f} x {new_dims.y:.2f} x {new_dims.z:.2f} mm")


def center_origin(obj):
    """Center origin to geometry and move to world origin."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='BOUNDS')
    obj.location = (0, 0, 0)


def clean_mesh(obj):
    """Remove doubles, fill holes, recalculate normals."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')

    # Remove doubles
    bpy.ops.mesh.remove_doubles(threshold=0.001)

    # Fill holes
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.mesh.select_non_manifold()
    bpy.ops.mesh.fill_holes(sides=64)

    # Recalculate normals
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.normals_make_consistent(inside=False)

    bpy.ops.object.mode_set(mode='OBJECT')
    print("JewelForge: Mesh cleaned — doubles removed, holes filled, normals fixed")


def voxel_remesh(obj, voxel_size=0.15):
    """Voxel remesh for uniform topology."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    obj.data.remesh_voxel_size = voxel_size
    bpy.ops.object.voxel_remesh()
    print(f"JewelForge: Voxel remesh done (size={voxel_size}), faces={len(obj.data.polygons)}")


def subdivide(obj, levels=1):
    """Subdivision surface for smoother output."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    mod = obj.modifiers.new(name="Subdivision", type='SUBSURF')
    mod.levels = levels
    mod.render_levels = levels
    bpy.ops.object.modifier_apply(modifier=mod.name)
    print(f"JewelForge: Subdivided (levels={levels}), faces={len(obj.data.polygons)}")


def sharpen_edges(obj, angle_threshold_deg=30):
    """Mark sharp edges where face angle > threshold."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.edges.ensure_lookup_table()

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

    print(f"JewelForge: Marked {sharp_count} sharp edges (threshold={angle_threshold_deg}°)")
    return sharp_count


def apply_weighted_normals(obj):
    """Weighted normals modifier with keep_sharp."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        mod = obj.modifiers.new(name="WeightedNormal", type='WEIGHTED_NORMAL')
        mod.weight = 50
        mod.mode = 'FACE_AREA'
        mod.keep_sharp = True
        bpy.ops.object.modifier_apply(modifier=mod.name)
        print("JewelForge: Applied weighted normals (keep_sharp=True)")
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


def apply_gold_material(obj):
    """Apply 18K gold PBR material."""
    mat = bpy.data.materials.new(name="18K_Gold")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.831, 0.686, 0.216, 1.0)
        bsdf.inputs["Metallic"].default_value = 1.0
        bsdf.inputs["Roughness"].default_value = 0.15
        try:
            bsdf.inputs["Specular IOR Level"].default_value = 0.8
        except KeyError:
            try:
                bsdf.inputs["Specular"].default_value = 0.8
            except KeyError:
                pass

    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)


def export_stl(filepath):
    bpy.ops.export_mesh.stl(
        filepath=filepath,
        use_selection=True,
        global_scale=1.0,  # already in mm from scaling step
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
    if len(args) < 5:
        print("Usage: blender --background --python refine.py -- input.glb output.stl output.glb jewelry_type target_mm")
        sys.exit(1)

    input_path = args[0]
    output_stl = args[1]
    output_glb = args[2]
    jewelry_type = args[3]        # ring, pendant, earring, figurine
    target_mm = float(args[4])    # target dimension in mm

    print(f"JewelForge: Processing {input_path} — type={jewelry_type}, target={target_mm}mm")

    # Clear and import
    clear_scene()
    mesh_objects = import_glb(input_path)

    if not mesh_objects:
        print("JewelForge: ERROR — No mesh found in GLB!")
        sys.exit(1)

    # Join all meshes into one
    if len(mesh_objects) > 1:
        bpy.context.view_layer.objects.active = mesh_objects[0]
        for obj in mesh_objects:
            obj.select_set(True)
        bpy.ops.object.join()
        mesh_objects = [bpy.context.active_object]

    obj = mesh_objects[0]
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    # Raw stats
    raw_dims = obj.dimensions
    print(f"JewelForge: Raw bbox: {raw_dims.x:.2f} x {raw_dims.y:.2f} x {raw_dims.z:.2f}")

    # Step 1: Scale to real-world mm
    scale_to_real_size(obj, jewelry_type, target_mm)

    # Step 2: Center origin
    center_origin(obj)

    # Step 3: Light cleanup — fix doubles, holes, normals
    clean_mesh(obj)

    # NO voxel remesh — it destroys prong tips and stone seat detail
    # NO subdivision — Hitem3D 2M faces already has enough detail

    # Step 4: Sharp edges — preserve prong/seat crispness
    sharpen_edges(obj, angle_threshold_deg=30)

    # Step 5: Weighted normals — smooth curves, sharp edges
    apply_weighted_normals(obj)

    # Step 6: Decimate to manageable size (shape-preserving collapse)
    decimate_if_needed(obj, target_faces=50000)

    # Final stats
    final_stats = get_mesh_stats(obj)
    print(f"JewelForge: Output — {final_stats['vertices']} verts, {final_stats['faces']} faces, "
          f"manifold={final_stats['is_manifold']}, watertight={final_stats['is_watertight']}")

    # Export STL (in mm)
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    export_stl(output_stl)
    print(f"JewelForge: STL exported to {output_stl}")

    # Apply gold material and export GLB
    apply_gold_material(obj)
    export_glb(output_glb)
    print(f"JewelForge: GLB exported to {output_glb}")

    # Output stats as JSON
    stats = {
        "vertices": final_stats["vertices"],
        "faces": final_stats["faces"],
        "is_manifold": final_stats["is_manifold"],
        "is_watertight": final_stats["is_watertight"],
        "non_manifold_edges": final_stats["non_manifold_edges"],
        "bounding_box_mm": final_stats["bounding_box_mm"],
        "volume_mm3": final_stats["volume_mm3"],
    }
    print(f"JEWELFORGE_STATS:{json.dumps(stats)}")
    print("JewelForge: Done!")


if __name__ == "__main__":
    main()
