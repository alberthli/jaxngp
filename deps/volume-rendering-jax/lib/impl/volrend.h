#pragma once

#include <cuda_runtime_api.h>
#include <cstdint>

#define STRINGIFY(x) #x
#define STR(x) STRINGIFY(x)
#define FILE_LINE __FILE__ ":" STR(__LINE__)
#define CUDA_CHECK_THROW(x)                                                                        \
    do {                                                                                           \
        cudaError_t result = x;                                                                    \
        if (result != cudaSuccess)                                                                 \
            throw std::runtime_error(                                                              \
                std::string(FILE_LINE " " #x " failed with error ")                                \
                + cudaGetErrorString(result));                                                     \
    } while(0)

#define SQRT3 1.732050807568877293527446341505872367

namespace volrendjax {

// Static parameters passed to `integrate_rays` and `integrate_rays_backward`
struct IntegratingDescriptor {
    // number of input rays
    std::uint32_t n_rays;

    // sum of number of samples of each ray
    std::uint32_t total_samples;
};

// Static parameters passed to `march_rays`
struct MarchingDescriptor {
    // number of input rays
    std::uint32_t n_rays;

    // maximum number of samples to generate per ray
    std::uint32_t max_n_samples;

    // paper: we maintain a cascade of 𝐾 multiscale occupancy grids, where 𝐾 = 1 for all synthetic
    // NeRF scenes (single grid) and 𝐾 ∈ [1, 5] for larger real-world scenes (up to 5 grids,
    // depending on scene size)
    std::uint32_t K;

    // density grid resolution, the paper uses 128 for every cascade
    std::uint32_t G;

    // the half-length of the longest axis of the scene’s bounding box.  E.g. the `bound` of the
    // bounding box [-1, 1]^3 is 1.
    float bound;

    // next step size is calculated as:
    //      clamp(z_val[i] * stepsize_portion, sqrt3/1024.f, 2 * bound * sqrt3/1024.f)
    // where bound is the half-length of the largest axis of the scene’s bounding box, as mentioned
    // in Appendix E.1 of the NGP paper (the intercept theorem)
    float stepsize_portion;
};

struct Morton3DDescriptor {
    // number of entries to process
    std::uint32_t length;
};

// Static parameters passed to `pack_density_into_bits`
struct PackbitsDescriptor {
    std::uint32_t n_bytes;
    // grid cells with densityes larger than this threshold is considered occupied
    float density_threshold;
};

// functions to register
void pack_density_into_bits(
    cudaStream_t stream,
    void **buffers,
    const char *opaque,
    std::size_t opaque_len
);

void march_rays(
    cudaStream_t stream,
    void **buffers,
    const char *opaque,
    std::size_t opaque_len
);

/// morton3d
void morton3d(
    cudaStream_t stream,
    void **buffers,
    const char *opaque,
    std::size_t opaque_len
);
void morton3d_invert(
    cudaStream_t stream,
    void **buffers,
    const char *opaque,
    std::size_t opaque_len
);

void integrate_rays(
    cudaStream_t stream,
    void **buffers,
    const char *opaque,
    std::size_t opaque_len
);

void integrate_rays_backward(
    cudaStream_t stream,
    void **buffers,
    const char *opaque,
    std::size_t opaque_len
);


#ifdef __CUDACC__
inline __device__ float clampf(float val, float lo, float hi) {
    return fminf(fmaxf(val, lo), hi);
}
inline __device__ int clampi(int val, int lo, int hi) {
    return min(max(val, lo), hi);
}
inline __host__ __device__ float signf(const float x) {
    return copysignf(1.0, x);
}
#endif

}  // namespace volrendjax
