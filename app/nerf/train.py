import dataclasses
import functools
import gc
import time
from typing import Any, Dict, List, Tuple

from flax.training import checkpoints
import jax
import jax.numpy as jnp
import jax.random as jran
import optax
import tyro

from models.nerfs import make_nerf_ngp, make_skysphere_background_model_ngp
from models.renderers import render_image_inference
from utils import common, data
from utils.args import NeRFTrainingArgs
from utils.types import (
    NeRFBatchConfig,
    NeRFState,
    OccupancyDensityGrid,
    RenderedImage,
    RigidTransformation,
    SceneData,
)

from ._utils import train_step


def train_epoch(
    KEY: jran.KeyArray,
    state: NeRFState,
    scene: SceneData,
    n_batches: int,
    total_samples: int,
    ep_log: int,
    total_epochs: int,
    logger: common.Logger,
) -> Tuple[NeRFState, Dict[str, Any]]:
    n_processed_rays = 0
    total_loss = None
    interrupted = False

    try:
        for _ in (pbar := common.tqdm(range(n_batches), desc="Training epoch#{:03d}/{:d}".format(ep_log, total_epochs))):
            KEY, key_perm, key_train_step = jran.split(KEY, 3)
            perm = jran.choice(key_perm, scene.meta.n_pixels, shape=(state.batch_config.n_rays,), replace=True)
            state, metrics = train_step(
                KEY=key_train_step,
                state=state,
                total_samples=total_samples,
                scene=scene,
                perm=perm,
            )
            n_processed_rays += state.batch_config.n_rays
            loss = metrics["loss"]
            if total_loss is None:
                total_loss = loss
            else:
                total_loss = jax.tree_util.tree_map(
                    lambda total, new: total + new * state.batch_config.n_rays,
                    total_loss,
                    loss,
                )

            pbar.set_description_str(
                desc="Training epoch#{:03d}/{:d} batch_size={}/{} samp./ray={:.1f}/{:.1f} n_rays={} loss:{{rgb={:.2e}({:.2f}dB),tv={:.2e}}}".format(
                    ep_log,
                    total_epochs,
                    metrics["measured_batch_size"],
                    metrics["measured_batch_size_before_compaction"],
                    state.batch_config.running_mean_effective_samples_per_ray,
                    state.batch_config.running_mean_samples_per_ray,
                    state.batch_config.n_rays,
                    loss["rgb"],
                    data.linear_to_db(loss["rgb"], maxval=1),
                    loss["total_variation"],
                )
            )

            if state.should_call_update_ogrid:
                # update occupancy grid
                for cas in range(state.scene_meta.cascades):
                    KEY, key = jran.split(KEY, 2)
                    state = state.update_ogrid_density(
                        KEY=key,
                        cas=cas,
                        update_all=bool(state.should_update_all_ogrid_cells),
                        max_inference=total_samples,
                    )
                state = state.threshold_ogrid()

            state = state.update_batch_config(
                new_measured_batch_size=metrics["measured_batch_size"],
                new_measured_batch_size_before_compaction=metrics["measured_batch_size_before_compaction"],
            )
            if state.should_commit_batch_config:
                state = state.replace(batch_config=state.batch_config.commit(total_samples))

            if state.should_write_batch_metrics:
                logger.write_scalar("batch/↓loss (rgb)", loss["rgb"], state.step)
                logger.write_scalar("batch/↑estimated PSNR (db)", data.linear_to_db(loss["rgb"], maxval=1), state.step)
                logger.write_scalar("batch/↓loss (total variation)", loss["total_variation"], state.step)
                logger.write_scalar("batch/effective batch size (not compacted)", metrics["measured_batch_size_before_compaction"], state.step)
                logger.write_scalar("batch/↑effective batch size (compacted)", metrics["measured_batch_size"], state.step)
                logger.write_scalar("rendering/↓effective samples per ray", state.batch_config.mean_effective_samples_per_ray, state.step)
                logger.write_scalar("rendering/↓marched samples per ray", state.batch_config.mean_samples_per_ray, state.step)
                logger.write_scalar("rendering/↑number of rays", state.batch_config.n_rays, state.step)
    except (InterruptedError, KeyboardInterrupt):
        interrupted = True

    return state, {
        "total_loss": total_loss,
        "n_processed_rays": n_processed_rays,
        "interrupted": interrupted,
    }


