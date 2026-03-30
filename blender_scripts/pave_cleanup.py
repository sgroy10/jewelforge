"""
Blender headless pave stone cleanup for JewelForge.
Called: blender --background --python pave_cleanup.py -- input.glb output.stl output.glb [params_json]

Detects pave stone bumps on AI-generated mesh, cuts clean hemispherical
stone seats, and optionally adds prong geometry. This is the "secret sauce"
that turns blobby AI pave into production-ready jewelry mesh.

params_json: {
    "min_stone_radius": 0.3,   # mm — minimum stone size to detect
    "max_stone_radius": 1.5,   # mm — maximum stone size
    "seat_depth": 0.6,         # fraction of stone radius for seat depth
    "detection_threshold": 0.15 # how much a vertex must protrude to be a peak
}
"""

import sys
import json
import math
import bpy
import bmesh
from mathutils import Vector, kdtree
from collections import defaultdict


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


# ──────────────────────────────────────────────
# Step 1: Detect stone bump positions
# ──────────────────────────────────────────────

def compute_vertex_protrusion(obj):
    """For each vertex, compute how much it protrudes above its local neighborhood.

    Returns list of (vertex_index, protrusion_amount, position, normal).
    Protrusion = distance from vertex to average of connected neighbors,
    projected onto the vertex normal direction.
    """
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    protrusions = []

    for vert in bm.verts:
        if not vert.link_edges:
            continue

        # Get connected neighbor positions
        neighbors = [e.other_vert(vert).co.copy() for e in vert.link_edges]
        if len(neighbors) < 3:
            continue

        # Average neighbor position
        avg = Vector((0, 0, 0))
        for n in neighbors:
            avg += n
        avg /= len(neighbors)

        # Protrusion = how far this vertex is above the neighbor average
        # along the vertex normal direction
        normal = vert.normal.copy()
        if normal.length < 0.001:
            continue

        diff = vert.co - avg
        protrusion = diff.dot(normal)

        protrusions.append((
            vert.index,
            protrusion,
            (obj.matrix_world @ vert.co).copy(),  # world position
            (obj.matrix_world.to_3x3() @ normal).normalized(),  # world normal
        ))

    bm.free()
    return protrusions


def find_bump_peaks(protrusions, threshold=0.15):
    """Find vertices that protrude significantly — these are stone bump peaks.

    threshold is in object-space units (after scaling, this is mm).
    """
    peaks = []
    for idx, protrusion, pos, normal in protrusions:
        if protrusion > threshold:
            peaks.append({
                "vertex_index": idx,
                "protrusion": protrusion,
                "position": pos,
                "normal": normal,
            })

    peaks.sort(key=lambda p: p["protrusion"], reverse=True)
    print(f"JewelForge: Found {len(peaks)} vertices above threshold {threshold}")
    return peaks


def cluster_peaks(peaks, min_distance=0.4):
    """Cluster nearby peaks into individual stone positions.

    Peaks within min_distance of each other belong to the same stone.
    Returns list of {"center": Vector, "normal": Vector, "radius": float, "peak_count": int}
    """
    if not peaks:
        return []

    # Build KD tree for spatial lookup
    kd = kdtree.KDTree(len(peaks))
    for i, peak in enumerate(peaks):
        kd.insert(peak["position"], i)
    kd.balance()

    used = set()
    stones = []

    for i, peak in enumerate(peaks):
        if i in used:
            continue

        # Find all peaks within min_distance
        cluster_indices = []
        for (co, idx, dist) in kd.find_range(peak["position"], min_distance):
            if idx not in used:
                cluster_indices.append(idx)
                used.add(idx)

        if not cluster_indices:
            continue

        # Compute cluster center, average normal, and estimated radius
        center = Vector((0, 0, 0))
        normal = Vector((0, 0, 0))
        max_dist_from_center = 0

        for idx in cluster_indices:
            center += peaks[idx]["position"]
            normal += peaks[idx]["normal"]
        center /= len(cluster_indices)
        normal = normal.normalized() if normal.length > 0.001 else Vector((0, 0, 1))

        # Radius = max distance from center to any cluster member
        for idx in cluster_indices:
            d = (peaks[idx]["position"] - center).length
            max_dist_from_center = max(max_dist_from_center, d)

        # Estimated stone radius is slightly larger than the bump extent
        estimated_radius = max(max_dist_from_center * 1.3, min_distance * 0.4)

        stones.append({
            "center": center,
            "normal": normal,
            "radius": estimated_radius,
            "peak_count": len(cluster_indices),
            "max_protrusion": max(peaks[idx]["protrusion"] for idx in cluster_indices),
        })

    print(f"JewelForge: Clustered into {len(stones)} stone positions")
    return stones


