#include <cuda_runtime.h>

#include <cstddef>

namespace {

__global__ void vector_add_with_race(float* output, const float* left,
                                     const float* right, std::size_t count) {
  __shared__ volatile float marker;
  const std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
  if (blockIdx.x == 0 && threadIdx.x == 0) {
    marker = 0.0F;
  }
  if (blockIdx.x == 0 && threadIdx.x == 32) {
    marker = 1.0F;
  }
  __syncthreads();
  if (blockIdx.x == 0 && threadIdx.x == 0) {
    marker = 0.0F;
  }
  __syncthreads();
  if (index < count) {
    output[index] = left[index] + right[index];
  }
  if (blockIdx.x == 0 && threadIdx.x == 0) {
    output[0] += marker;
  }
}

}  // namespace

extern "C" void launch_candidate(float* output, const float* left,
                                 const float* right, std::size_t count,
                                 cudaStream_t stream) {
  constexpr int threads = 256;
  const int blocks = static_cast<int>((count + threads - 1) / threads);
  vector_add_with_race<<<blocks, threads, 0, stream>>>(output, left, right,
                                                       count);
}
