"""
Render a GLB from 4 fixed camera angles to PNG.

Used by vision_check.py to generate image pairs for Gemini 3.1 Pro
visual destruction-detection.

Usage:
    blender --background --python render_views.py -- /abs/in.glb /abs/out_dir [resolution]

Output (4 PNGs in out_dir):
    view_front.png
    view_side.png
    view_top.png
    view_perspective.png

Camera positions are computed from the mesh's bbox so framing is
consistent across rings of different sizes.
"""

import sys
import os
import math
import bpy
from mathutils import Vector, Matrix


def get_args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for mesh in list(bpy.data.meshes):
        bpy.data.meshes.remove(mesh)
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)
    for light in list(bpy.data.lights):
        bpy.data.lights.remove(light)
    for cam in list(bpy.data.cameras):
        bpy.data.cameras.remove(cam)


def import_glb(path):
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
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return obj


def center_object_at_origin(obj):
    """Translate vertices so bbox center is at world origin (bakes into mesh)."""
    bm_corners = [Vector(c) for c in obj.bound_box]
    cx = sum(v.x for v in bm_corners) / 8
    cy = sum(v.y for v in bm_corners) / 8
    cz = sum(v.z for v in bm_corners) / 8
    obj.location = (-cx, -cy, -cz)
    bpy.context.view_layer.update()
    bpy.ops.object.transform_apply(location=True, rotation=False, scale=False)


def setup_material(obj):
    mat = bpy.data.materials.new(name="RingMatte")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.80, 0.80, 0.82, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.45
        bsdf.inputs["Metallic"].default_value = 0.0
    obj.data.materials.clear()
    obj.data.materials.append(mat)


def setup_world():
    world = bpy.data.worlds["World"] if "World" in bpy.data.worlds else bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        bg.inputs["Strength"].default_value = 1.5


def add_lights(bbox_radius):
    """Three-point lighting for clear feature visibility."""
    positions_and_energies = [
        ("KeyLight",  Vector(( bbox_radius * 4, -bbox_radius * 3, bbox_radius * 4)),  bbox_radius * 4, 1000),
        ("FillLight", Vector((-bbox_radius * 4, -bbox_radius * 2, bbox_radius * 2)), bbox_radius * 4, 500),
        ("RimLight",  Vector((0,                bbox_radius * 4,  bbox_radius * 3)), bbox_radius * 2, 400),
    ]
    # Energies scale with bbox^2 (area light), so adjust for tiny rings
    energy_scale = max(bbox_radius * bbox_radius * 4, 0.001)
    for name, loc, size, base_energy in positions_and_energies:
        ldata = bpy.data.lights.new(name, 'AREA')
        ldata.energy = base_energy * energy_scale
        ldata.size = size
        light = bpy.data.objects.new(name, ldata)
        light.location = loc
        bpy.context.scene.collection.objects.link(light)


def make_camera_look_at(cam_obj, target=Vector((0, 0, 0))):
    """Set camera rotation so its -Z axis points toward `target`. No constraints."""
    direction = (target - cam_obj.location).normalized()
    # Default camera looks down -Z with Y up. Build rotation matrix from direction.
    # track_to: -Z -> direction, Y -> closest to global Z.
    quat = direction.to_track_quat('-Z', 'Y')
    cam_obj.rotation_euler = quat.to_euler()


def render_to(filepath, resolution=768):
    scene = bpy.context.scene
    # BLENDER_EEVEE_NEXT was introduced in Blender 4.2 (replaces BLENDER_EEVEE).
    # Railway production runs Blender 3.6.5 which only knows BLENDER_EEVEE.
    # Local dev runs 4.3.2 which has both (EEVEE_NEXT preferred). Without this
    # version-gate, the render call rejects the enum and writes zero PNGs —
    # exactly the bug we saw on 2026-05-14 (every ring fell back to solid).
    if bpy.app.version >= (4, 2, 0):
        scene.render.engine = 'BLENDER_EEVEE_NEXT'
    else:
        scene.render.engine = 'BLENDER_EEVEE'
    scene.render.resolution_x = resolution
    scene.render.resolution_y = resolution
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGB'
    scene.render.filepath = os.path.abspath(filepath)
    bpy.ops.render.render(write_still=True)