def filter_stones(stones, min_radius=0.3, max_radius=1.5):
    """Filter out noise — keep only stones within expected size range (mm)."""
    filtered = [s for s in stones if min_radius <= s["radius"] <= max_radius]
    removed = len(stones) - len(filtered)
    if removed > 0:
        print(f"JewelForge: Filtered out {removed} noise clusters (outside {min_radius}-{max_radius}mm)")
    print(f"JewelForge: {len(filtered)} valid stone positions detected")
    return filtered


# ──────────────────────────────────────────────
# Step 2: Cut stone seats using boolean operations
# ──────────────────────────────────────────────

def create_stone_cutter(center, normal, radius, depth_fraction=0.6):
    """Create a hemisphere cutter mesh at the given position.

    The cutter is oriented along the normal direction and sized to
    create a clean stone seat.
    """
    # Create UV sphere for the cutter
    bpy.ops.mesh.primitive_uv_sphere_add(
        radius=radius,
        segments=16,
        ring_count=8,
        location=center,
    )
    cutter = bpy.context.active_object
    cutter.name = "StoneCutter"

    # Orient the cutter along the surface normal
    # Default sphere is along Z — rotate to align with normal
    z_axis = Vector((0, 0, 1))
    if normal.length > 0.001 and (normal - z_axis).length > 0.001:
        rotation = z_axis.rotation_difference(normal)
        cutter.rotation_euler = rotation.to_euler()

    # Cut the sphere in half — keep only the part that digs into the surface
    # Move it so only the bottom hemisphere intersects the mesh
    offset = normal * (radius * (1.0 - depth_fraction))
    cutter.location = center + offset

    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)

    return cutter


