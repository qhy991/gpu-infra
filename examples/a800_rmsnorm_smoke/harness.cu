#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <string>
#include <vector>

extern "C" void launch_candidate(float* output, const float* input,
                                 const float* weight, int rows, int columns,
                                 float epsilon, cudaStream_t stream);

namespace {

constexpr int kThreads = 256;
constexpr float kEpsilon = 1.0e-5F;

struct Workload {
  int rows;
  int columns;
};

const std::vector<Workload> kWorkloads = {{4096, 1024}, {2048, 4096}};

[[noreturn]] void fail_cuda(cudaError_t status, const char* expression,
                            const char* file, int line) {
  std::cerr << file << ':' << line << ": " << expression
            << " failed: " << cudaGetErrorString(status) << '\n';
  std::exit(2);
}

#define CUDA_CHECK(expression)                                             \
  do {                                                                     \
    const cudaError_t status = (expression);                               \
    if (status != cudaSuccess) {                                           \
      fail_cuda(status, #expression, __FILE__, __LINE__);                  \
    }                                                                      \
  } while (false)

__global__ void baseline_rmsnorm(float* output, const float* input,
                                 const float* weight, int rows, int columns,
                                 float epsilon) {
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
  __shared__ float partial[kThreads];
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

void launch_baseline(float* output, const float* input, const float* weight,
                     int rows, int columns, float epsilon,
                     cudaStream_t stream) {
  baseline_rmsnorm<<<rows, kThreads, 0, stream>>>(
      output, input, weight, rows, columns, epsilon);
}

struct DeviceBuffers {
  float* input = nullptr;
  float* weight = nullptr;
  float* output = nullptr;

  DeviceBuffers(int rows, int columns) {
    const std::size_t elements = static_cast<std::size_t>(rows) * columns;
    CUDA_CHECK(cudaMalloc(&input, elements * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&weight, columns * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&output, elements * sizeof(float)));
  }

  ~DeviceBuffers() {
    cudaFree(output);
    cudaFree(weight);
    cudaFree(input);
  }

  DeviceBuffers(const DeviceBuffers&) = delete;
  DeviceBuffers& operator=(const DeviceBuffers&) = delete;
};

std::string workload_id(const Workload& workload) {
  return "rmsnorm-r" + std::to_string(workload.rows) + "-c" +
         std::to_string(workload.columns);
}

double median(std::vector<float> samples) {
  std::sort(samples.begin(), samples.end());
  const std::size_t middle = samples.size() / 2;
  if (samples.size() % 2 == 0) {
    return (samples[middle - 1] + samples[middle]) / 2.0;
  }
  return samples[middle];
}

double coefficient_of_variation(const std::vector<float>& samples) {
  const double mean = std::accumulate(samples.begin(), samples.end(), 0.0) /
                      static_cast<double>(samples.size());
  double square_error = 0.0;
  for (const float sample : samples) {
    const double delta = sample - mean;
    square_error += delta * delta;
  }
  return std::sqrt(square_error / static_cast<double>(samples.size())) / mean;
}

void print_samples(const std::vector<float>& samples) {
  std::cout << '[';
  for (std::size_t index = 0; index < samples.size(); ++index) {
    if (index != 0) {
      std::cout << ',';
    }
    std::cout << samples[index];
  }
  std::cout << ']';
}

float measure(bool candidate, DeviceBuffers& buffers, const Workload& workload,
              cudaStream_t stream) {
  cudaEvent_t start;
  cudaEvent_t finish;
  CUDA_CHECK(cudaEventCreate(&start));
  CUDA_CHECK(cudaEventCreate(&finish));
  CUDA_CHECK(cudaEventRecord(start, stream));
  if (candidate) {
    launch_candidate(buffers.output, buffers.input, buffers.weight,
                     workload.rows, workload.columns, kEpsilon, stream);
  } else {
    launch_baseline(buffers.output, buffers.input, buffers.weight,
                    workload.rows, workload.columns, kEpsilon, stream);
  }
  CUDA_CHECK(cudaGetLastError());
  CUDA_CHECK(cudaEventRecord(finish, stream));
  CUDA_CHECK(cudaEventSynchronize(finish));
  float milliseconds = 0.0F;
  CUDA_CHECK(cudaEventElapsedTime(&milliseconds, start, finish));
  CUDA_CHECK(cudaEventDestroy(finish));
  CUDA_CHECK(cudaEventDestroy(start));
  return milliseconds;
}

bool check_workload(const Workload& workload, double* maximum_absolute,
                    double* maximum_relative) {
  const std::size_t elements =
      static_cast<std::size_t>(workload.rows) * workload.columns;
  std::vector<float> input(elements);
  std::vector<float> weight(workload.columns);
  std::vector<float> output(elements);
  for (std::size_t index = 0; index < elements; ++index) {
    input[index] = static_cast<float>(static_cast<int>(index % 257) - 128) /
                   64.0F;
  }
  for (int column = 0; column < workload.columns; ++column) {
    weight[column] = 0.5F + static_cast<float>(column % 17) / 32.0F;
  }

  DeviceBuffers buffers(workload.rows, workload.columns);
  CUDA_CHECK(cudaMemcpy(buffers.input, input.data(), elements * sizeof(float),
                        cudaMemcpyHostToDevice));
  CUDA_CHECK(cudaMemcpy(buffers.weight, weight.data(),
                        workload.columns * sizeof(float),
                        cudaMemcpyHostToDevice));
  launch_candidate(buffers.output, buffers.input, buffers.weight,
                   workload.rows, workload.columns, kEpsilon, nullptr);
  CUDA_CHECK(cudaGetLastError());
  CUDA_CHECK(cudaDeviceSynchronize());
  CUDA_CHECK(cudaMemcpy(output.data(), buffers.output, elements * sizeof(float),
                        cudaMemcpyDeviceToHost));

  *maximum_absolute = 0.0;
  *maximum_relative = 0.0;
  bool correct = true;
  for (int row = 0; row < workload.rows; ++row) {
    double square_sum = 0.0;
    for (int column = 0; column < workload.columns; ++column) {
      const double value = input[static_cast<long long>(row) * workload.columns +
                                 column];
      square_sum += value * value;
    }
    const double inverse =
        1.0 / std::sqrt(square_sum / workload.columns + kEpsilon);
    for (int column = 0; column < workload.columns; ++column) {
      const long long index =
          static_cast<long long>(row) * workload.columns + column;
      const double expected = input[index] * inverse * weight[column];
      const double absolute = std::abs(static_cast<double>(output[index]) - expected);
      const double relative = absolute / std::max(std::abs(expected), 1.0e-8);
      *maximum_absolute = std::max(*maximum_absolute, absolute);
      *maximum_relative = std::max(*maximum_relative, relative);
      if (absolute > 2.0e-4 + 2.0e-4 * std::abs(expected)) {
        correct = false;
      }
    }
  }
  return correct;
}

void run_correctness(const cudaDeviceProp& properties) {
  bool all_correct = true;
  std::cout << std::setprecision(9);
  std::cout << "{\"mode\":\"correctness\",\"device\":\""
            << properties.name << "\",\"compute_capability\":\""
            << properties.major << '.' << properties.minor
            << "\",\"workloads\":[";
  for (std::size_t index = 0; index < kWorkloads.size(); ++index) {
    double maximum_absolute = 0.0;
    double maximum_relative = 0.0;
    const bool correct = check_workload(kWorkloads[index], &maximum_absolute,
                                        &maximum_relative);
    all_correct = all_correct && correct;
    if (index != 0) {
      std::cout << ',';
    }
    std::cout << "{\"id\":\"" << workload_id(kWorkloads[index])
              << "\",\"correct\":" << (correct ? "true" : "false")
              << ",\"max_abs_error\":" << maximum_absolute
              << ",\"max_rel_error\":" << maximum_relative << '}';
  }
  std::cout << "]}" << std::endl;
  if (!all_correct) {
    std::exit(1);
  }
}

void run_benchmark(const cudaDeviceProp& properties) {
  constexpr int warmups = 10;
  constexpr int repetitions = 20;
  cudaStream_t stream;
  CUDA_CHECK(cudaStreamCreate(&stream));
  std::cout << std::setprecision(9);
  std::cout << "{\"mode\":\"benchmark\",\"device\":\""
            << properties.name << "\",\"compute_capability\":\""
            << properties.major << '.' << properties.minor
            << "\",\"workloads\":[";
  for (std::size_t index = 0; index < kWorkloads.size(); ++index) {
    const Workload workload = kWorkloads[index];
    const std::size_t elements =
        static_cast<std::size_t>(workload.rows) * workload.columns;
    DeviceBuffers buffers(workload.rows, workload.columns);
    CUDA_CHECK(cudaMemsetAsync(buffers.input, 0, elements * sizeof(float), stream));
    CUDA_CHECK(cudaMemsetAsync(buffers.weight, 0,
                               workload.columns * sizeof(float), stream));
    for (int warmup = 0; warmup < warmups; ++warmup) {
      launch_baseline(buffers.output, buffers.input, buffers.weight,
                      workload.rows, workload.columns, kEpsilon, stream);
      launch_candidate(buffers.output, buffers.input, buffers.weight,
                       workload.rows, workload.columns, kEpsilon, stream);
    }
    CUDA_CHECK(cudaGetLastError());
    CUDA_CHECK(cudaStreamSynchronize(stream));

    std::vector<float> baseline_samples;
    std::vector<float> candidate_samples;
    baseline_samples.reserve(repetitions);
    candidate_samples.reserve(repetitions);
    for (int repeat = 0; repeat < repetitions; ++repeat) {
      if (repeat % 2 == 0) {
        baseline_samples.push_back(measure(false, buffers, workload, stream));
        candidate_samples.push_back(measure(true, buffers, workload, stream));
      } else {
        candidate_samples.push_back(measure(true, buffers, workload, stream));
        baseline_samples.push_back(measure(false, buffers, workload, stream));
      }
    }

    if (index != 0) {
      std::cout << ',';
    }
    std::cout << "{\"id\":\"" << workload_id(workload)
              << "\",\"candidate_ms\":" << median(candidate_samples)
              << ",\"baseline_ms\":" << median(baseline_samples)
              << ",\"baseline_cv\":"
              << coefficient_of_variation(baseline_samples)
              << ",\"candidate_samples_ms\":";
    print_samples(candidate_samples);
    std::cout << ",\"baseline_samples_ms\":";
    print_samples(baseline_samples);
    std::cout << '}';
  }
  std::cout << "]}" << std::endl;
  CUDA_CHECK(cudaStreamDestroy(stream));
}

}  // namespace

int main(int argc, char** argv) {
  if (argc != 3 || std::string(argv[1]) != "--mode") {
    std::cerr << "usage: runner --mode correctness|benchmark\n";
    return 2;
  }
  CUDA_CHECK(cudaSetDevice(0));
  cudaDeviceProp properties{};
  CUDA_CHECK(cudaGetDeviceProperties(&properties, 0));
  const std::string mode = argv[2];
  if (mode == "correctness") {
    run_correctness(properties);
    return 0;
  }
  if (mode == "benchmark") {
    run_benchmark(properties);
    return 0;
  }
  std::cerr << "unsupported mode: " << mode << '\n';
  return 2;
}
