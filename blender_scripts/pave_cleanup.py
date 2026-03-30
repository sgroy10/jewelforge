"""
Blender headless pave stone cleanup for JewelForge.
Called: blender --background --python pave_cleanup.py -- input.glb output.stl output.glb [params_json]

The "secret sauce": Detects pave stone bumps on AI-generated mesh using
Laplacian curvature analysis, cuts clean conical stone seats via boolean
difference, adds optional bead prongs, and sharpens seat edges.

Pipeline: decimate → detect → cut seats → add prongs → cleanup → sharpen → export
"""

import sys
import json
import math
import bpy
import bmesh
from mathutils import Vector, Quaternion, kdtree


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
    dims = obj.dimensions
    stats = {
        "vertices": len(mesh.vertices),
        "faces": len(mesh.polygons),
        "is_manifold": non_manifold == 0,
        "is_watertight": non_manifold == 0 and boundary == 0,
        "bounding_box_mm": {
            "x": round(dims.x, 2),
            "y": round(dims.y, 2),
            "z": round(dims.z, 2),
        },
    }
    bm.free()
    return stats


# ──────────────────────────────────────────────
# Step 0: Decimate for faster boolean ops
# ──────────────────────────────────────────────

def decimate_for_processing(obj, target_faces=150000):
    """Decimate to a workable face count for boolean operations.
    Boolean on 2M faces = 15+ min. On 150K = 30 seconds.
    """
    face_count = len(obj.data.polygons)
    if face_count <= target_faces:
        print(f"JewelForge: {face_count} faces, no pre-decimation needed")
        return

    ratio = target_faces / face_count
    print(f"JewelForge: Pre-decimating {face_count} → ~{target_faces} for boolean ops")

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    mod = obj.modifiers.new(name="PreDecimate", type='DECIMATE')
    mod.decimate_type = 'COLLAPSE'
    mod.ratio = ratio
    mod.use_collapse_triangulate = True
    bpy.ops.object.modifier_apply(modifier=mod.name)
    print(f"JewelForge: Decimated to {len(obj.data.polygons)} faces")


# ──────────────────────────────────────────────
# Step 1: Detect stone bumps via Laplacian curvature
# ──────────────────────────────────────────────

def detect_stone_bumps(obj, curvature_threshold=0.3, min_stone_radius=0.3,
                       max_stone_radius=1.5, cluster_distance=0.8):
    """Detect pave stone positions using Laplacian curvature analysis.

    For each vertex, computes how much it protrudes outward relative to
    its neighbors (Laplacian). Vertices at the peak of bumps have high
    positive values. Clusters nearby peaks into individual stone positions.

    Returns list of (center: Vector, normal: Vector, radius: float)
    """
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()

    world_matrix = obj.matrix_world

    # Phase 1: Compute Laplacian curvature at each vertex
    curvature = {}
    for vert in bm.verts:
        if not vert.link_edges or not vert.link_faces:
            continue

        neighbors = [e.other_vert(vert) for e in vert.link_edges]
        if len(neighbors) < 3:
            continue

        # Laplacian: average neighbor position - vertex position
        avg = Vector((0, 0, 0))
        for n in neighbors:
            avg += n.co
        avg /= len(neighbors)

        laplacian = avg - vert.co
        normal = vert.normal

        if normal.length < 0.001:
            continue

        # Dot product with normal: negative = vertex sticks OUT (convex bump)
        curv = -laplacian.dot(normal)
        curvature[vert.index] = curv

    # Phase 2: Find local maxima (peaks of each bump)
    candidates = []
    for vert in bm.verts:
        curv = curvature.get(vert.index, 0.0)
        if curv < curvature_threshold:
            continue

        # Check if this vertex is a local maximum in curvature
        neighbors = [e.other_vert(vert) for e in vert.link_edges]
        is_peak = all(
            curvature.get(n.index, 0.0) <= curv
            for n in neighbors
        )
        if is_peak:
            world_pos = world_matrix @ vert.co
            world_normal = (world_matrix.to_3x3() @ vert.normal).normalized()
            candidates.append((world_pos.copy(), world_normal.copy(), curv))

    print(f"JewelForge: Found {len(candidates)} curvature peaks above threshold {curvature_threshold}")

    if not candidates:
        bm.free()
        return []

    # Phase 3: Cluster nearby peaks into individual stones
    kd = kdtree.KDTree(len(candidates))
    for i, (pos, norm, curv) in enumerate(candidates):
        kd.insert(pos, i)
    kd.balance()

    used = set()
    stones = []

    # Process strongest peaks first
    sorted_idx = sorted(range(len(candidates)), key=lambda i: candidates[i][2], reverse=True)

    for idx in sorted_idx:
        if idx in used:
            continue

        pos, normal, curv = candidates[idx]
        nearby = kd.find_range(pos, cluster_distance)
        cluster = [i for _, i, _ in nearby if i not in used]

        if not cluster:
            continue

        # Weighted average position and normal
        total_w = 0.0
        avg_pos = Vector((0, 0, 0))
        avg_norm = Vector((0, 0, 0))
        for ci in cluster:
            w = candidates[ci][2]
            avg_pos += candidates[ci][0] * w
            avg_norm += candidates[ci][1] * w
            total_w += w
            used.add(ci)

        avg_pos /= total_w
        avg_norm = (avg_norm / total_w).normalized()

        # Estimate radius from cluster spread
        if len(cluster) > 1:
            max_dist = max((candidates[ci][0] - avg_pos).length for ci in cluster)
            est_radius = max(min_stone_radius, min(max_dist * 1.2, max_stone_radius))
        else:
            est_radius = min_stone_radius

        stones.append((avg_pos.copy(), avg_norm.copy(), est_radius))

    bm.free()

    # Filter by size
    stones = [(p, n, r) for p, n, r in stones if min_stone_radius <= r <= max_stone_radius]
    print(f"JewelForge: {len(stones)} stone positions detected after clustering")
    return stones


