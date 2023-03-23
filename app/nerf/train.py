import logging
from typing import Tuple

from PIL import Image
from flax.training import checkpoints
from flax.training.train_state import TrainState
import jax
import jax.numpy as jnp
import jax.random as jran
import numpy as np
import optax
from tqdm import tqdm
import tyro

from models.nerfs import make_nerf_ngp
from models.renderers import march_rays, render_image
from utils import common, data
from utils.args import NeRFArgs
from utils.types import AABB, PinholeCamera, RayMarchingOptions, RenderingOptions, RigidTransformation


@common.jit_jaxfn_with(static_argnames=["raymarch_options"])
def train_step(
        state: TrainState,
        aabb: AABB,
        camera: PinholeCamera,
        all_xys: jax.Array,
        all_rgbs: jax.Array,
        all_transforms: jax.Array,
        raymarch_options: RayMarchingOptions,
        render_options: RenderingOptions,
        perm: jax.Array
    ):
    # TODO:
    #   merge this and `models.renderers.make_rays_worldspace` as a single function
    def make_rays_worldspace() -> Tuple[jax.Array, jax.Array]:
        # [N, 2]
        xys = all_xys[perm]
        # [N, 3]
        xyzs = jnp.concatenate([xys, jnp.ones((xys.shape[0], 1))], axis=-1)
        # [N, 1]
        d_cam_xs = xyzs[:, 0:1]
        d_cam_xs = ((d_cam_xs + 0.5) - camera.W/2)
        # [N, 1]
        d_cam_ys = xyzs[:, 1:2]
        d_cam_ys = -((d_cam_ys + 0.5) - camera.H/2)
        # [N, 1]
        d_cam_zs = -camera.focal * xyzs[:, 2:3]
        # [N, 3]
        d_cam = jnp.concatenate([d_cam_xs, d_cam_ys, d_cam_zs], axis=-1)
        d_cam /= jnp.linalg.norm(d_cam, axis=-1, keepdims=True) + 1e-15

        # indices of views, used to retrieve transformation information for each ray
        view_idcs = perm // (camera.H * camera.W)
        # [N, 3]
        o_world = all_transforms[view_idcs, -3:]  # WARN: using `perm` instead of `view_idcs` here
                                                  # will silently clip the out-of-bounds indices.
                                                  # REF:
                                                  #   <https://jax.readthedocs.io/en/latest/notebooks/Common_Gotchas_in_JAX.html#out-of-bounds-indexing>
        # [N, 3, 3]
        R_cws = all_transforms[view_idcs, :9].reshape(-1, 3, 3)
        # [N, 3]
        # equavalent to performing `d_cam[i] @ R_cws[i].T` for each i in [0, N)
        d_world = (d_cam[:, None, :] * R_cws).sum(-1)

        # d_cam was normalized already, normalize d_world just to be sure
        d_world /= jnp.linalg.norm(d_world, axis=-1, keepdims=True) + 1e-15

        return o_world, d_world

    def loss(params, gt):
        o_world, d_world = make_rays_worldspace()
        preds = march_rays(
            o_world,
            d_world,
            aabb,
            render_options.use_white_bg,
            raymarch_options,
            {"params": params},
            state.apply_fn,
        )
        loss = jnp.square(preds - gt).mean()
        return loss

    loss_grad_fn = jax.value_and_grad(loss)

    loss, grads = loss_grad_fn(state.params, all_rgbs[perm])
    state = state.apply_gradients(grads=grads)
    metrics = {
        "loss": loss * perm.shape[0],
    }
    return state, metrics


def train_epoch(
        aabb: AABB,
        scene_metadata: data.SceneMetadata,
        raymarch_options: RayMarchingOptions,
        render_options: RenderingOptions,
        permutation: data.Dataset,
        total_batches: int,
        state: TrainState,
        ep_log: int
    ):
    loss, running_loss = 0, -1
    for perm in (pbar := tqdm(permutation, total=total_batches, desc="Training epoch#{:03d}".format(ep_log), bar_format=common.tqdm_format)):
        state, metrics = train_step(
            state,
            aabb,
            scene_metadata.camera,
            scene_metadata.all_xys,
            scene_metadata.all_rgbs,
            scene_metadata.all_transforms,
            raymarch_options,
            render_options,
            perm,
        )
        loss += metrics["loss"]
        loss_log = metrics["loss"] / perm.shape[0]
        if running_loss < 0:
            running_loss = loss_log
        else:
            running_loss = running_loss * 0.99 + 0.01 * loss_log
        pbar.set_description_str("Training epoch#{:03d} loss={:.3e}".format(ep_log, running_loss))
    return loss, state


