#include <cuda_runtime.h>

#include <cmath>

namespace {

__global__ void rmsnorm(float* output, const float* input, const float* weight,
                        int rows, int columns, float epsilon) {
  const int row = blockIdx.x;
  const int thread = threadIdx.x;
  if (row >= rows) {
    return;
  }
  float sum = 0.0F;
  for (int column = thread; column < columns; column += blockDim.x) {
    const float value = input[static_cast<long long>(row) * columns + column];
    sum += value * value;
  }
  __shared__ float partial[256];
  partial[thread] = sum;
  __syncthreads();
  for (int offset = blockDim.x / 2; offset > 0; offset /= 2) {
    if (thread < offset) {
      partial[thread] += partial[thread + offset];
    }
    __syncthreads();
  }
  const float inverse = rsqrtf(partial[0] / static_cast<float>(columns) + epsilon);
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
