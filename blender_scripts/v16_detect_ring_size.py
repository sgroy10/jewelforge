"""
Ring inner-diameter detection via ray casting from the central axis.

Replaces the failed histogram approaches (whole-mesh, band-only, min-radius)
which all got confused by internal mesh artifacts and asymmetric rings.

Algorithm — what Rhino does when measuring a finger hole:
  1. Find central axis = smallest bbox dim direction.
  2. Sample N points evenly along that axis (avoiding the two extreme ends).
  3. From each sample point, cast K rays outward at evenly-spaced angles
     in the plane perpendicular to the central axis.
  4. Each ray's FIRST HIT on the mesh = the inner finger hole edge at that
     position and angle.
  5. Take the MEDIAN of all hit distances. Robust against:
       - internal Tripo junk vertices (rays don't see them, only surfaces)
       - asymmetric ring shapes (chunky top, decorations)
       - sparse outliers (e.g. ray that misses entirely or hits stone setting)

Usage:
    blender --background --python detect_ring_size.py -- input.glb
    blender --background --python detect_ring_size.py -- input.glb --target-us 10.5 --output out.glb
    blender --background --python detect_ring_size.py -- input.glb --target-us 10.5 --output out.glb --output-stl out.stl
"""

import sys
import json
import math
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree


US_RING_SIZES = {
    3: 14.05, 3.5: 14.45, 4: 14.86, 4.5: 15.27,
    5: 15.70, 5.5: 16.10, 6: 16.51, 6.5: 16.92,
    7: 17.35, 7.5: 17.75, 8: 18.19, 8.5: 18.53,
    9: 18.89, 9.5: 19.41, 10: 19.84, 10.5: 20.20,
    11: 20.68, 11.5: 21.08, 12: 21.49, 12.5: 21.89, 13: 22.33,
}


def get_args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def parse_optional_args(args):
    out = {"target_us": None, "output_glb": None, "output_stl": None,
           "axial_samples": 20, "angular_samples": 72,
           "axial_skip_pct": 0.10}
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--target-us" and i + 1 < len(args):
            out["target_us"] = float(args[i + 1]); i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            out["output_glb"] = args[i + 1]; i += 2
        elif args[i] == "--output-stl" and i + 1 < len(args):
            out["output_stl"] = args[i + 1]; i += 2
        elif args[i] == "--axial" and i + 1 < len(args):
            out["axial_samples"] = int(args[i + 1]); i += 2
        elif args[i] == "--angular" and i + 1 < len(args):
            out["angular_samples"] = int(args[i + 1]); i += 2
        elif args[i] == "--skip" and i + 1 < len(args):
            out["axial_skip_pct"] = float(args[i + 1]); i += 2
        else:
            rest.append(args[i]); i += 1
    return rest, out


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
    # CRITICAL: bake any glTF-importer transform (location/rotation/scale) into
    # the vertex coords. Tripo's raw GLB imports with obj.scale ~ 0.030 (the
    # importer's mm/m unit compensation). Without baking, BVH ray_cast measures
    # in local units that don't match world units, and apply_uniform_scale
    # compounds with the existing scale → results 30x off.
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return obj


def closest_us_size(diameter_mm):
    return min(US_RING_SIZES.items(), key=lambda kv: abs(kv[1] - diameter_mm))


