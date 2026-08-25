#include <cuda_runtime.h>

namespace {

__global__ void weighted_copy(float* output, const float* input,
                              const float* weight, int rows, int columns) {
  const long long index =
      static_cast<long long>(blockIdx.x) * blockDim.x + threadIdx.x;
  const long long count = static_cast<long long>(rows) * columns;
  if (index < count) {
    output[index] = input[index] * weight[index % columns];
  }
}

}  // namespace

extern "C" void launch_candidate(float* output, const float* input,
                                 const float* weight, int rows, int columns,
                                 float epsilon, cudaStream_t stream) {
  (void)epsilon;
  constexpr int threads = 256;
  const long long count = static_cast<long long>(rows) * columns;
  const int blocks = static_cast<int>((count + threads - 1) / threads);
  weighted_copy<<<blocks, threads, 0, stream>>>(output, input, weight, rows,
                                                columns);
}
