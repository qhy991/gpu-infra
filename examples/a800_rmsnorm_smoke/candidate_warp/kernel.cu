#include <cuda_runtime.h>

#include <cmath>

namespace {

__device__ float warp_sum(float value) {
  for (int offset = 16; offset > 0; offset /= 2) {
    value += __shfl_down_sync(0xffffffffU, value, offset);
  }
  return value;
}

__global__ void rmsnorm(float* output, const float* input, const float* weight,
                        int rows, int columns, float epsilon) {
  const int row = blockIdx.x;
  const int thread = threadIdx.x;
  const int lane = thread & 31;
  const int warp = thread >> 5;
  if (row >= rows) {
    return;
  }
  float sum = 0.0F;
  for (int column = thread; column < columns; column += blockDim.x) {
    const float value = input[static_cast<long long>(row) * columns + column];
    sum += value * value;
  }
  sum = warp_sum(sum);
  __shared__ float warp_sums[8];
  __shared__ float inverse;
  if (lane == 0) {
    warp_sums[warp] = sum;
  }
  __syncthreads();
  if (warp == 0) {
    float total = lane < 8 ? warp_sums[lane] : 0.0F;
    total = warp_sum(total);
    if (lane == 0) {
      inverse = rsqrtf(total / static_cast<float>(columns) + epsilon);
    }
  }
  __syncthreads();
  for (int column = thread; column < columns; column += blockDim.x) {
    const long long index = static_cast<long long>(row) * columns + column;
    output[index] = input[index] * inverse * weight[column];
  }
}

}  // namespace

extern "C" void launch_candidate(float* output, const float* input,
                                 const float* weight, int rows, int columns,
                                 float epsilon, cudaStream_t stream) {
  rmsnorm<<<rows, 256, 0, stream>>>(output, input, weight, rows, columns,
                                    epsilon);
}
