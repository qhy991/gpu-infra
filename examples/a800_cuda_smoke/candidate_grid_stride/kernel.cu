#include <cuda_runtime.h>

#include <algorithm>
#include <cstddef>

namespace {

__global__ void vector_add(float* output, const float* left, const float* right,
                           std::size_t count) {
  const std::size_t first = blockIdx.x * blockDim.x + threadIdx.x;
  const std::size_t stride = blockDim.x * gridDim.x;
  for (std::size_t index = first; index < count; index += stride) {
    output[index] = left[index] + right[index];
  }
}

}  // namespace

extern "C" void launch_candidate(float* output, const float* left,
                                 const float* right, std::size_t count,
                                 cudaStream_t stream) {
  constexpr int threads = 256;
  const int full_grid = static_cast<int>((count + threads - 1) / threads);
  const int blocks = std::min(full_grid, 4096);
  vector_add<<<blocks, threads, 0, stream>>>(output, left, right, count);
}