def detect_inner_diameter_raycast(obj, n_axial=20, n_angular=72,
                                   axial_skip_pct=0.10):
    """Ray-cast from points on central axis outward; median first-hit
    distance = inner radius. Returns (inner_diameter_mm, debug)."""
    dims = obj.dimensions
    axes = [("x", dims.x), ("y", dims.y), ("z", dims.z)]
    sorted_axes = sorted(axes, key=lambda p: p[1])
    central_axis = sorted_axes[0][0]

    verts = obj.data.vertices
    if not verts:
        return 0.0, {"error": "no vertices", "central_axis": central_axis}

    xs = [v.co.x for v in verts]
    ys = [v.co.y for v in verts]
    zs = [v.co.z for v in verts]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    cz = (min(zs) + max(zs)) / 2.0

    if central_axis == "x":
        axial_min, axial_max = min(xs), max(xs)
        perp_center = (cy, cz)
    elif central_axis == "y":
        axial_min, axial_max = min(ys), max(ys)
        perp_center = (cx, cz)
    else:
        axial_min, axial_max = min(zs), max(zs)
        perp_center = (cx, cy)

    # Sample range — skip the outermost portions where the ring's edge
    # cross-section may be irregular (chamfered, decorated)
    skip = (axial_max - axial_min) * axial_skip_pct
    sample_min = axial_min + skip
    sample_max = axial_max - skip

    # Build BVH from object's evaluated mesh
    deps = bpy.context.evaluated_depsgraph_get()
    bvh = BVHTree.FromObject(obj, deps)

    # Sample positions
    sample_positions = []
    for i in range(n_axial):
        t = i / max(1, n_axial - 1)
        axial_pos = sample_min + t * (sample_max - sample_min)
        if central_axis == "x":
            origin = Vector((axial_pos, cy, cz))
        elif central_axis == "y":
            origin = Vector((cx, axial_pos, cz))
        else:
            origin = Vector((cx, cy, axial_pos))
        sample_positions.append(origin)

    # Cast rays
    hit_distances = []  # in Blender meters
    miss_count = 0
    for origin in sample_positions:
        for k in range(n_angular):
            angle = (2.0 * math.pi * k) / n_angular
            co_a, si_a = math.cos(angle), math.sin(angle)
            if central_axis == "x":
                direction = Vector((0.0, co_a, si_a))
            elif central_axis == "y":
                direction = Vector((co_a, 0.0, si_a))
            else:
                direction = Vector((co_a, si_a, 0.0))
            location, normal, index, distance = bvh.ray_cast(origin, direction)
            if distance is not None and distance > 0:
                hit_distances.append(distance)
            else:
                miss_count += 1

    n_total = len(sample_positions) * n_angular
    n_hits = len(hit_distances)

    if n_hits == 0:
        return 0.0, {"error": "no ray hits", "central_axis": central_axis,
                     "n_total_rays": n_total, "n_misses": miss_count}

    sorted_hits = sorted(hit_distances)
    def pct(p):
        idx = max(0, min(n_hits - 1, int(p * n_hits)))
        return sorted_hits[idx] * 1000.0

    median_radius_mm = pct(0.50)
    debug = {
        "central_axis": central_axis,
        "n_axial_samples": n_axial,
        "n_angular_samples": n_angular,
        "n_total_rays": n_total,
        "n_hits": n_hits,
        "n_misses": miss_count,
        "hit_rate": round(n_hits / n_total, 4),
        "axial_range_mm": [round((sample_min) * 1000, 3),
                           round((sample_max) * 1000, 3)],
        "p05_radius_mm": round(pct(0.05), 4),
        "p10_radius_mm": round(pct(0.10), 4),
        "p25_radius_mm": round(pct(0.25), 4),
        "p50_radius_mm": round(median_radius_mm, 4),
        "p75_radius_mm": round(pct(0.75), 4),
        "p90_radius_mm": round(pct(0.90), 4),
        "p95_radius_mm": round(pct(0.95), 4),
        "min_radius_mm": round(sorted_hits[0] * 1000, 4),
        "max_radius_mm": round(sorted_hits[-1] * 1000, 4),
    }
    inner_diameter_mm = 2.0 * median_radius_mm
    debug["inner_diameter_mm"] = round(inner_diameter_mm, 4)
    return inner_diameter_mm, debug