def boolean_subtract(target_obj, cutter_obj):
    """Boolean subtract cutter from target."""
    bpy.context.view_layer.objects.active = target_obj
    target_obj.select_set(True)

    mod = target_obj.modifiers.new(name="Boolean", type='BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.object = cutter_obj
    mod.solver = 'FAST'  # FAST is more reliable for many small cuts

    try:
        bpy.ops.object.modifier_apply(modifier=mod.name)
        return True
    except Exception as e:
        print(f"JewelForge: Boolean failed for one stone: {e}")
        # Remove the failed modifier
        if mod.name in [m.name for m in target_obj.modifiers]:
            target_obj.modifiers.remove(mod)
        return False


def cut_all_stone_seats(obj, stones, depth_fraction=0.6):
    """Cut clean stone seats for all detected positions."""
    success_count = 0
    fail_count = 0

    for i, stone in enumerate(stones):
        if (i + 1) % 10 == 0 or i == 0:
            print(f"JewelForge: Cutting stone seat {i+1}/{len(stones)}...")

        # Create cutter
        cutter = create_stone_cutter(
            stone["center"],
            stone["normal"],
            stone["radius"],
            depth_fraction,
        )

        # Boolean subtract
        ok = boolean_subtract(obj, cutter)
        if ok:
            success_count += 1
        else:
            fail_count += 1

        # Delete the cutter object
        bpy.data.objects.remove(cutter, do_unlink=True)

    print(f"JewelForge: Stone seats cut — {success_count} success, {fail_count} failed")
    return success_count


# ──────────────────────────────────────────────
# Step 3: Post-boolean cleanup
# ──────────────────────────────────────────────

def post_boolean_cleanup(obj):
    """Clean up mesh after boolean operations."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')

    # Remove doubles created by boolean
    bpy.ops.mesh.remove_doubles(threshold=0.001)

    # Remove loose geometry
    bpy.ops.mesh.delete_loose(use_verts=True, use_edges=True, use_faces=False)

    # Fix normals
    bpy.ops.mesh.normals_make_consistent(inside=False)

    bpy.ops.object.mode_set(mode='OBJECT')
    print("JewelForge: Post-boolean cleanup done")


def sharpen_seat_edges(obj, angle_threshold_deg=25):
    """Mark sharp edges — especially around the boolean-cut stone seats.

    Lower angle threshold than normal (25° vs 35°) to catch the
    seat edges which are the sharpest features.
    """
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

    # Auto-smooth
    try:
        mesh.use_auto_smooth = True
        mesh.auto_smooth_angle = angle_threshold
    except AttributeError:
        pass  # Blender version difference

    # Smooth shade with sharp edges preserved
    bpy.ops.object.shade_smooth()

    print(f"JewelForge: Marked {sharp_count} sharp edges (threshold={angle_threshold_deg}°)")
    return sharp_count


# ──────────────────────────────────────────────
# Stats & Export
# ──────────────────────────────────────────────

def get_mesh_stats(obj):
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)

    non_manifold = sum(1 for e in bm.edges if not e.is_manifold)
    boundary = sum(1 for e in bm.edges if e.is_boundary)
    is_manifold = non_manifold == 0
    is_watertight = is_manifold and boundary == 0

    dims = obj.dimensions
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
    }
    bm.free()
    return stats


def export_stl(filepath):
    bpy.ops.export_mesh.stl(
        filepath=filepath,
        use_selection=True,
        global_scale=1.0,  # already in mm
        ascii=False,
    )


def export_glb(filepath):
    bpy.ops.export_scene.gltf(
        filepath=filepath,
        export_format='GLB',
        use_selection=True,
        export_apply=True,
    )


# ──────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────

def main():
    args = get_args()
    if len(args) < 3:
        print("Usage: blender --background --python pave_cleanup.py -- input.glb output.stl output.glb [params_json]")
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

    min_stone_radius = params.get("min_stone_radius", 0.3)
    max_stone_radius = params.get("max_stone_radius", 1.5)
    seat_depth = params.get("seat_depth", 0.6)
    detection_threshold = params.get("detection_threshold", 0.15)
    cluster_distance = params.get("cluster_distance", 0.5)

    print(f"JewelForge: Pave cleanup — input={input_path}")
    print(f"JewelForge: Params — min_r={min_stone_radius}, max_r={max_stone_radius}, "
          f"depth={seat_depth}, threshold={detection_threshold}, cluster_dist={cluster_distance}")

    # Import
    clear_scene()
    mesh_objects = import_glb(input_path)
    if not mesh_objects:
        print("JewelForge: ERROR — No mesh found!")
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

    # Initial stats
    initial_stats = get_mesh_stats(obj)
    print(f"JewelForge: Input — {initial_stats['vertices']} verts, {initial_stats['faces']} faces")
    print(f"JewelForge: Dimensions — {obj.dimensions.x:.2f} x {obj.dimensions.y:.2f} x {obj.dimensions.z:.2f} mm")

    # Step 1: Detect stone bumps
    print("\n=== STEP 1: Detecting stone bumps ===")
    protrusions = compute_vertex_protrusion(obj)
    peaks = find_bump_peaks(protrusions, threshold=detection_threshold)
    stones = cluster_peaks(peaks, min_distance=cluster_distance)
    stones = filter_stones(stones, min_radius=min_stone_radius, max_radius=max_stone_radius)

    if len(stones) == 0:
        print("JewelForge: No stone positions detected — skipping boolean cuts")
        print("JewelForge: This may mean detection_threshold is too high, or the mesh doesn't have pave")
    else:
        # Step 2: Cut stone seats
        print(f"\n=== STEP 2: Cutting {len(stones)} stone seats ===")
        cut_count = cut_all_stone_seats(obj, stones, depth_fraction=seat_depth)
        print(f"JewelForge: Cut {cut_count} stone seats")

        # Step 3: Post-boolean cleanup
        print("\n=== STEP 3: Post-boolean cleanup ===")
        post_boolean_cleanup(obj)

    # Step 4: Sharpen edges
    print("\n=== STEP 4: Edge sharpening ===")
    sharpen_seat_edges(obj, angle_threshold_deg=25)

    # Final stats
    final_stats = get_mesh_stats(obj)
    print(f"\nJewelForge: Output — {final_stats['vertices']} verts, {final_stats['faces']} faces")
    print(f"JewelForge: Manifold={final_stats['is_manifold']}, Watertight={final_stats['is_watertight']}")

    # Export
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)

    export_stl(output_stl)
    print(f"JewelForge: STL exported to {output_stl}")

    export_glb(output_glb)
    print(f"JewelForge: GLB exported to {output_glb}")

    # Stats JSON
    stats = {
        "input_vertices": initial_stats["vertices"],
        "input_faces": initial_stats["faces"],
        "output_vertices": final_stats["vertices"],
        "output_faces": final_stats["faces"],
        "is_manifold": final_stats["is_manifold"],
        "is_watertight": final_stats["is_watertight"],
        "bounding_box_mm": final_stats["bounding_box_mm"],
        "stones_detected": len(stones),
        "stones_cut": cut_count if len(stones) > 0 else 0,
        "pave_cleanup": True,
    }
    print(f"JEWELFORGE_STATS:{json.dumps(stats)}")
    print("JewelForge: Pave cleanup done!")


if __name__ == "__main__":
    main()
