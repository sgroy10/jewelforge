"""
Blender headless mesh refinement script for JewelForge.
Called via: blender --background --python refine.py -- input.glb output.stl output.glb
"""

import sys
import json
import math
import os

import bpy
import bmesh
from mathutils import Vector


def get_args():
    """Parse arguments after '--' separator."""
    argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1:]
    else:
        args = []
    return args


def clear_scene():
    """Remove all objects from the scene."""
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)


def import_glb(filepath):
    """Import a GLB file."""
    bpy.ops.import_scene.gltf(filepath=filepath)
    # Find the imported mesh object
    mesh_objects = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH']
    if not mesh_objects:
        raise Exception("No mesh found in GLB file")
    return mesh_objects


def get_mesh_stats(obj):
    """Get mesh statistics."""
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)

    # Check manifold
    non_manifold_edges = sum(1 for e in bm.edges if not e.is_manifold)
    is_manifold = non_manifold_edges == 0

    # Check watertight (manifold + no boundary edges)
    boundary_edges = sum(1 for e in bm.edges if e.is_boundary)
    is_watertight = is_manifold and boundary_edges == 0

    # Bounding box
    bbox = obj.bound_box
    dims = obj.dimensions

    # Volume (approximate for watertight meshes)
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
        "edges": len(mesh.edges),
        "is_manifold": is_manifold,
        "is_watertight": is_watertight,
        "non_manifold_edges": non_manifold_edges,
        "boundary_edges": boundary_edges,
        "bounding_box_mm": {
            "x": round(dims.x * 1000, 2),
            "y": round(dims.y * 1000, 2),
            "z": round(dims.z * 1000, 2),
        },
        "volume_mm3": round(volume * 1e9, 2),
    }

    bm.free()
    return stats


def cleanup_mesh(obj):
    """Basic mesh cleanup: remove doubles, recalculate normals, remove loose."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')

    # Remove doubles (merge by distance)
    bpy.ops.mesh.remove_doubles(threshold=0.0001)

    # Delete loose vertices and edges
    bpy.ops.mesh.delete_loose(use_verts=True, use_edges=True, use_faces=False)

    # Recalculate normals outside
    bpy.ops.mesh.normals_make_consistent(inside=False)

    # Fill holes (small ones)
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.mesh.select_non_manifold(extend=False)
    try:
        bpy.ops.mesh.fill_holes(sides=8)
    except Exception:
        pass

    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.normals_make_consistent(inside=False)

    bpy.ops.object.mode_set(mode='OBJECT')


def remesh_voxel(obj, resolution=0.0002):
    """Apply voxel remesh for clean topology."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    # Use Blender's voxel remesh
    mod = obj.modifiers.new(name="Remesh", type='REMESH')
    mod.mode = 'VOXEL'
    mod.voxel_size = resolution
    mod.use_smooth_shade = True

    bpy.ops.object.modifier_apply(modifier=mod.name)


def smooth_mesh(obj, iterations=1, factor=0.3):
    """Apply Laplacian smooth - volume preserving."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    mod = obj.modifiers.new(name="Smooth", type='LAPLACIANSMOOTH')
    mod.iterations = iterations
    mod.lambda_factor = factor
    mod.lambda_border = 0.0  # Don't move boundary verts
    mod.use_volume_preserve = True

    bpy.ops.object.modifier_apply(modifier=mod.name)


def repair_manifold(obj):
    """Make mesh manifold and watertight."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')

    # Select non-manifold
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.mesh.select_non_manifold(extend=False)

    # Try to fill
    try:
        bpy.ops.mesh.fill()
    except Exception:
        pass

    # Fix normals again
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.normals_make_consistent(inside=False)

    # Remove interior faces
    try:
        bpy.ops.mesh.select_interior_faces()
        bpy.ops.mesh.delete(type='FACE')
    except Exception:
        pass

    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.normals_make_consistent(inside=False)

    bpy.ops.object.mode_set(mode='OBJECT')


def apply_gold_material(obj):
    """Apply gold PBR material for GLB export."""
    mat = bpy.data.materials.new(name="18K_Gold")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    bsdf = nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.831, 0.686, 0.216, 1.0)  # #D4AF37
        bsdf.inputs["Metallic"].default_value = 0.95
        bsdf.inputs["Roughness"].default_value = 0.25
        bsdf.inputs["Specular IOR Level"].default_value = 0.8

    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)


def export_stl(filepath):
    """Export as STL in millimeters."""
    bpy.ops.export_mesh.stl(
        filepath=filepath,
        use_selection=True,
        global_scale=1000.0,  # Convert to mm
        ascii=False,
    )


def export_glb(filepath):
    """Export as GLB with materials."""
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

    input_path = args[0]
    output_stl = args[1]
    output_glb = args[2]

    print(f"JewelForge: Processing {input_path}")

    # Clear and import
    clear_scene()
    mesh_objects = import_glb(input_path)

    # Join all mesh objects into one
    if len(mesh_objects) > 1:
        bpy.context.view_layer.objects.active = mesh_objects[0]
        for obj in mesh_objects:
            obj.select_set(True)
        bpy.ops.object.join()
        mesh_objects = [bpy.context.active_object]

    obj = mesh_objects[0]
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    # Get initial stats
    initial_stats = get_mesh_stats(obj)
    print(f"JewelForge: Input - {initial_stats['vertices']} verts, {initial_stats['faces']} faces")

    # Step 1: Cleanup
    cleanup_mesh(obj)

    # Step 2: Voxel remesh — adaptive resolution based on object size
    dims = obj.dimensions
    max_dim = max(dims.x, dims.y, dims.z)
    # Target ~200 voxels along largest dimension
    voxel_size = max(max_dim / 200.0, 0.0001)
    remesh_voxel(obj, resolution=voxel_size)

    # Step 3: Light smooth
    smooth_mesh(obj, iterations=1, factor=0.2)

    # Step 4: Repair
    repair_manifold(obj)

    # Get final stats
    final_stats = get_mesh_stats(obj)

    # Step 5: Export STL
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    export_stl(output_stl)

    # Step 6: Apply gold material and export GLB
    apply_gold_material(obj)
    export_glb(output_glb)

    # Output stats as JSON for server to parse
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
