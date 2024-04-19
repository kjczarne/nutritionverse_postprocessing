import subprocess
from pathlib import Path
import toml
import argparse
import os


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=str,
                        default="data", help="Path to the directory containing the input meshes")
    parser.add_argument("--output-dir", type=str,
                        default="outputs", help="Path to the directory to save the output textures")
    parser.add_argument("--metadata-dir", type=str,
                        default="metadata", help="Path to the directory containing the metadata files")
    parser.add_argument("--device", type=str,
                        default="a6000", help="Device to use for texture generation",
                        choices=["a6000", "2080"])
    parser.add_argument("--device-id", type=str, default="cuda:0",
                        help="Specify which gpu to use")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the commands without executing them")

    args = parser.parse_args()
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    metadata_dir = Path(args.metadata_dir)
    device = args.device
    device_id = args.device_id

    for file in input_dir.glob("*.obj"):
        metadata_file_path = metadata_dir / f"{file.stem.replace('_mesh.pt', '')}.toml"
        with open(metadata_file_path, "r") as f:
            metadata = toml.load(f)

        description = f'{metadata["item"]["food_type"]}, {metadata["item"]["description"]}'

        command = f"""python scripts/generate_texture.py \
            --input_dir {str(input_dir)} \
            --output_dir {os.path.join(str(output_dir), file.stem)} \
            --obj_name {file.stem} \
            --obj_file {file.name} \
            --prompt \"{description}\" \
            --add_view_to_prompt \
            --ddim_steps 50 \
            --new_strength 1 \
            --update_strength 0.3 \
            --view_threshold 0.1 \
            --blend 0 \
            --dist 1 \
            --num_viewpoints 36 \
            --viewpoint_mode predefined \
            --use_principle \
            --update_steps 20 \
            --update_mode heuristic \
            --seed 42 \
            --post_process \
            --device \"{device}\" \
            --device_id \"{device_id}\" \
            --use_objaverse # assume the mesh is normalized with y-axis as up
        """
        print(command)
        if args.dry_run:
            print(command)
        else:
            subprocess.run(command, shell=True, check=True)


if __name__ == "__main__":
    main()