# ──────────────────────────────────────────────
# Step 2: Cut stone seats via boolean difference
# ──────────────────────────────────────────────

def create_conical_cutter(position, normal, radius, seat_depth=None):
    """Create a closed conical cutter mesh for boolean subtraction.

    Built with bmesh for maximum control — no UV sphere artifacts.
    Conical seat is the industry standard for pave stone setting.
    """
    if seat_depth is None:
        seat_depth = radius * 0.7

    bm = bmesh.new()
    segments = 20

    # Top circle (at surface level, slight overshoot above)
    overshoot = radius * 0.15
    top_verts = []
    for i in range(segments):
        angle = 2.0 * math.pi * i / segments
        x = math.cos(angle) * radius
        y = math.sin(angle) * radius
        top_verts.append(bm.verts.new((x, y, overshoot)))

    # Bottom point (culet — tip of the cone)
    bottom_vert = bm.verts.new((0, 0, -seat_depth))

    # Side faces (triangle fan from rim to culet)
    for i in range(segments):
        next_i = (i + 1) % segments
        bm.faces.new([top_verts[i], top_verts[next_i], bottom_vert])

    # Top cap (closes the volume — required for boolean)
    bm.faces.new(list(reversed(top_verts)))

    # Build Blender mesh object
    mesh_data = bpy.data.meshes.new("ConeCutter")
    bm.to_mesh(mesh_data)
    bm.free()

    cutter = bpy.data.objects.new("ConeCutter", mesh_data)
    bpy.context.scene.collection.objects.link(cutter)

    # Orient along surface normal
    up = Vector((0, 0, 1))
    if normal.dot(up) < -0.999:
        quat = Quaternion((0, 1, 0), math.pi)
    elif normal.dot(up) > 0.999:
        quat = Quaternion()
    else:
        axis = up.cross(normal).normalized()
        angle = math.acos(max(-1.0, min(1.0, up.dot(normal))))
        quat = Quaternion(axis, angle)

    cutter.rotation_mode = 'QUATERNION'
    cutter.rotation_quaternion = quat
    cutter.location = position

    # Apply transforms for clean boolean
    bpy.context.view_layer.objects.active = cutter
    cutter.select_set(True)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bpy.ops.object.select_all(action='DESELECT')

    return cutter