def main():
    args = get_args()
    if len(args) < 2:
        print("Usage: -- /abs/in.glb /abs/out_dir [resolution]")
        sys.exit(1)
    in_glb = os.path.abspath(args[0])
    out_dir = os.path.abspath(args[1])
    resolution = int(args[2]) if len(args) > 2 else 768

    os.makedirs(out_dir, exist_ok=True)

    print(f"Render: input = {in_glb}")
    print(f"Render: output dir = {out_dir}")
    print(f"Render: resolution = {resolution}x{resolution}")

    clear_scene()
    obj = import_glb(in_glb)
    print(f"Render: imported {len(obj.data.vertices):,}v, dims = "
          f"{obj.dimensions.x*1000:.2f} x {obj.dimensions.y*1000:.2f} x {obj.dimensions.z*1000:.2f} mm")
    center_object_at_origin(obj)
    setup_material(obj)
    setup_world()

    dims = obj.dimensions
    bbox_radius = max(dims.x, dims.y, dims.z) / 2.0
    cam_dist = bbox_radius * 4.5  # generous framing
    print(f"Render: bbox_radius = {bbox_radius*1000:.2f} mm, cam_dist = {cam_dist*1000:.2f} mm")

    add_lights(bbox_radius)

    # Single camera, repositioned for each view (no constraints).
    # Convention: ring's finger axis = X (smallest dim usually).
    # FRONT = looking down +Y at origin (camera at -Y, sees the ring face-on)
    # SIDE  = looking down +X at origin (camera at -X)
    # TOP   = looking down -Z at origin (camera at +Z)
    # PERSP = 3/4 angle, slight elevation
    views = [
        ("view_front",       Vector((0,              -cam_dist,        0))),
        ("view_side",        Vector((-cam_dist,       0,               0))),
        ("view_top",         Vector((0,               0,               cam_dist))),
        ("view_perspective", Vector(( cam_dist * 0.7, -cam_dist * 0.7, cam_dist * 0.6))),
    ]

    # Create camera once
    cam_data = bpy.data.cameras.new("Cam")
    cam_data.angle = math.radians(35)
    # Critical: default clip_start is 0.1m but our rings are at ~0.06m scale.
    # Without this fix the geometry sits behind the near clip plane and the
    # render is empty (background-only).
    cam_data.clip_start = 0.001  # 1mm
    cam_data.clip_end = 100.0    # 100m
    cam = bpy.data.objects.new("Cam", cam_data)
    bpy.context.scene.collection.objects.link(cam)
    bpy.context.scene.camera = cam

    # Diagnostic: print scene state once before first render
    print(f"DIAG: scene.camera = {bpy.context.scene.camera}")
    print(f"DIAG: scene objects = {[o.name + ':' + o.type for o in bpy.context.scene.objects]}")
    print(f"DIAG: obj.location = {obj.location}")
    print(f"DIAG: obj.dimensions = {obj.dimensions}")
    # Verify obj bbox in WORLD space after centering
    world_corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    print(f"DIAG: obj world_bbox min/max:")
    print(f"      x: {min(c.x for c in world_corners):.4f} to {max(c.x for c in world_corners):.4f}")
    print(f"      y: {min(c.y for c in world_corners):.4f} to {max(c.y for c in world_corners):.4f}")
    print(f"      z: {min(c.z for c in world_corners):.4f} to {max(c.z for c in world_corners):.4f}")

    for name, loc in views:
        cam.location = loc
        make_camera_look_at(cam, Vector((0, 0, 0)))
        bpy.context.view_layer.update()
        print(f"DIAG: cam loc={cam.location} rot_euler={cam.rotation_euler}")
        out_path = os.path.join(out_dir, f"{name}.png")
        print(f"Render: {name} -> {out_path}")
        render_to(out_path, resolution)

    print("Render: Done")


if __name__ == "__main__":
    main()
