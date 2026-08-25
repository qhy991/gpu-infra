#include <cuda_runtime.h>

#include <cstddef>

namespace {

__global__ void vector_subtract(float* output, const float* left,
                                const float* right, std::size_t count) {
  const std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < count) {
    output[index] = left[index] - right[index];
  }
}

}  // namespace

extern "C" void launch_candidate(float* output, const float* left,
                                 const float* right, std::size_t count,
                                 cudaStream_t stream) {
  constexpr int threads = 256;
  const int blocks = static_cast<int>((count + threads - 1) / threads);
  vector_subtract<<<blocks, threads, 0, stream>>>(output, left, right, count);
}
