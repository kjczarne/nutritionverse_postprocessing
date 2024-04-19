# common utils
import os
import argparse
import time

# pytorch3d
from pytorch3d.renderer import TexturesUV

# torch
import torch

from torchvision import transforms

# numpy
import numpy as np

# image
from PIL import Image


# customized
import sys
sys.path.append(".")

from lib.mesh_helper import (
    init_mesh,
    apply_offsets_to_mesh,
    adjust_uv_map
)
from lib.render_helper import render
from lib.io_helper import (
    save_backproject_obj,
    save_args,
    save_viewpoints
)
from lib.vis_helper import (
    visualize_outputs, 
    visualize_principle_viewpoints, 
    visualize_refinement_viewpoints
)
from lib.diffusion_helper import (
    get_controlnet_depth,
    get_inpainting,
    apply_controlnet_depth,
    apply_inpainting_postprocess
)
from lib.projection_helper import (
    backproject_from_image,
    render_one_view_and_build_masks,
    select_viewpoint,
    build_similarity_texture_cache_for_all_views
)
from lib.camera_helper import init_viewpoints



"""
    Use Diffusion Models conditioned on depth input to back-project textures on 3D mesh.

    The inputs should be constructed as follows:
        - <input_dir>/
            |- <obj_file> # name of the input OBJ file
    
    The outputs of this script would be stored under `outputs/`, with the
    configuration parameters as the folder name. Specifically, there should be following files in such
    folder:
        - outputs/
            |- <configs>/                       # configurations of the run
                |- generate/                    # assets generated in generation stage
                    |- depth/                   # depth map
                    |- inpainted/               # images generated by diffusion models
                    |- intermediate/            # renderings of textured mesh after each step
                    |- mask/                    # generation mask
                    |- mesh/                    # textured mesh
                    |- normal/                  # normal map
                    |- rendering/               # input renderings
                    |- similarity/              # simiarity map
                |- update/                      # assets generated in refinement stage
                    |- ...                      # the structure is the same as generate/
                |- args.json                    # all arguments for the run
                |- viewpoints.json              # all viewpoints
                |- principle_viewpoints.png     # principle viewpoints
                |- refinement_viewpoints.png    # refinement viewpoints

"""

def init_args():
    print("=> initializing input arguments...")
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_dir", type=str, required=True)
    parser.add_argument("--output_dir", type=str, default="./outputs")
    parser.add_argument("--obj_name", type=str, required=True)
    parser.add_argument("--obj_file", type=str, required=True)
    parser.add_argument("--prompt", type=str, required=True)
    parser.add_argument("--a_prompt", type=str, default="best quality, high quality, extremely detailed, good geometry")
    parser.add_argument("--n_prompt", type=str, default="deformed, extra digit, fewer digits, cropped, worst quality, low quality, smoke")
    parser.add_argument("--new_strength", type=float, default=1)
    parser.add_argument("--update_strength", type=float, default=0.5)
    parser.add_argument("--ddim_steps", type=int, default=20)
    parser.add_argument("--guidance_scale", type=float, default=10)
    parser.add_argument("--output_scale", type=float, default=1)
    parser.add_argument("--view_threshold", type=float, default=0.1)
    parser.add_argument("--num_viewpoints", type=int, default=8)
    parser.add_argument("--viewpoint_mode", type=str, default="predefined", choices=["predefined", "hemisphere"])
    parser.add_argument("--update_steps", type=int, default=8)
    parser.add_argument("--update_mode", type=str, default="heuristic", choices=["sequential", "heuristic", "random"])
    parser.add_argument("--blend", type=float, default=0.5)
    parser.add_argument("--eta", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=42)

    parser.add_argument("--use_patch", action="store_true", help="apply repaint during refinement to patch up the missing regions")
    parser.add_argument("--use_multiple_objects", action="store_true", help="operate on multiple objects")
    parser.add_argument("--use_principle", action="store_true", help="poperate on multiple objects")
    parser.add_argument("--use_shapenet", action="store_true", help="operate on ShapeNet objects")
    parser.add_argument("--use_objaverse", action="store_true", help="operate on Objaverse objects")
    parser.add_argument("--use_unnormalized", action="store_true", help="save unnormalized mesh")

    parser.add_argument("--add_view_to_prompt", action="store_true", help="add view information to the prompt")
    parser.add_argument("--post_process", action="store_true", help="post processing the texture")

    parser.add_argument("--smooth_mask", action="store_true", help="smooth the diffusion mask")

    parser.add_argument("--force", action="store_true", help="forcefully generate more image")

    # negative options
    parser.add_argument("--no_repaint", action="store_true", help="do NOT apply repaint")
    parser.add_argument("--no_update", action="store_true", help="do NOT apply update")

    # device parameters
    parser.add_argument("--device", type=str, default="a6000")
    parser.add_argument("--device_id", type=str, default="cuda:0")

    # camera parameters NOTE need careful tuning!!!
    parser.add_argument("--test_camera", action="store_true")
    parser.add_argument("--dist", type=float, default=1, 
        help="distance to the camera from the object")
    parser.add_argument("--elev", type=float, default=0,
        help="the angle between the vector from the object to the camera and the horizontal plane")
    parser.add_argument("--azim", type=float, default=180,
        help="the angle between the vector from the object to the camera and the vertical plane")

    args = parser.parse_args()

    if args.device == "a6000":
        setattr(args, "render_simple_factor", 12)
        setattr(args, "fragment_k", 1)
        setattr(args, "image_size", 768)
        setattr(args, "uv_size", 3000)
    else:
        setattr(args, "render_simple_factor", 4)
        setattr(args, "fragment_k", 1)
        setattr(args, "image_size", 768)
        setattr(args, "uv_size", 1000)

    return args


