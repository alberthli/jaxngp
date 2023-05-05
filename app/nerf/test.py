from typing import List

from PIL import Image
from flax.training import checkpoints
import jax
import jax.random as jran
import jax.numpy as jnp
import numpy as np
from tqdm import tqdm

from models.nerfs import make_nerf_ngp, make_skysphere_background_model_ngp
from models.renderers import render_image_inference
from utils import common, data
from utils.args import NeRFTestingArgs
from utils.types import NeRFState, RenderedImage, RigidTransformation


def test(KEY: jran.KeyArray, args: NeRFTestingArgs, logger: common.Logger):
    logs_dir = args.exp_dir.joinpath("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = common.setup_logging(
        "nerf.test",
        file=logs_dir.joinpath("test.log"),
        level=args.common.logging.upper(),
        file_level="DEBUG",
    )
    if not args.ckpt.exists():
        logger.error("specified checkpoint '{}' does not exist".format(args.ckpt))
        exit(1)

    logger.info("loading testing frames")
    scene_data, test_views = data.load_scene(
        srcs=args.frames,
        world_scale=args.scene.world_scale,
        image_scale=args.scene.image_scale,
    )

    # load parameters
    state: NeRFState = checkpoints.restore_checkpoint(
        args.ckpt,
        target=NeRFState.empty(
            raymarch=args.raymarch,
            render=args.render,
            scene_options=args.scene,
            scene_meta=scene_data.meta,
            nerf_fn=make_nerf_ngp(bound=scene_data.meta.bound).apply,
            bg_fn=make_skysphere_background_model_ngp(bound=scene_data.meta.bound).apply if args.scene.with_bg else None,
        ),
    )
    # WARN:
    #   flax.checkpoints.restore_checkpoint() returns a pytree with all arrays of numpy's array type,
    #   which slows down inference.  use jax.device_put() to convert them to jax's DeviceArray type.
    # REF: <https://github.com/google/flax/discussions/1199#discussioncomment-635132>
    state = jax.device_put(state)

    rendered_images: List[RenderedImage] = []
    try:
        logger.info("starting testing (totally {} image(s) to test)".format(len(test_views)))
        for test_i, test_view in enumerate(tqdm(test_views, desc="testing", bar_format=common.tqdm_format)):
            logger.debug("testing on {}".format(test_view))
            transform = RigidTransformation(
                rotation=scene_data.all_transforms[test_i, :9].reshape(3, 3),
                translation=scene_data.all_transforms[test_i, -3:].reshape(3),
            )
            KEY, key = jran.split(KEY, 2)
            bg, rgb, depth = render_image_inference(
                KEY=key,
                transform_cw=transform,
                state=state,
            )
            rendered_images.append(RenderedImage(
                bg=data.to_cpu(bg),
                rgb=data.to_cpu(rgb),
                depth=data.to_cpu(depth),
            ))
    except KeyboardInterrupt:
        logger.warn("keyboard interrupt, tested {} images".format(len(rendered_images)))

    gt_rgbs_f32 = map(
        lambda test_view, rendered_image: data.blend_rgba_image_array(
            test_view.image_rgba_u8.astype(jnp.float32) / 255,
            rendered_image.bg,
        ),
        test_views,
        rendered_images,
    )
    logger.debug("calculating psnr")
    mean_psnr = sum(map(
        data.psnr,
        map(data.f32_to_u8, gt_rgbs_f32),
        map(lambda ri: ri.rgb, rendered_images),
    )) / len(rendered_images)
    logger.info("tested {} images, mean psnr={}".format(len(rendered_images), mean_psnr))

    save_dest = args.exp_dir.joinpath(args.split)
    save_dest.mkdir(parents=True, exist_ok=True)
    if "image" in args.save_as:
        dest_rgb = save_dest.joinpath("rgb")
        dest_depth = save_dest.joinpath("depth")

        dest_rgb.mkdir(parents=True, exist_ok=True)
        dest_depth.mkdir(parents=True, exist_ok=True)

        logger.debug("saving as images")
        for save_i, img in enumerate(tqdm(rendered_images, desc="saving images", bar_format=common.tqdm_format)):
            Image.fromarray(np.asarray(img.rgb)).save(dest_rgb.joinpath("{:03d}.png".format(save_i)))
            Image.fromarray(np.asarray(img.depth)).save(dest_depth.joinpath("{:03d}.png".format(save_i)))

    if "video" in args.save_as:
        dest_rgb_video = save_dest.joinpath("rgb.mp4")
        dest_depth_video = save_dest.joinpath("depth.mp4")

        logger.debug("saving predicted color images as a video at '{}'".format(dest_rgb_video))
        data.write_video(
            save_dest.joinpath("rgb.mp4"),
            map(lambda img: img.rgb, rendered_images),
        )

        logger.debug("saving predicted depths as a video at '{}'".format(dest_depth_video))
        data.write_video(
            save_dest.joinpath("depth.mp4"),
            map(lambda img: img.depth, rendered_images),
        )

    return mean_psnr