def cut_all_seats(target_obj, stones):
    """Boolean-subtract conical seat cutters from the jewelry mesh."""
    face_count = len(target_obj.data.polygons)
    solver = 'FAST' if face_count > 100000 else 'EXACT'
    print(f"JewelForge: Using {solver} boolean solver ({face_count} faces)")

    bpy.context.view_layer.objects.active = target_obj
    success = 0
    failed = 0

    for i, (pos, normal, radius) in enumerate(stones):
        if (i + 1) % 5 == 0 or i == 0:
            print(f"JewelForge: Cutting seat {i+1}/{len(stones)} (r={radius:.2f}mm)")

        cutter = create_conical_cutter(pos, normal, radius)

        # Boolean difference
        mod = target_obj.modifiers.new(name="PaveSeat", type='BOOLEAN')
        mod.operation = 'DIFFERENCE'
        mod.object = cutter
        mod.solver = solver

        try:
            bpy.context.view_layer.objects.active = target_obj
            target_obj.select_set(True)
            bpy.ops.object.modifier_apply(modifier=mod.name)
            success += 1
        except Exception as e:
            print(f"JewelForge: Boolean failed for stone {i}: {e}")
            # Remove failed modifier
            if "PaveSeat" in [m.name for m in target_obj.modifiers]:
                target_obj.modifiers.remove(target_obj.modifiers["PaveSeat"])
            failed += 1

        # Remove cutter
        bpy.data.objects.remove(cutter, do_unlink=True)

    print(f"JewelForge: Seats cut — {success} success, {failed} failed")
    return success


# ──────────────────────────────────────────────
# Step 3: Add bead prongs (optional)
# ──────────────────────────────────────────────

def add_bead_prongs(target_obj, stones, prong_count=4, prong_radius=0.12,
                     prong_height=0.2):
    """Add small bead prongs at the rim of each stone seat.

    Standard pave technique: 4 small spheres around each stone
    that hold it in place. Makes the mesh look production-ready.
    """
    prong_objects = []

    for i, (pos, normal, stone_radius) in enumerate(stones):
        # Build local coordinate frame
        if abs(normal.dot(Vector((1, 0, 0)))) < 0.9:
            tangent = normal.cross(Vector((1, 0, 0))).normalized()
        else:
            tangent = normal.cross(Vector((0, 1, 0))).normalized()
        bitangent = normal.cross(tangent).normalized()

        for p in range(prong_count):
            angle = 2.0 * math.pi * p / prong_count
            # Offset angle by 45° per stone to avoid alignment patterns
            angle += (i * math.pi / 7.0)

            prong_pos = (
                pos
                + tangent * (stone_radius * 0.9 * math.cos(angle))
                + bitangent * (stone_radius * 0.9 * math.sin(angle))
                + normal * prong_height * 0.3
            )

            bpy.ops.mesh.primitive_ico_sphere_add(
                radius=prong_radius,
                subdivisions=2,
                location=prong_pos,
            )
            prong_obj = bpy.context.active_object
            prong_obj.name = f"Prong_{i}_{p}"
            prong_objects.append(prong_obj)

    if not prong_objects:
        return

    # Join all prongs into target mesh
    bpy.ops.object.select_all(action='DESELECT')
    target_obj.select_set(True)
    for obj in prong_objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = target_obj
    bpy.ops.object.join()

    print(f"JewelForge: Added {len(prong_objects)} bead prongs ({prong_count} per stone)")


# ──────────────────────────────────────────────
# Step 4: Post-processing
# ──────────────────────────────────────────────

def post_cleanup(obj):
    """Clean up after boolean operations."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.remove_doubles(threshold=0.005)
    bpy.ops.mesh.delete_loose(use_verts=True, use_edges=True, use_faces=False)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode='OBJECT')
    print("JewelForge: Post-boolean cleanup done")


def sharpen_edges(obj, angle_deg=25):
    """Sharp edges at low threshold to catch seat cut boundaries."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.edges.ensure_lookup_table()

    threshold = math.radians(angle_deg)
    sharp_count = 0
    crease_layer = bm.edges.layers.crease.verify()

    for edge in bm.edges:
        if len(edge.link_faces) == 2:
            if edge.calc_face_angle(0) > threshold:
                edge.smooth = False
                edge[crease_layer] = 1.0
                sharp_count += 1

    bm.to_mesh(mesh)
    bm.free()

    try:
        mesh.use_auto_smooth = True
        mesh.auto_smooth_angle = threshold
    except AttributeError:
        pass

    bpy.ops.object.shade_smooth()
    print(f"JewelForge: Marked {sharp_count} sharp edges (threshold={angle_deg}°)")


def export_stl(filepath):
    bpy.ops.export_mesh.stl(
        filepath=filepath, use_selection=True, global_scale=1.0, ascii=False,
    )


