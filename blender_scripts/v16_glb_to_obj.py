"""Quick converter: GLB -> OBJ. Mirrors blender_hollow.py's OBJ settings."""
import sys
import bpy

args = sys.argv[sys.argv.index("--") + 1:]
in_glb, out_obj = args[0], args[1]

# Clear scene
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

# Import GLB and bake any importer transform
bpy.ops.import_scene.gltf(filepath=in_glb)
meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
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

# Export OBJ at mm scale
bpy.ops.object.select_all(action='DESELECT')
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
bpy.ops.wm.obj_export(
    filepath=out_obj,
    export_selected_objects=True,
    global_scale=1000.0,
    apply_modifiers=True,
    export_normals=True,
    export_materials=False,
    export_triangulated_mesh=False,
)
print(f"Converted: {in_glb} -> {out_obj}")
print(f"Vertices: {len(obj.data.vertices):,}")
print(f"Dimensions (mm): {obj.dimensions.x*1000:.2f} x {obj.dimensions.y*1000:.2f} x {obj.dimensions.z*1000:.2f}")