def detect_inner_diameter_horizontal_max_at_mid(obj, n_angular=72):
    """Match Rhino CAD guy's measurement: at the mid-axial slice (one specific
    perpendicular plane through the center of the cavity), find the WIDEST
    inner diameter across all directions. This replicates what a jeweler does
    in Rhino — they draw a horizontal line across the middle of the cavity at
    its widest point.

    Difference from detect_inner_diameter_raycast (legacy):
      - Legacy: median of 1440 rays (20 axial * 72 angular) — averages across
        the WHOLE cavity in all directions. For oval cavities (decorated rings
        where the gallery compresses the cavity vertically), this UNDERSHOOTS
        the horizontal max that Rhino reads.
      - This function: single axial slice at center, pair opposite-direction
        rays into diameter lines, take MAX. Matches Rhino's tool exactly.

    Trade-off accepted per CAD guy 2026-05-12: the vertical inner diameter
    will be ~5% smaller than horizontal on decorated rings (half ring size
    down on the finger). Lovable UX should advise customers to size up on
    chunky/decorated designs.

    Returns (inner_diameter_mm, debug)."""
    dims = obj.dimensions
    axes = [("x", dims.x), ("y", dims.y), ("z", dims.z)]
    sorted_axes = sorted(axes, key=lambda p: p[1])
    central_axis = sorted_axes[0][0]

    verts = obj.data.vertices
    if not verts:
        return 0.0, {"error": "no vertices", "central_axis": central_axis}

    xs = [v.co.x for v in verts]
    ys = [v.co.y for v in verts]
    zs = [v.co.z for v in verts]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    cz = (min(zs) + max(zs)) / 2.0
    origin = Vector((cx, cy, cz))

    deps = bpy.context.evaluated_depsgraph_get()
    bvh = BVHTree.FromObject(obj, deps)

    # Cast paired opposite rays at each angle to get diameter (left + right hit distances)
    half = n_angular // 2
    diameters_m = []  # list of (angle_deg, diameter_meters)
    for k in range(half):
        angle1 = (2.0 * math.pi * k) / n_angular
        angle2 = angle1 + math.pi
        co1, si1 = math.cos(angle1), math.sin(angle1)
        co2, si2 = math.cos(angle2), math.sin(angle2)
        if central_axis == "x":
            dir1 = Vector((0.0, co1, si1))
            dir2 = Vector((0.0, co2, si2))
        elif central_axis == "y":
            dir1 = Vector((co1, 0.0, si1))
            dir2 = Vector((co2, 0.0, si2))
        else:
            dir1 = Vector((co1, si1, 0.0))
            dir2 = Vector((co2, si2, 0.0))
        _, _, _, d1 = bvh.ray_cast(origin, dir1)
        _, _, _, d2 = bvh.ray_cast(origin, dir2)
        if d1 is not None and d2 is not None and d1 > 0 and d2 > 0:
            diameters_m.append((math.degrees(angle1), d1 + d2))

    if not diameters_m:
        return 0.0, {"error": "no diameter pairs",
                     "central_axis": central_axis,
                     "method": "horizontal_max_at_mid"}

    sorted_dia_m = sorted(d for _, d in diameters_m)
    n = len(sorted_dia_m)
    def pct(p):
        return sorted_dia_m[min(n - 1, int(p * n))] * 1000.0
    max_diameter_m = sorted_dia_m[-1]
    max_angle_deg = max(diameters_m, key=lambda p: p[1])[0]
    inner_diameter_mm = max_diameter_m * 1000.0
    debug = {
        "method": "horizontal_max_at_mid",
        "central_axis": central_axis,
        "n_directions": len(diameters_m),
        "max_diameter_mm": round(inner_diameter_mm, 4),
        "max_at_angle_deg": round(max_angle_deg, 1),
        "min_diameter_mm": round(sorted_dia_m[0] * 1000, 4),
        "p50_diameter_mm": round(pct(0.50), 4),
        "p90_diameter_mm": round(pct(0.90), 4),
        "p95_diameter_mm": round(pct(0.95), 4),
        "inner_diameter_mm": round(inner_diameter_mm, 4),
    }
    return inner_diameter_mm, debug


def apply_uniform_scale(obj, factor):
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    obj.scale *= factor
    bpy.context.view_layer.update()
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)


def export_glb(obj, filepath):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.export_scene.gltf(filepath=filepath, export_format='GLB',
                              use_selection=True, export_apply=True)


def export_stl(obj, filepath):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    if bpy.app.version >= (4, 0, 0):
        bpy.ops.wm.stl_export(filepath=filepath, export_selected_objects=True,
                              global_scale=1000.0, apply_modifiers=True, ascii_format=False)
    else:
        bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True,
                                global_scale=1000.0, ascii=False)


