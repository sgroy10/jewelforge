"""Quick GLB → STL + OBJ converter in a single Blender invocation.

Used by ring_pipeline_v16 in the fallback path (sized-solid delivery) to
guarantee all three output formats are present. The hollow path already
produces STL via the hollow script; this script handles the case where the
sized-solid is shipped without ever going through hollow.

Usage:
    blender --background --python v16_finalize_outputs.py -- in.glb out.stl out.obj
"""

import sys
import bpy


def get_args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def main():
    args = get_args()
    if len(args) < 3:
        print("Usage: -- in.glb out.stl out.obj")
        sys.exit(1)
    in_glb, out_stl, out_obj = args[0], args[1], args[2]

    # Clear scene
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    # Import GLB and bake importer transform (handles Tripo coord-scale quirks)
    bpy.ops.import_scene.gltf(filepath=in_glb)
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if not meshes:
        print(f"FINALIZE_ERROR: no mesh in {in_glb}")
        sys.exit(2)
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

    # Make sure only this object is selected for both exports
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    # Export STL (mm scale). Version-gated for Blender 3.6 vs 4.x.
    if bpy.app.version >= (4, 0, 0):
        bpy.ops.wm.stl_export(
            filepath=out_stl,
            export_selected_objects=True,
            global_scale=1000.0,
            apply_modifiers=True,
            ascii_format=False,
        )
    else:
        bpy.ops.export_mesh.stl(
            filepath=out_stl,
            use_selection=True,
            global_scale=1000.0,
            ascii=False,
        )
    print(f"FINALIZE: exported STL -> {out_stl}")

    # Export OBJ (mm scale, with normals for Rhino compatibility)
    bpy.ops.wm.obj_export(
        filepath=out_obj,
        export_selected_objects=True,
        global_scale=1000.0,
        apply_modifiers=True,
        export_normals=True,
        export_materials=False,
        export_triangulated_mesh=False,
    )
    print(f"FINALIZE: exported OBJ -> {out_obj}")

    print(f"FINALIZE: vertices={len(obj.data.vertices):,}, "
          f"dims_mm={obj.dimensions.x*1000:.2f}x{obj.dimensions.y*1000:.2f}x{obj.dimensions.z*1000:.2f}")
    print("FINALIZE: Done")


if __name__ == "__main__":
    main()