if __name__ == "__main__":
    args = init_args()
    # Setup
    if torch.cuda.is_available():
        DEVICE = torch.device(args.device_id)
        torch.cuda.set_device(DEVICE)
    else:
        print("no gpu avaiable")
        exit()

    # save
    output_dir = os.path.join(
        args.output_dir, 
        "{}-{}-{}-{}-{}-{}".format(
            str(args.seed),
            args.viewpoint_mode[0]+str(args.num_viewpoints),
            args.update_mode[0]+str(args.update_steps),
            str(args.new_strength),
            str(args.update_strength),
            str(args.view_threshold)
        ),
    )
    if args.no_repaint: output_dir += "-norepaint"
    if args.no_update: output_dir += "-noupdate"

    os.makedirs(output_dir, exist_ok=True)
    print("=> OUTPUT_DIR:", output_dir)

    # init resources
    # init mesh
    mesh, _, faces, aux, principle_directions, mesh_center, mesh_scale = init_mesh(
        os.path.join(args.input_dir, args.obj_file),
        os.path.join(output_dir, args.obj_file), 
        DEVICE
    )

    # gradient texture
    init_texture = Image.open("./samples/textures/dummy.png").convert("RGB").resize((args.uv_size, args.uv_size))

    # HACK adjust UVs for multiple materials
    if args.use_multiple_objects:
        new_verts_uvs, init_texture = adjust_uv_map(faces, aux, init_texture, args.uv_size)
    else:
        new_verts_uvs = aux.verts_uvs

    # update the mesh
    mesh.textures = TexturesUV(
        maps=transforms.ToTensor()(init_texture)[None, ...].permute(0, 2, 3, 1).to(DEVICE),
        faces_uvs=faces.textures_idx[None, ...],
        verts_uvs=new_verts_uvs[None, ...]
    )

    # back-projected faces
    exist_texture = torch.from_numpy(np.zeros([args.uv_size, args.uv_size]).astype(np.float32)).to(DEVICE)

    # initialize viewpoints
    # including: principle viewpoints for generation + refinement viewpoints for updating
    (
        dist_list, 
        elev_list, 
        azim_list, 
        sector_list,
        view_punishments
    ) = init_viewpoints(args.viewpoint_mode, args.num_viewpoints, args.dist, args.elev, principle_directions, 
                            use_principle=True, 
                            use_shapenet=args.use_shapenet,
                            use_objaverse=args.use_objaverse)

    # save args
    save_args(args, output_dir)

    # initialize depth2image model
    controlnet, ddim_sampler = get_controlnet_depth()


    # ------------------- OPERATION ZONE BELOW ------------------------

    # 1. generate texture with RePaint 
    # NOTE no update / refinement

    generate_dir = os.path.join(output_dir, "generate")
    os.makedirs(generate_dir, exist_ok=True)

    update_dir = os.path.join(output_dir, "update")
    os.makedirs(update_dir, exist_ok=True)

    init_image_dir = os.path.join(generate_dir, "rendering")
    os.makedirs(init_image_dir, exist_ok=True)

    normal_map_dir = os.path.join(generate_dir, "normal")
    os.makedirs(normal_map_dir, exist_ok=True)

    mask_image_dir = os.path.join(generate_dir, "mask")
    os.makedirs(mask_image_dir, exist_ok=True)

    depth_map_dir = os.path.join(generate_dir, "depth")
    os.makedirs(depth_map_dir, exist_ok=True)

    similarity_map_dir = os.path.join(generate_dir, "similarity")
    os.makedirs(similarity_map_dir, exist_ok=True)

    inpainted_image_dir = os.path.join(generate_dir, "inpainted")
    os.makedirs(inpainted_image_dir, exist_ok=True)

    mesh_dir = os.path.join(generate_dir, "mesh")
    os.makedirs(mesh_dir, exist_ok=True)

    interm_dir = os.path.join(generate_dir, "intermediate")
    os.makedirs(interm_dir, exist_ok=True)

    # prepare viewpoints and cache
    NUM_PRINCIPLE = 10 if args.use_shapenet or args.use_objaverse else 6
    pre_dist_list = dist_list[:NUM_PRINCIPLE]
    pre_elev_list = elev_list[:NUM_PRINCIPLE]
    pre_azim_list = azim_list[:NUM_PRINCIPLE]
    pre_sector_list = sector_list[:NUM_PRINCIPLE]
    pre_view_punishments = view_punishments[:NUM_PRINCIPLE]

    pre_similarity_texture_cache = build_similarity_texture_cache_for_all_views(mesh, faces, new_verts_uvs,
        pre_dist_list, pre_elev_list, pre_azim_list,
        args.image_size, args.image_size * args.render_simple_factor, args.uv_size, args.fragment_k,
        DEVICE
    )


    # start generation
    print("=> start generating texture...")
    start_time = time.time()
    for view_idx in range(NUM_PRINCIPLE):
        print("=> processing view {}...".format(view_idx))

        # sequentially pop the viewpoints
        dist, elev, azim, sector = pre_dist_list[view_idx], pre_elev_list[view_idx], pre_azim_list[view_idx], pre_sector_list[view_idx] 
        prompt = " the {} view of {}".format(sector, args.prompt) if args.add_view_to_prompt else args.prompt
        print("=> generating image for prompt: {}...".format(prompt))

        # 1.1. render and build masks
        (
            view_score,
            renderer, cameras, fragments,
            init_image, normal_map, depth_map, 
            init_images_tensor, normal_maps_tensor, depth_maps_tensor, similarity_tensor, 
            keep_mask_image, update_mask_image, generate_mask_image, 
            keep_mask_tensor, update_mask_tensor, generate_mask_tensor, all_mask_tensor, quad_mask_tensor,
        ) = render_one_view_and_build_masks(dist, elev, azim, 
            view_idx, view_idx, view_punishments, # => actual view idx and the sequence idx 
            pre_similarity_texture_cache, exist_texture,
            mesh, faces, new_verts_uvs,
            args.image_size, args.fragment_k,
            init_image_dir, mask_image_dir, normal_map_dir, depth_map_dir, similarity_map_dir,
            DEVICE, save_intermediate=True, smooth_mask=args.smooth_mask, view_threshold=args.view_threshold
        )

        # 1.2. generate missing region
        # NOTE first view still gets the mask for consistent ablations
        if args.no_repaint and view_idx != 0:
            actual_generate_mask_image = Image.fromarray((np.ones_like(np.array(generate_mask_image)) * 255.).astype(np.uint8))
        else:
            actual_generate_mask_image = generate_mask_image

        print("=> generate for view {}".format(view_idx))
        generate_image, generate_image_before, generate_image_after = apply_controlnet_depth(controlnet, ddim_sampler, 
            init_image.convert("RGBA"), prompt, args.new_strength, args.ddim_steps,
            actual_generate_mask_image, keep_mask_image, depth_maps_tensor.permute(1, 2, 0).repeat(1, 1, 3).cpu().numpy(), 
            args.a_prompt, args.n_prompt, args.guidance_scale, args.seed, args.eta, 1, DEVICE, args.blend)

        generate_image.save(os.path.join(inpainted_image_dir, "{}.png".format(view_idx)))
        generate_image_before.save(os.path.join(inpainted_image_dir, "{}_before.png".format(view_idx)))
        generate_image_after.save(os.path.join(inpainted_image_dir, "{}_after.png".format(view_idx)))

        # 1.2.2 back-project and create texture
        # NOTE projection mask = generate mask
        init_texture, project_mask_image, exist_texture = backproject_from_image(
            mesh, faces, new_verts_uvs, cameras, 
            generate_image, generate_mask_image, generate_mask_image, init_texture, exist_texture, 
            args.image_size * args.render_simple_factor, args.uv_size, args.fragment_k,
            DEVICE
        )

        project_mask_image.save(os.path.join(mask_image_dir, "{}_project.png".format(view_idx)))

        # update the mesh
        mesh.textures = TexturesUV(
            maps=transforms.ToTensor()(init_texture)[None, ...].permute(0, 2, 3, 1).to(DEVICE),
            faces_uvs=faces.textures_idx[None, ...],
            verts_uvs=new_verts_uvs[None, ...]
        )

        # 1.2.3. re: render 
        # NOTE only the rendered image is needed - masks should be re-used
        (
            view_score,
            renderer, cameras, fragments,
            init_image, *_,
        ) = render_one_view_and_build_masks(dist, elev, azim, 
            view_idx, view_idx, view_punishments, # => actual view idx and the sequence idx 
            pre_similarity_texture_cache, exist_texture,
            mesh, faces, new_verts_uvs,
            args.image_size, args.fragment_k,
            init_image_dir, mask_image_dir, normal_map_dir, depth_map_dir, similarity_map_dir,
            DEVICE, save_intermediate=False, smooth_mask=args.smooth_mask, view_threshold=args.view_threshold
        )

        # 1.3. update blurry region
        # only when: 1) use update flag; 2) there are contents to update; 3) there are enough contexts.
        if not args.no_update and update_mask_tensor.sum() > 0 and update_mask_tensor.sum() / (all_mask_tensor.sum()) > 0.05:
            print("=> update {} pixels for view {}".format(update_mask_tensor.sum().int(), view_idx))
            diffused_image, diffused_image_before, diffused_image_after = apply_controlnet_depth(controlnet, ddim_sampler, 
                init_image.convert("RGBA"), prompt, args.update_strength, args.ddim_steps,
                update_mask_image, keep_mask_image, depth_maps_tensor.permute(1, 2, 0).repeat(1, 1, 3).cpu().numpy(), 
                args.a_prompt, args.n_prompt, args.guidance_scale, args.seed, args.eta, 1, DEVICE, args.blend)

            diffused_image.save(os.path.join(inpainted_image_dir, "{}_update.png".format(view_idx)))
            diffused_image_before.save(os.path.join(inpainted_image_dir, "{}_update_before.png".format(view_idx)))
            diffused_image_after.save(os.path.join(inpainted_image_dir, "{}_update_after.png".format(view_idx)))
        
            # 1.3.2. back-project and create texture
            # NOTE projection mask = generate mask
            init_texture, project_mask_image, exist_texture = backproject_from_image(
                mesh, faces, new_verts_uvs, cameras, 
                diffused_image, update_mask_image, update_mask_image, init_texture, exist_texture, 
                args.image_size * args.render_simple_factor, args.uv_size, args.fragment_k,
                DEVICE
            )
            
            # update the mesh
            mesh.textures = TexturesUV(
                maps=transforms.ToTensor()(init_texture)[None, ...].permute(0, 2, 3, 1).to(DEVICE),
                faces_uvs=faces.textures_idx[None, ...],
                verts_uvs=new_verts_uvs[None, ...]
            )


        # 1.4. save generated assets
        # save backprojected OBJ file
        save_backproject_obj(
            mesh_dir, "{}.obj".format(view_idx),
            mesh_scale * mesh.verts_packed() + mesh_center if args.use_unnormalized else mesh.verts_packed(),
            faces.verts_idx, new_verts_uvs, faces.textures_idx, init_texture, 
            DEVICE
        )

        # save the intermediate view
        inter_images_tensor, *_ = render(mesh, renderer)
        inter_image = inter_images_tensor[0].cpu()
        inter_image = inter_image.permute(2, 0, 1)
        inter_image = transforms.ToPILImage()(inter_image).convert("RGB")
        inter_image.save(os.path.join(interm_dir, "{}.png".format(view_idx)))

        # save texture mask
        exist_texture_image = exist_texture * 255. 
        exist_texture_image = Image.fromarray(exist_texture_image.cpu().numpy().astype(np.uint8)).convert("L")
        exist_texture_image.save(os.path.join(mesh_dir, "{}_texture_mask.png".format(view_idx)))

    print("=> total generate time: {} s".format(time.time() - start_time))

    # visualize viewpoints
    visualize_principle_viewpoints(output_dir, pre_dist_list, pre_elev_list, pre_azim_list)

    # 2. update texture with RePaint 

    if args.update_steps > 0:

        update_dir = os.path.join(output_dir, "update")
        os.makedirs(update_dir, exist_ok=True)

        init_image_dir = os.path.join(update_dir, "rendering")
        os.makedirs(init_image_dir, exist_ok=True)

        normal_map_dir = os.path.join(update_dir, "normal")
        os.makedirs(normal_map_dir, exist_ok=True)

        mask_image_dir = os.path.join(update_dir, "mask")
        os.makedirs(mask_image_dir, exist_ok=True)

        depth_map_dir = os.path.join(update_dir, "depth")
        os.makedirs(depth_map_dir, exist_ok=True)

        similarity_map_dir = os.path.join(update_dir, "similarity")
        os.makedirs(similarity_map_dir, exist_ok=True)

        inpainted_image_dir = os.path.join(update_dir, "inpainted")
        os.makedirs(inpainted_image_dir, exist_ok=True)

        mesh_dir = os.path.join(update_dir, "mesh")
        os.makedirs(mesh_dir, exist_ok=True)

        interm_dir = os.path.join(update_dir, "intermediate")
        os.makedirs(interm_dir, exist_ok=True)

        dist_list = dist_list[NUM_PRINCIPLE:]
        elev_list = elev_list[NUM_PRINCIPLE:]
        azim_list = azim_list[NUM_PRINCIPLE:]
        sector_list = sector_list[NUM_PRINCIPLE:]
        view_punishments = view_punishments[NUM_PRINCIPLE:]

        similarity_texture_cache = build_similarity_texture_cache_for_all_views(mesh, faces, new_verts_uvs,
            dist_list, elev_list, azim_list,
            args.image_size, args.image_size * args.render_simple_factor, args.uv_size, args.fragment_k,
            DEVICE
        )
        selected_view_ids = []

        print("=> start updating...")
        start_time = time.time()
        for view_idx in range(args.update_steps):
            print("=> processing view {}...".format(view_idx))
            
            # 2.1. render and build masks

            # heuristically select the viewpoints
            dist, elev, azim, sector, selected_view_ids, view_punishments = select_viewpoint(
                selected_view_ids, view_punishments,
                args.update_mode, dist_list, elev_list, azim_list, sector_list, view_idx,
                similarity_texture_cache, exist_texture,
                mesh, faces, new_verts_uvs,
                args.image_size, args.fragment_k,
                init_image_dir, mask_image_dir, normal_map_dir, depth_map_dir, similarity_map_dir,
                DEVICE, False
            )

            (
                view_score,
                renderer, cameras, fragments,
                init_image, normal_map, depth_map, 
                init_images_tensor, normal_maps_tensor, depth_maps_tensor, similarity_tensor, 
                old_mask_image, update_mask_image, generate_mask_image, 
                old_mask_tensor, update_mask_tensor, generate_mask_tensor, all_mask_tensor, quad_mask_tensor,
            ) = render_one_view_and_build_masks(dist, elev, azim, 
                selected_view_ids[-1], view_idx, view_punishments, # => actual view idx and the sequence idx 
                similarity_texture_cache, exist_texture,
                mesh, faces, new_verts_uvs,
                args.image_size, args.fragment_k,
                init_image_dir, mask_image_dir, normal_map_dir, depth_map_dir, similarity_map_dir,
                DEVICE, save_intermediate=True, smooth_mask=args.smooth_mask, view_threshold=args.view_threshold
            )

            # # -------------------- OPTION ZONE ------------------------
            # # still generate for missing regions during refinement
            # # NOTE this could take significantly more time to complete.
            # if args.use_patch:
            #     # 2.2.1 generate missing region
            #     prompt = " the {} view of {}".format(sector, args.prompt) if args.add_view_to_prompt else args.prompt
            #     print("=> generating image for prompt: {}...".format(prompt))

            #     if args.no_repaint:
            #         generate_mask_image = Image.fromarray((np.ones_like(np.array(generate_mask_image)) * 255.).astype(np.uint8))

            #     print("=> generate {} pixels for view {}".format(generate_mask_tensor.sum().int(), view_idx))
            #     generate_image, generate_image_before, generate_image_after = apply_controlnet_depth(controlnet, ddim_sampler, 
            #         init_image.convert("RGBA"), prompt, args.new_strength, args.ddim_steps,
            #         generate_mask_image, keep_mask_image, depth_maps_tensor.permute(1, 2, 0).repeat(1, 1, 3).cpu().numpy(), 
            #         args.a_prompt, args.n_prompt, args.guidance_scale, args.seed, args.eta, 1, DEVICE, args.blend)

            #     generate_image.save(os.path.join(inpainted_image_dir, "{}_new.png".format(view_idx)))
            #     generate_image_before.save(os.path.join(inpainted_image_dir, "{}_new_before.png".format(view_idx)))
            #     generate_image_after.save(os.path.join(inpainted_image_dir, "{}_new_after.png".format(view_idx)))

            #     # 2.2.2. back-project and create texture
            #     # NOTE projection mask = generate mask
            #     init_texture, project_mask_image, exist_texture = backproject_from_image(
            #         mesh, faces, new_verts_uvs, cameras, 
            #         generate_image, generate_mask_image, generate_mask_image, init_texture, exist_texture, 
            #         args.image_size * args.render_simple_factor, args.uv_size, args.fragment_k,
            #         DEVICE
            #     )

            #     project_mask_image.save(os.path.join(mask_image_dir, "{}_new_project.png".format(view_idx)))

            #     # update the mesh
            #     mesh.textures = TexturesUV(
            #         maps=transforms.ToTensor()(init_texture)[None, ...].permute(0, 2, 3, 1).to(DEVICE),
            #         faces_uvs=faces.textures_idx[None, ...],
            #         verts_uvs=new_verts_uvs[None, ...]
            #     )

            #     # 2.2.4. save generated assets
            #     # save backprojected OBJ file
            #     save_backproject_obj(
            #         mesh_dir, "{}_new.obj".format(view_idx),
            #         mesh.verts_packed(), faces.verts_idx, new_verts_uvs, faces.textures_idx, init_texture, 
            #         DEVICE
            #     )

            # # -------------------- OPTION ZONE ------------------------


            # 2.2. update existing region
            prompt = " the {} view of {}".format(sector, args.prompt) if args.add_view_to_prompt else args.prompt
            print("=> updating image for prompt: {}...".format(prompt))

            if not args.no_update and update_mask_tensor.sum() > 0 and update_mask_tensor.sum() / (all_mask_tensor.sum()) > 0.05:
                print("=> update {} pixels for view {}".format(update_mask_tensor.sum().int(), view_idx))
                update_image, update_image_before, update_image_after = apply_controlnet_depth(controlnet, ddim_sampler, 
                    init_image.convert("RGBA"), prompt, args.update_strength, args.ddim_steps,
                    update_mask_image, old_mask_image, depth_maps_tensor.permute(1, 2, 0).repeat(1, 1, 3).cpu().numpy(), 
                    args.a_prompt, args.n_prompt, args.guidance_scale, args.seed, args.eta, 1, DEVICE, args.blend)

                update_image.save(os.path.join(inpainted_image_dir, "{}.png".format(view_idx)))
                update_image_before.save(os.path.join(inpainted_image_dir, "{}_before.png".format(view_idx)))
                update_image_after.save(os.path.join(inpainted_image_dir, "{}_after.png".format(view_idx)))
            else:
                print("=> nothing to update for view {}".format(view_idx))
                update_image = init_image

                old_mask_tensor += update_mask_tensor
                update_mask_tensor[update_mask_tensor == 1] = 0 # HACK nothing to update

                old_mask_image = transforms.ToPILImage()(old_mask_tensor)
                update_mask_image = transforms.ToPILImage()(update_mask_tensor)


            # 2.3. back-project and create texture
            # NOTE projection mask = update mask
            init_texture, project_mask_image, exist_texture = backproject_from_image(
                mesh, faces, new_verts_uvs, cameras, 
                update_image, update_mask_image, update_mask_image, init_texture, exist_texture, 
                args.image_size * args.render_simple_factor, args.uv_size, args.fragment_k,
                DEVICE
            )

            project_mask_image.save(os.path.join(mask_image_dir, "{}_project.png".format(view_idx)))

            # update the mesh
            mesh.textures = TexturesUV(
                maps=transforms.ToTensor()(init_texture)[None, ...].permute(0, 2, 3, 1).to(DEVICE),
                faces_uvs=faces.textures_idx[None, ...],
                verts_uvs=new_verts_uvs[None, ...]
            )

            # 2.4. save generated assets
            # save backprojected OBJ file            
            save_backproject_obj(
                mesh_dir, "{}.obj".format(view_idx),
                mesh_scale * mesh.verts_packed() + mesh_center if args.use_unnormalized else mesh.verts_packed(),
                faces.verts_idx, new_verts_uvs, faces.textures_idx, init_texture, 
                DEVICE
            )

            # save the intermediate view
            inter_images_tensor, *_ = render(mesh, renderer)
            inter_image = inter_images_tensor[0].cpu()
            inter_image = inter_image.permute(2, 0, 1)
            inter_image = transforms.ToPILImage()(inter_image).convert("RGB")
            inter_image.save(os.path.join(interm_dir, "{}.png".format(view_idx)))

            # save texture mask
            exist_texture_image = exist_texture * 255. 
            exist_texture_image = Image.fromarray(exist_texture_image.cpu().numpy().astype(np.uint8)).convert("L")
            exist_texture_image.save(os.path.join(mesh_dir, "{}_texture_mask.png".format(view_idx)))

        print("=> total update time: {} s".format(time.time() - start_time))

        # post-process
        if args.post_process:
            del controlnet
            del ddim_sampler

            inpainting = get_inpainting(DEVICE)
            post_texture = apply_inpainting_postprocess(inpainting, 
                init_texture, 1-exist_texture[None, :, :, None], "", args.uv_size, args.uv_size, DEVICE)

            save_backproject_obj(
                mesh_dir, "{}_post.obj".format(view_idx),
                mesh_scale * mesh.verts_packed() + mesh_center if args.use_unnormalized else mesh.verts_packed(),
                faces.verts_idx, new_verts_uvs, faces.textures_idx, post_texture, 
                DEVICE
            )
    
        # save viewpoints
        save_viewpoints(args, output_dir, dist_list, elev_list, azim_list, selected_view_ids)

        # visualize viewpoints
        visualize_refinement_viewpoints(output_dir, selected_view_ids, dist_list, elev_list, azim_list)