def main():
    raw_args = get_args()
    rest, opts = parse_optional_args(raw_args)
    if not rest:
        print("Usage: -- input.glb [--target-us 10.5] [--output out.glb] [--output-stl out.stl]")
        sys.exit(1)
    in_glb = rest[0]
    target_us = opts["target_us"]
    out_glb = opts["output_glb"]
    out_stl = opts["output_stl"]

    print(f"Detector: input={in_glb}")
    print(f"Detector: blender {bpy.app.version_string}")
    print(f"Detector: ray casting with {opts['axial_samples']} axial * "
          f"{opts['angular_samples']} angular = "
          f"{opts['axial_samples'] * opts['angular_samples']} rays")

    clear_scene()
    obj = import_glb_and_join(in_glb)

    # PRIMARY: horizontal-max-at-mid (matches Rhino CAD guy's measurement)
    inner_dia, debug = detect_inner_diameter_horizontal_max_at_mid(
        obj, n_angular=opts["angular_samples"]
    )
    closest_size, closest_dia = closest_us_size(inner_dia)

    # LEGACY (for reference logging only): median across all axial slices
    legacy_inner_dia, legacy_debug = detect_inner_diameter_raycast(
        obj,
        n_axial=opts["axial_samples"],
        n_angular=opts["angular_samples"],
        axial_skip_pct=opts["axial_skip_pct"],
    )

    print(f"Detector: central axis = {debug.get('central_axis')}")
    print(f"Detector: [PRIMARY] horizontal-max-at-mid:")
    print(f"           max diameter = {debug.get('max_diameter_mm')} mm  "
          f"(at angle {debug.get('max_at_angle_deg')}°)")
    print(f"           min diameter = {debug.get('min_diameter_mm')} mm  "
          f"(perpendicular to max)")
    print(f"           p50/p90/p95 = {debug.get('p50_diameter_mm')} / "
          f"{debug.get('p90_diameter_mm')} / {debug.get('p95_diameter_mm')} mm")
    print(f"Detector: [LEGACY ref] median-across-all-axial = {legacy_inner_dia:.3f} mm")
    print(f"Detector: ===> INNER DIAMETER (used for scaling) = {inner_dia:.3f} mm")
    print(f"Detector: ===> Closest US size = {closest_size}  (table inner = {closest_dia:.2f} mm)")

    result = {
        "input": in_glb,
        "method": "raycast",
        "measured_inner_diameter_mm": round(inner_dia, 4),
        "closest_us_size": closest_size,
        "closest_us_inner_dia_mm": closest_dia,
        "debug": debug,
    }

    if target_us is not None:
        if target_us not in US_RING_SIZES:
            print(f"Detector: ERROR unknown US size {target_us}")
            sys.exit(2)
        target_inner = US_RING_SIZES[target_us]
        factor = target_inner / inner_dia if inner_dia > 0 else 1.0
        print(f"Detector: target US {target_us} = {target_inner:.2f} mm inner")
        print(f"Detector: scale factor needed = {factor:.6f}")
        result["target_us_size"] = target_us
        result["target_inner_dia_mm"] = target_inner
        result["scale_factor"] = round(factor, 6)

        if out_glb or out_stl:
            print(f"Detector: applying uniform scale {factor:.6f} ...")
            apply_uniform_scale(obj, factor)
            # Post-scale: use the SAME primary metric (horizontal-max-at-mid)
            # to confirm the ring is now at the target Rhino-readable size.
            inner2, debug2 = detect_inner_diameter_horizontal_max_at_mid(
                obj, n_angular=opts["angular_samples"]
            )
            # Also log legacy median for reference
            legacy_inner2, _ = detect_inner_diameter_raycast(
                obj,
                n_axial=opts["axial_samples"],
                n_angular=opts["angular_samples"],
                axial_skip_pct=opts["axial_skip_pct"],
            )
            print(f"Detector: post-scale horizontal-max = {inner2:.3f} mm "
                  f"(target {target_inner:.2f}, delta {inner2 - target_inner:+.3f}mm)")
            print(f"Detector: post-scale legacy-median = {legacy_inner2:.3f} mm "
                  f"(for reference — what previous detector would have said)")
            print(f"Detector: post-scale min-diameter = {debug2.get('min_diameter_mm')} mm "
                  f"(perpendicular to widest — what finger fit feels like)")
            result["post_scale_inner_dia_mm"] = round(inner2, 4)
            result["post_scale_delta_mm"] = round(inner2 - target_inner, 4)
            new_dims = obj.dimensions
            result["post_scale_bbox_mm"] = {
                "x": round(new_dims.x * 1000, 3),
                "y": round(new_dims.y * 1000, 3),
                "z": round(new_dims.z * 1000, 3),
            }
            print(f"Detector: post-scale bbox = "
                  f"{new_dims.x*1000:.3f} x {new_dims.y*1000:.3f} x {new_dims.z*1000:.3f} mm")
            if out_glb:
                export_glb(obj, out_glb)
                print(f"Detector: exported GLB -> {out_glb}")
                result["output_glb"] = out_glb
            if out_stl:
                export_stl(obj, out_stl)
                print(f"Detector: exported STL -> {out_stl}")
                result["output_stl"] = out_stl

    print(f"DETECTOR_RESULT:{json.dumps(result)}")
    print("Detector: Done")


if __name__ == "__main__":
    main()
