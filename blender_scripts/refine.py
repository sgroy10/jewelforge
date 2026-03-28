"""
Blender headless mesh refinement for JewelForge.
Called: blender --background --python refine.py -- input.glb output.stl output.glb

Focus: Sharp edges on settings/prongs, clean topology, jewelry-ready output.
"""

import sys
import json
import math
import bpy
import bmesh
from mathutils import Vector


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
            "x": round(dims.x * 1000, 2),
            "y": round(dims.y * 1000, 2),
            "z": round(dims.z * 1000, 2),
        },
        "volume_mm3": round(volume * 1e9, 2),
    }
    bm.free()
    return stats


def light_cleanup(obj):
    """Light cleanup — fix minor issues without destroying mesh."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')

    # Merge very close vertices only
    bpy.ops.mesh.remove_doubles(threshold=0.00001)

    # Remove loose geometry
    bpy.ops.mesh.delete_loose(use_verts=True, use_edges=True, use_faces=False)

    # Fix normals
    bpy.ops.mesh.normals_make_consistent(inside=False)

    bpy.ops.object.mode_set(mode='OBJECT')


def sharpen_edges(obj, angle_threshold_deg=35):
    """Mark sharp edges based on face angle — critical for prongs, settings, pave.

    Edges where adjacent faces meet at a sharp angle (> threshold) get marked as
    sharp/creased. This preserves the crisp look of prong tips, channel walls, and
    bezel edges that AI models tend to smooth out.
    """
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    angle_threshold = math.radians(angle_threshold_deg)
    sharp_count = 0

    # Get or create crease layer
    crease_layer = bm.edges.layers.crease.verify()

    for edge in bm.edges:
        if len(edge.link_faces) == 2:
            face_angle = edge.calc_face_angle(0)
            if face_angle > angle_threshold:
                edge.smooth = False  # Mark as sharp
                edge[crease_layer] = 1.0  # Full crease
                sharp_count += 1

    bm.to_mesh(mesh)
    bm.free()

    # Enable auto-smooth with custom normals
    mesh.use_auto_smooth = True
    mesh.auto_smooth_angle = angle_threshold

    # Add custom split normals for sharp display
    if sharp_count > 0:
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.mesh.edges_select_sharp(sharpness=angle_threshold)
        bpy.ops.object.mode_set(mode='OBJECT')

    print(f"JewelForge: Marked {sharp_count} sharp edges (threshold={angle_threshold_deg}°)")
    return sharp_count


def smooth_surface(obj):
    """Apply smooth shading with sharp edges preserved.

    Smooth shading + auto-smooth + sharp edge marks =
    smooth bands/shanks but crisp prong tips and settings.
    """
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    # Smooth shade the whole object
    bpy.ops.object.shade_smooth()

    # The sharp marks from sharpen_edges() will override smooth on those edges
    print("JewelForge: Applied smooth shading with sharp edge preservation")


def decimate_if_needed(obj, target_faces=200000):
    """Only decimate if mesh is very high poly. Preserves shape."""
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


def apply_weighted_normals(obj):
    """Apply weighted normals modifier for better shading on curved surfaces.

    This makes curved surfaces (ring bands, bezels) look smoother while
    sharp edges stay sharp.
    """
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    try:
        mod = obj.modifiers.new(name="WeightedNormal", type='WEIGHTED_NORMAL')
        mod.weight = 50
        mod.mode = 'FACE_AREA'
        mod.keep_sharp = True  # Respect our sharp edge marks
        bpy.ops.object.modifier_apply(modifier=mod.name)
        print("JewelForge: Applied weighted normals")
    except Exception as e:
        print(f"JewelForge: Weighted normals skipped — {e}")


def apply_gold_material(obj):
    """Apply 18K gold PBR material."""
    mat = bpy.data.materials.new(name="18K_Gold")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.831, 0.686, 0.216, 1.0)
        bsdf.inputs["Metallic"].default_value = 0.95
        bsdf.inputs["Roughness"].default_value = 0.25
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
        global_scale=1000.0,  # meters -> mm
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
        print("Usage: blender --background --python refine.py -- input.glb output.stl output.glb")
        sys.exit(1)

    input_path, output_stl, output_glb = args[0], args[1], args[2]
    print(f"JewelForge: Processing {input_path}")

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
    print(f"JewelForge: Dimensions — {obj.dimensions.x:.4f} x {obj.dimensions.y:.4f} x {obj.dimensions.z:.4f} m")

    # Step 1: Light cleanup
    light_cleanup(obj)

    # Step 2: Mark sharp edges — this is what makes prongs/settings look crisp
    sharpen_edges(obj, angle_threshold_deg=35)

    # Step 3: Smooth shading with sharp edge preservation
    smooth_surface(obj)

    # Step 4: Weighted normals for better curved surface rendering
    apply_weighted_normals(obj)

    # Step 5: Decimate only if over 200K faces
    decimate_if_needed(obj, target_faces=200000)

    # Step 6: Get final stats
    final_stats = get_mesh_stats(obj)
    print(f"JewelForge: Output — {final_stats['vertices']} verts, {final_stats['faces']} faces")

    # Step 7: Export STL (in mm)
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    export_stl(output_stl)
    print(f"JewelForge: STL exported to {output_stl}")

    # Step 8: Apply gold material and export GLB
    apply_gold_material(obj)
    export_glb(output_glb)
    print(f"JewelForge: GLB exported to {output_glb}")

    # Output stats
    stats = {
        "input_vertices": initial_stats["vertices"],
        "input_faces": initial_stats["faces"],
        "output_vertices": final_stats["vertices"],
        "output_faces": final_stats["faces"],
        "is_manifold": final_stats["is_manifold"],
        "is_watertight": final_stats["is_watertight"],
        "bounding_box_mm": final_stats["bounding_box_mm"],
        "volume_mm3": final_stats["volume_mm3"],
    }
    print(f"JEWELFORGE_STATS:{json.dumps(stats)}")
    print("JewelForge: Done!")


if __name__ == "__main__":
    main()