def train(KEY: jran.KeyArray, args: NeRFTrainingArgs, logger: common.Logger):
    if args.exp_dir.exists():
        logger.error("specified experiment directory '{}' already exists".format(args.exp_dir))
        exit(1)
    logs_dir = args.exp_dir.joinpath("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = common.setup_logging(
        "nerf.train",
        file=logs_dir.joinpath("train.log"),
        with_tensorboard=True,
        level=args.common.logging.upper(),
        file_level="DEBUG",
    )
    args.exp_dir.joinpath("config.yaml").write_text(tyro.to_yaml(args))
    logger.write_hparams(dataclasses.asdict(args))
    logger.info("configurations saved to '{}'".format(args.exp_dir.joinpath("config.yaml")))

    # data
    logger.info("loading training frames")
    scene_train, _ = data.load_scene(
        srcs=args.frames_train,
        scene_options=args.scene,
    )
    logger.debug("sharpness_min={:.3f}, sharpness_max={:.3f}".format(*scene_train.meta.sharpness_range))

    if len(args.frames_val) > 0:
        logger.info("loading validation frames")
        scene_val, val_views = data.load_scene(
            srcs=args.frames_val,
            scene_options=args.scene,
        )
        assert scene_train.meta.replace(frames=None) == scene_val.meta.replace(frames=None)
    else:
        logger.warn("got empty validation set, this run will not do validation")

    scene_meta = scene_train.meta

    # model parameters
    nerf_model, init_input = (
        make_nerf_ngp(bound=scene_meta.bound, inference=False, tv_scale=args.train.tv_scale),
        (jnp.zeros((1, 3), dtype=jnp.float32), jnp.zeros((1, 3), dtype=jnp.float32))
    )
    KEY, key = jran.split(KEY, 2)
    nerf_variables = nerf_model.init(key, *init_input)
    if args.common.summary:
        print(nerf_model.tabulate(key, *init_input))

    if scene_meta.bg:
        bg_model, init_input = (
            make_skysphere_background_model_ngp(bound=scene_meta.bound),
            (jnp.zeros((1, 3), dtype=jnp.float32), jnp.zeros((1, 3), dtype=jnp.float32))
        )
        KEY, key = jran.split(KEY, 2)
        bg_variables = bg_model.init(key, *init_input)

    lr_sch = optax.exponential_decay(
        init_value=args.train.lr,
        transition_steps=10_000,
        decay_rate=1/3,  # decay to `1/3 * init_lr` after `transition_steps` steps
        staircase=True,  # use integer division to determine lr drop step
        transition_begin=10_000,  # hold the initial lr value for the initial 10k steps (but first lr drop happens at 20k steps because `staircase` is specified)
        end_value=args.train.lr / 100,  # stop decaying at `1/100 * init_lr`
    )
    optimizer = optax.adamw(
        learning_rate=lr_sch,
        b1=0.9,
        b2=0.99,
        # paper:
        #   the small value of 𝜖 = 10^{−15} can significantly accelerate the convergence of the
        #   hash table entries when their gradients are sparse and weak.
        eps=1e-15,
        eps_root=1e-15,
        # In NeRF experiments, the network can converge to a reasonably low loss during the
        # frist ~50k training steps (with 1024 rays per batch and 1024 samples per ray), but the
        # loss becomes NaN after about 50~150k training steps.
        # paper:
        #   To prevent divergence after long training periods, we apply a weak L2 regularization
        #   (factor 10^{−6}) to the neural network weights, ...
        weight_decay=1e-6,
        # paper:
        #   ... to the neural network weights, but not to the hash table entries.
        mask={
            "nerf": {
                "density_mlp": True,
                "rgb_mlp": True,
                "position_encoder": False,
            },
            "bg": scene_meta.bg,
        },
    )

    # training state
    state = NeRFState.create(
        ogrid=OccupancyDensityGrid.create(
            cascades=scene_meta.cascades,
            grid_resolution=args.raymarch.density_grid_res,
        ),
        batch_config=NeRFBatchConfig.create(
            mean_effective_samples_per_ray=args.raymarch.diagonal_n_steps,
            mean_samples_per_ray=args.raymarch.diagonal_n_steps,
            n_rays=args.train.bs // args.raymarch.diagonal_n_steps,
        ),
        raymarch=args.raymarch,
        render=args.render,
        scene_options=args.scene,
        scene_meta=scene_meta,
        # unfreeze the frozen dict so that the weight_decay mask can apply, see:
        #   <https://github.com/deepmind/optax/issues/160>
        #   <https://github.com/google/flax/issues/1223>
        nerf_fn=nerf_model.apply,
        bg_fn=bg_model.apply if scene_meta.bg else None,
        params={
            "nerf": nerf_variables["params"].unfreeze(),
            "bg": bg_variables["params"].unfreeze() if scene_meta.bg else None,
        },
        tx=optimizer,
    )
    state = state.mark_untrained_density_grid()

    logger.info("starting training")
    # training loop
    for ep in range(args.train.n_epochs):
        gc.collect()

        ep_log = ep + 1

        KEY, key = jran.split(KEY, 2)
        state, metrics = train_epoch(
            KEY=key,
            state=state,
            scene=scene_train,
            n_batches=args.train.n_batches,
            total_samples=args.train.bs,
            ep_log=ep_log,
            total_epochs=args.train.n_epochs,
            logger=logger,
        )
        if metrics["interrupted"]:
            logger.warn("aborted at epoch {}".format(ep_log))
            logger.info("saving training state ... ")
            ckpt_name = checkpoints.save_checkpoint(args.exp_dir, state, step="ep{}aborted".format(ep_log), overwrite=True, keep=2**30)
            logger.info("training state of epoch {} saved to: {}".format(ep_log, ckpt_name))
            logger.info("exiting cleanly ...")
            exit()

        mean_loss = jax.tree_util.tree_map(
            lambda val: val / metrics["n_processed_rays"],
            metrics["total_loss"],
        )
        logger.info("epoch#{:03d}: loss:{{rgb={:.3e}({:.2f}dB),tv={:.3e}}}".format(
            ep_log,
            mean_loss["rgb"],
            data.linear_to_db(mean_loss["rgb"], maxval=1,),
            mean_loss["total_variation"],
        ))
        logger.write_scalar("epoch/↓loss (rgb)", mean_loss["rgb"], step=ep_log)
        logger.write_scalar("epoch/↑estimated PSNR (db)", data.linear_to_db(mean_loss["rgb"], maxval=1), step=ep_log)
        logger.write_scalar("batch/↓loss (total variation)", mean_loss["total_variation"], state.step)

        logger.info("saving training state ... ")
        ckpt_name = checkpoints.save_checkpoint(
            args.exp_dir,
            state,
            step=ep_log * args.train.n_batches,
            overwrite=True,
            keep=args.train.keep,
            keep_every_n_steps=args.train.keep_every_n_steps,
        )
        logger.info("training state of epoch {} saved to: {}".format(ep_log, ckpt_name))

        if ep_log % args.train.validate_every == 0:
            if len(args.frames_val) == 0:
                logger.warn("empty validation set, skipping validation")
                continue

            val_start_time = time.time()
            rendered_images: List[RenderedImage] = []
            state_eval = state\
                .replace(raymarch=args.raymarch_eval)\
                .replace(render=args.render_eval)
            for val_i, val_view in enumerate(common.tqdm(val_views, desc="validating")):
                logger.debug("validating on {}".format(val_view.file))
                val_transform = RigidTransformation(
                    rotation=scene_val.all_transforms[val_i, :9].reshape(3, 3),
                    translation=scene_val.all_transforms[val_i, -3:].reshape(3),
                )
                KEY, key = jran.split(KEY, 2)
                bg, rgb, depth, _ = data.to_cpu(render_image_inference(
                    KEY=key,
                    transform_cw=val_transform,
                    state=state_eval,
                ))
                rendered_images.append(RenderedImage(
                    bg=bg,
                    rgb=rgb,
                    depth=depth,  # call to data.mono_to_rgb is deferred below so as to minimize impact on rendering speed
                ))
            val_end_time = time.time()
            logger.write_scalar(
                tag="validation/↓rendering time (ms) per image",
                value=(val_end_time - val_start_time) / len(rendered_images) * 1000,
                step=ep_log,
            )

            gt_rgbs_f32 = list(map(
                lambda val_view, rendered_image: data.blend_rgba_image_array(
                    val_view.image_rgba_u8.astype(jnp.float32) / 255,
                    rendered_image.bg,
                ),
                val_views,
                rendered_images,
            ))

            logger.debug("calculating psnr")
            mean_psnr = sum(map(
                data.psnr,
                map(data.f32_to_u8, gt_rgbs_f32),
                map(lambda ri: ri.rgb, rendered_images),
            )) / len(rendered_images)
            logger.info("validated {} images, mean psnr={}".format(len(rendered_images), mean_psnr))
            logger.write_scalar("validation/↑mean psnr", mean_psnr, step=ep_log)

            logger.debug("writing images to tensorboard")
            concatenate_fn = lambda gt, rendered_image: data.add_border(functools.reduce(
                functools.partial(
                    data.side_by_side,
                    H=scene_meta.camera.H,
                    W=scene_meta.camera.W,
                ),
                [
                    gt,
                    rendered_image.rgb,
                    common.compose(data.mono_to_rgb, data.f32_to_u8)(rendered_image.depth),
                ],
            ))
            logger.write_image(
                tag="validation/[gt|rendered|depth]",
                image=list(map(
                    concatenate_fn,
                    map(data.f32_to_u8, gt_rgbs_f32),
                    rendered_images,
                )),
                step=ep_log,
                max_outputs=len(rendered_images),
            )

            del state_eval
            del gt_rgbs_f32
            del rendered_images
