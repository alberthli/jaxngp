from typing import Tuple

import chex
import jax
import jax.numpy as jnp

from . import impl

def march_rays(
    # static
    total_samples: int,
    diagonal_n_steps: int,
    K: int,
    G: int,
    bound: float,
    stepsize_portion: float,

    # inputs
    rays_o: jax.Array,
    rays_d: jax.Array,
    t_starts: jax.Array,
    t_ends: jax.Array,
    noises: jax.Array,
    occupancy_bitfield: jax.Array,
) -> Tuple[jax.Array, jax.Array, jax.Array, jax.Array, jax.Array]:
    """
    Given a pack of rays (`rays_o`, `rays_d`), their intersection time with the scene bounding box
    (`t_starts`, `t_ends`), and an occupancy grid (`occupancy_bitfield`), generate samples along
    each ray.

    Inputs:
        total_samples `int`: ,
        diagonal_n_steps `int`: the length of a minimal ray marching step is calculated internally
                                as:
                                    Δ𝑡 := √3 / diagonal_n_steps;
                                the NGP paper uses diagonal_n_steps=1024 (as described in appendix
                                E.1).
        K `int`: total number of cascades of `occupancy_bitfield`
        G `int`: occupancy grid resolution, the paper uses 128 for every cascade
        bound `float`: the half length of the longest axis of the scene’s bounding box,
                       e.g. the `bound` of the bounding box [-1, 1]^3 is 1
        stepsize_portion: next step size is calculated as t * stepsize_portion, the paper uses 1/256

        rays_o `[n_rays, 3]`: ray origins
        rays_d `[n_rays, 3]`: **unit** vectors representing ray directions
        t_starts `[n_rays]`: time of the ray entering the scene bounding box
        t_ends `[n_rays]`: time of the ray leaving the scene bounding box
        noises `broadcastable to [n_rays]`: noises to perturb the starting point of ray marching
        occupancy_bitfield `[K*(G**3)//8]`: the occupancy grid represented as a bit array, grid
                                            cells are laid out in Morton (z-curve) order, as
                                            described in appendix E.2 of the NGP paper

    Returns:
        measured_batch_size_before_compaction `int`: total number of generated samples of all rays
        rays_n_samples `[n_rays]`: number of samples of each ray, its sum is `total_samples`
                                   referenced below
        rays_sample_startidx `[n_rays]`: indices of each ray's first sample
        xyzs `[total_samples, 3]`: spatial coordinates of the generated samples, invalid array
                                   locations are masked out with zeros
        dirs `[total_samples, 3]`: spatial coordinates of the generated samples, invalid array
                                   locations are masked out with zeros.
        dss `[total_samples]`: `ds`s of each sample, for a more detailed explanation of this
                               notation, see documentation of function `volrendjax.integrat_rays`,
                               invalid array locations are masked out with zeros.
        z_vals `[total_samples]`: samples' distances to their origins, invalid array
                                  locations are masked out with zeros.
    """
    n_rays, _ = rays_o.shape
    noises = jnp.broadcast_to(noises, (n_rays,))

    chex.assert_scalar_positive(total_samples)
    chex.assert_scalar_positive(diagonal_n_steps)
    chex.assert_scalar_positive(K)
    chex.assert_scalar_positive(G)
    chex.assert_scalar_positive(bound)
    chex.assert_scalar_non_negative(stepsize_portion)

    measured_batch_size_before_compaction, rays_n_samples, rays_sample_startidx, xyzs, dirs, dss, z_vals = impl.march_rays_p.bind(
        # arrays
        rays_o,
        rays_d,
        t_starts,
        t_ends,
        noises,
        occupancy_bitfield,

        # static args
        total_samples=total_samples,
        diagonal_n_steps=diagonal_n_steps,
        K=K,
        G=G,
        bound=bound,
        stepsize_portion=stepsize_portion,
    )

    return measured_batch_size_before_compaction[0], rays_n_samples, rays_sample_startidx, xyzs, dirs, dss, z_vals


def march_rays_inference(
    # static
    diagonal_n_steps: int,
    K: int,
    G: int,
    march_steps_cap: int,
    bound: float,
    stepsize_portion: float,

    # inputs
    rays_o: jax.Array,
    rays_d: jax.Array,
    t_starts: jax.Array,
    t_ends: jax.Array,
    occupancy_bitfield: jax.Array,
    counter: jax.Array,
    terminated: jax.Array,
    indices: jax.Array,
):
    counter, indices, n_samples, t_starts, xyzdirs, dss, z_vals = impl.march_rays_inference_p.bind(
        rays_o,
        rays_d,
        t_starts,
        t_ends,
        occupancy_bitfield,
        counter,
        terminated,
        indices,

        diagonal_n_steps=diagonal_n_steps,
        K=K,
        G=G,
        march_steps_cap=march_steps_cap,
        bound=bound,
        stepsize_portion=stepsize_portion,
    )
    return counter, indices, n_samples, t_starts, xyzdirs, dss, z_vals