def train(args: NeRFArgs, logger: logging.Logger):
    args.exp_dir.mkdir(parents=True)
    args.exp_dir.joinpath("config.yaml").write_text(tyro.to_yaml(args))
    logger.info("configurations saved to '{}'".format(args.exp_dir.joinpath("config.yaml")))

    dtype = getattr(jnp, "float{}".format(args.common.prec))
    logger.setLevel(args.common.logging.upper())

    # deterministic
    K = common.set_deterministic(args.common.seed)

    # model parameters
    K, key = jran.split(K, 2)
    aabb = [[-args.bound, args.bound]] * 3
    model, init_input = (
        make_nerf_ngp(aabb=aabb),
        (jnp.zeros((1, 3), dtype=dtype), jnp.zeros((1, 3), dtype=dtype))
    )
    variables = model.init(key, *init_input)
    if args.common.display_model_summary:
        print(model.tabulate(key, *init_input))

    lr_sch = optax.exponential_decay(
        init_value=args.train.lr,
        transition_steps=10_000,
        decay_rate=1/3,  # decay to `1/3 * init_lr` after `transition_steps` steps
        transition_begin=20_000,  # hold the initial lr value for the initial 20k steps
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
            "density_mlp": True,
            "rgb_mlp": True,
            "position_encoder": False,
        },
    )

    # training state
    state = TrainState.create(
        apply_fn=model.apply,
        # unfreeze the frozen dict so that below weight_decay mask can apply, see:
        #   <https://github.com/deepmind/optax/issues/160>
        #   <https://github.com/google/flax/issues/1223>
        params=variables["params"].unfreeze(),
        tx=optimizer,
    )

    # data
    scene_metadata_train, _ = data.make_nerf_synthetic_scene_metadata(
        rootdir=args.data_root,
        split="train",
        use_white_bg=args.render.use_white_bg,
    )

    # val_transform = RigidTransformation(
    #     rotation=scene_metadata_train.all_transforms[3, :9].reshape(3, 3),
    #     translation=scene_metadata_train.all_transforms[3, -3:].reshape(3),
    # )

    scene_metadata_val, val_views = data.make_nerf_synthetic_scene_metadata(
        rootdir=args.data_root,
        split="val",
        use_white_bg=args.render.use_white_bg,
    )

    logger.info("starting training")
    # training loop
    for ep in range(args.train.n_epochs):
        ep_log = ep + 1
        K, key = jran.split(K, 2)
        permutation = data.make_permutation_dataset(
            key,
            size=scene_metadata_train.all_xys.shape[0],
            shuffle=True
        )\
            .batch(args.render.ray_chunk_size, drop_remainder=True)\
            .repeat(args.data.loop)

        try:
            loss, state = train_epoch(
                aabb=aabb,
                scene_metadata=scene_metadata_train,
                raymarch_options=args.raymarch,
                render_options=args.render,
                permutation=permutation.take(args.train.n_batches).as_numpy_iterator(),
                total_batches=args.train.n_batches,
                state=state,
                ep_log=ep_log,
            )
        except KeyboardInterrupt:
            logger.warn("aborted at epoch {}".format(ep_log))
            logger.info("saving training state ... ")
            ckpt_name = checkpoints.save_checkpoint(args.exp_dir, state, step="ep{}aborted".format(ep_log), overwrite=True, keep=2**30)
            logger.info("training state of epoch {} saved to: {}".format(ep_log, ckpt_name))
            logger.info("exiting cleanly ...")
            exit()

        logger.info("epoch#{:03d}: per-pixel loss={:.2e}".format(ep_log, loss / scene_metadata_train.all_xys.shape[0]))

        logger.info("saving training state ... ")
        ckpt_name = checkpoints.save_checkpoint(args.exp_dir, state, step=ep_log, overwrite=True, keep=2**30)
        logger.info("training state of epoch {} saved to: {}".format(ep_log, ckpt_name))

        # validate on `args.val_num` random camera transforms
        K, key = jran.split(K, 2)
        for val_i in jran.choice(
                key,
                jnp.arange(len(val_views)),
                (min(args.val_num, len(val_views)),),
                replace=False,
            ):
            logger.info("validating on {}".format(val_views[val_i].file))
            val_transform = RigidTransformation(
                rotation=scene_metadata_val.all_transforms[val_i, :9].reshape(3, 3),
                translation=scene_metadata_val.all_transforms[val_i, -3:].reshape(3),
            )
            image = render_image(
                aabb=aabb,
                camera=scene_metadata_val.camera,
                transform_cw=val_transform,
                options=args.render,
                raymarch_options=args.raymarch,
                param_dict={"params": state.params},
                nerf_fn=state.apply_fn,
            )
            gt_image = Image.open(val_views[val_i].file)
            gt_image = np.asarray(gt_image) / 255
            gt_image = data.blend_alpha_channel(gt_image, use_white_bg=args.render.use_white_bg)
            gt_image = (gt_image * 255).astype(jnp.uint8)
            logger.info("{}: psnr={}".format(val_views[val_i].file, data.psnr(gt_image, image)))
            dest = args.exp_dir\
                .joinpath("validataion")\
                .joinpath("ep{}".format(ep_log))\
                .joinpath("{:03d}.png".format(val_i))
            dest.parent.mkdir(parents=True, exist_ok=True)
            logger.info("saving image to {}".format(dest))
            Image.fromarray(np.asarray(image)).save(dest)