def export_glb(filepath):
    bpy.ops.export_scene.gltf(
        filepath=filepath, export_format='GLB', use_selection=True, export_apply=True,
    )


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    args = get_args()
    if len(args) < 3:
        print("Usage: blender --background --python pave_cleanup.py -- input.glb output.stl output.glb [params_json]")
        sys.exit(1)

    input_path, output_stl, output_glb = args[0], args[1], args[2]

    params = {}
    if len(args) >= 4:
        try:
            params = json.loads(args[3])
        except json.JSONDecodeError:
            pass

    curvature_threshold = params.get("curvature_threshold", 0.3)
    min_stone_radius = params.get("min_stone_radius", 0.3)
    max_stone_radius = params.get("max_stone_radius", 1.5)
    cluster_distance = params.get("cluster_distance", 0.8)
    seat_depth_ratio = params.get("seat_depth_ratio", 0.7)
    add_prongs = params.get("add_prongs", True)
    prong_count = params.get("prong_count", 4)

    print(f"JewelForge: Pave cleanup — {input_path}")
    print(f"JewelForge: Params — curv_thresh={curvature_threshold}, "
          f"stone_r=[{min_stone_radius}-{max_stone_radius}], "
          f"cluster_dist={cluster_distance}, prongs={add_prongs}")

    # Import
    clear_scene()
    mesh_objects = import_glb(input_path)
    if not mesh_objects:
        print("JewelForge: ERROR — No mesh!")
        sys.exit(1)

    if len(mesh_objects) > 1:
        bpy.context.view_layer.objects.active = mesh_objects[0]
        for obj in mesh_objects:
            obj.select_set(True)
        bpy.ops.object.join()

    obj = bpy.context.active_object
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    initial_stats = get_mesh_stats(obj)
    print(f"JewelForge: Input — {initial_stats['vertices']} verts, {initial_stats['faces']} faces")

    # Step 0: Decimate for faster boolean (keep original detail for detection first)
    # Detect on full mesh, then decimate, then cut
    print("\n=== DETECTING STONE POSITIONS ===")
    stones = detect_stone_bumps(
        obj,
        curvature_threshold=curvature_threshold,
        min_stone_radius=min_stone_radius,
        max_stone_radius=max_stone_radius,
        cluster_distance=cluster_distance,
    )

    stones_detected = len(stones)

    if stones_detected == 0:
        print("JewelForge: No stones detected — trying lower threshold")
        stones = detect_stone_bumps(
            obj,
            curvature_threshold=curvature_threshold * 0.5,
            min_stone_radius=min_stone_radius * 0.7,
            max_stone_radius=max_stone_radius * 1.3,
            cluster_distance=cluster_distance * 0.7,
        )
        stones_detected = len(stones)

    if stones_detected == 0:
        print("JewelForge: Still no stones — exporting as-is with edge sharpening only")
        sharpen_edges(obj, angle_deg=25)
    else:
        # Decimate for boolean speed
        print(f"\n=== DECIMATING FOR BOOLEAN ({initial_stats['faces']} → 150K) ===")
        decimate_for_processing(obj, target_faces=150000)

        # Cut seats
        print(f"\n=== CUTTING {stones_detected} STONE SEATS ===")
        cut_count = cut_all_seats(obj, stones)

        # Add prongs
        if add_prongs and cut_count > 0:
            print(f"\n=== ADDING BEAD PRONGS ===")
            add_bead_prongs(obj, stones, prong_count=prong_count)

        # Cleanup
        print("\n=== POST-PROCESSING ===")
        post_cleanup(obj)
        sharpen_edges(obj, angle_deg=25)

    # Export
    final_stats = get_mesh_stats(obj)
    print(f"\nJewelForge: Output — {final_stats['vertices']} verts, {final_stats['faces']} faces")

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    export_stl(output_stl)
    export_glb(output_glb)
    print(f"JewelForge: Exported STL + GLB")

    stats = {
        "input_vertices": initial_stats["vertices"],
        "input_faces": initial_stats["faces"],
        "output_vertices": final_stats["vertices"],
        "output_faces": final_stats["faces"],
        "is_manifold": final_stats["is_manifold"],
        "is_watertight": final_stats["is_watertight"],
        "bounding_box_mm": final_stats["bounding_box_mm"],
        "stones_detected": stones_detected,
        "stones_cut": cut_count if stones_detected > 0 else 0,
        "prongs_added": add_prongs and stones_detected > 0,
        "pave_cleanup": True,
    }
    print(f"JEWELFORGE_STATS:{json.dumps(stats)}")
    print("JewelForge: Pave cleanup done!")


if __name__ == "__main__":
    main()
