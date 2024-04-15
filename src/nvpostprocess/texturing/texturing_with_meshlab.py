import pymeshlab
import argparse
from pathlib import Path
import re
import os
import contextlib


@contextlib.contextmanager
def chdir(x):
    d = os.getcwd()
    os.chdir(x)
    try:
        yield
    finally:
        os.chdir(d)


def auto_texture(obj_path: Path, resolution: int = 4096):
    with chdir(obj_path.parent):
        obj_name = obj_path.name
        textname_str = obj_name\
            .replace("_mesh", "")\
            .replace("_pc", "")

        textname = Path(textname_str)\
            .with_suffix('.png')

        ms = pymeshlab.MeshSet()
        ms.load_new_mesh(obj_name)
        ms.compute_texcoord_by_function_per_vertex()
        ms.compute_texcoord_transfer_vertex_to_wedge()
        ms.compute_texcoord_parametrization_triangle_trivial_per_wedge(
            textdim=resolution,
            border=0,
            method="Basic"
        )
        try:
            ms.compute_texmap_from_color(
                textname=str(textname),
                textw=resolution,
                texth=resolution,
                overwrite=True
            )
        except pymeshlab.pmeshlab.PyMeshLabException as e:
            ms.compute_texmap_from_color(
                textname=str(textname),
                textw=resolution,
                texth=resolution,
                overwrite=False
            )
        mesh_outfile_name = str(obj_path.with_suffix('.pt.obj').name)
        ms.save_current_mesh(mesh_outfile_name)
        print(f"Texture saved as {textname}")


def auto_texture_batch(obj_folder: Path,
                       regex_filename_pattern: str = r".*.obj",
                       resolution: int = 4096,
                       dry_run: bool = False):
    for obj_path in obj_folder.glob('*.obj'):
        if re.match(regex_filename_pattern, obj_path.name):
            print(obj_path.name)
            if not dry_run:
                auto_texture(obj_path, resolution)
    print("Because PyMeshlab is a bugfest, the texture might be saved in a subdirectory")
    print("Enjoy your lunch")


def main():

    parser = argparse.ArgumentParser(description='Process OBJ file to generate texture')
    parser.add_argument('-d', '--dir', type=str, help='Directory with OBJ files to process')
    parser.add_argument('-r', '--resolution', type=int, help='Texture resolution', default=4096)
    parser.add_argument('-x', '--dry-run', action='store_true', help='Dry run')
    parser.add_argument('-p', '--pattern',
                        type=str,
                        help='Regex pattern for file name',
                        default=r"\d+_\D+\d+(_mesh)?.obj")

    args = parser.parse_args()

    if args.dir is None:
        raise ValueError('No file provided')
    else:
        dir_path = Path(args.dir)

    auto_texture_batch(dir_path,
                       args.pattern,
                       args.resolution,
                       args.dry_run)


if __name__ == '__main__':
    main()
