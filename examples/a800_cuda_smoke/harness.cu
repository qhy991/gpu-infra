#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <string>
#include <vector>

extern "C" void launch_candidate(float* output, const float* left,
                                 const float* right, std::size_t count,
                                 cudaStream_t stream);

namespace {

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

__global__ void baseline_vector_add(float* output, const float* left,
                                    const float* right, std::size_t count) {
  const std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < count) {
    output[index] = left[index] + right[index];
  }
}

void launch_baseline(float* output, const float* left, const float* right,
                     std::size_t count, cudaStream_t stream) {
  constexpr int threads = 256;
  const int blocks = static_cast<int>((count + threads - 1) / threads);
  baseline_vector_add<<<blocks, threads, 0, stream>>>(output, left, right,
                                                       count);
}

struct DeviceBuffers {
  float* left = nullptr;
  float* right = nullptr;
  float* output = nullptr;

  explicit DeviceBuffers(std::size_t count) {
    CUDA_CHECK(cudaMalloc(&left, count * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&right, count * sizeof(float)));
    CUDA_CHECK(cudaMalloc(&output, count * sizeof(float)));
  }

  ~DeviceBuffers() {
    cudaFree(output);
    cudaFree(right);
    cudaFree(left);
  }

  DeviceBuffers(const DeviceBuffers&) = delete;
  DeviceBuffers& operator=(const DeviceBuffers&) = delete;
};

std::string workload_id(std::size_t count) {
  return "vector-add-n" + std::to_string(count);
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

float measure(bool candidate, DeviceBuffers& buffers, std::size_t count,
              cudaStream_t stream) {
  cudaEvent_t start;
  cudaEvent_t finish;
  CUDA_CHECK(cudaEventCreate(&start));
  CUDA_CHECK(cudaEventCreate(&finish));
  CUDA_CHECK(cudaEventRecord(start, stream));
  if (candidate) {
    launch_candidate(buffers.output, buffers.left, buffers.right, count, stream);
  } else {
    launch_baseline(buffers.output, buffers.left, buffers.right, count, stream);
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

bool check_workload(std::size_t count, double* maximum_error) {
  std::vector<float> left(count);
  std::vector<float> right(count);
  std::vector<float> output(count);
  for (std::size_t index = 0; index < count; ++index) {
    left[index] = static_cast<float>(static_cast<int>(index % 251) - 125) /
                  128.0F;
    right[index] = static_cast<float>(static_cast<int>(index % 127) - 63) /
                   64.0F;
  }

  DeviceBuffers buffers(count);
  CUDA_CHECK(cudaMemcpy(buffers.left, left.data(), count * sizeof(float),
                        cudaMemcpyHostToDevice));
  CUDA_CHECK(cudaMemcpy(buffers.right, right.data(), count * sizeof(float),
                        cudaMemcpyHostToDevice));
  launch_candidate(buffers.output, buffers.left, buffers.right, count, nullptr);
  CUDA_CHECK(cudaGetLastError());
  CUDA_CHECK(cudaDeviceSynchronize());
  CUDA_CHECK(cudaMemcpy(output.data(), buffers.output, count * sizeof(float),
                        cudaMemcpyDeviceToHost));

  *maximum_error = 0.0;
  for (std::size_t index = 0; index < count; ++index) {
    const double expected = static_cast<double>(left[index] + right[index]);
    *maximum_error =
        std::max(*maximum_error, std::abs(static_cast<double>(output[index]) -
                                          expected));
  }
  return *maximum_error == 0.0;
}

void run_correctness(const cudaDeviceProp& properties) {
  const std::vector<std::size_t> counts = {1U << 22, 1U << 24};
  bool all_correct = true;
  std::cout << std::setprecision(9);
  std::cout << "{\"mode\":\"correctness\",\"device\":\""
            << properties.name << "\",\"compute_capability\":\""
            << properties.major << '.' << properties.minor
            << "\",\"workloads\":[";
  for (std::size_t index = 0; index < counts.size(); ++index) {
    double maximum_error = 0.0;
    const bool correct = check_workload(counts[index], &maximum_error);
    all_correct = all_correct && correct;
    if (index != 0) {
      std::cout << ',';
    }
    std::cout << "{\"id\":\"" << workload_id(counts[index])
              << "\",\"correct\":" << (correct ? "true" : "false")
              << ",\"max_abs_error\":" << maximum_error << '}';
  }
  std::cout << "]}" << std::endl;
  if (!all_correct) {
    std::exit(1);
  }
}

void run_benchmark(const cudaDeviceProp& properties) {
  const std::vector<std::size_t> counts = {1U << 22, 1U << 24};
  constexpr int warmups = 10;
  constexpr int repetitions = 20;
  cudaStream_t stream;
  CUDA_CHECK(cudaStreamCreate(&stream));

  std::cout << std::setprecision(9);
  std::cout << "{\"mode\":\"benchmark\",\"device\":\""
            << properties.name << "\",\"compute_capability\":\""
            << properties.major << '.' << properties.minor
            << "\",\"workloads\":[";
  for (std::size_t workload = 0; workload < counts.size(); ++workload) {
    const std::size_t count = counts[workload];
    DeviceBuffers buffers(count);
    CUDA_CHECK(cudaMemsetAsync(buffers.left, 0, count * sizeof(float), stream));
    CUDA_CHECK(cudaMemsetAsync(buffers.right, 0, count * sizeof(float), stream));
    for (int index = 0; index < warmups; ++index) {
      launch_baseline(buffers.output, buffers.left, buffers.right, count, stream);
      launch_candidate(buffers.output, buffers.left, buffers.right, count, stream);
    }
    CUDA_CHECK(cudaGetLastError());
    CUDA_CHECK(cudaStreamSynchronize(stream));

    std::vector<float> baseline_samples;
    std::vector<float> candidate_samples;
    baseline_samples.reserve(repetitions);
    candidate_samples.reserve(repetitions);
    for (int index = 0; index < repetitions; ++index) {
      if (index % 2 == 0) {
        baseline_samples.push_back(measure(false, buffers, count, stream));
        candidate_samples.push_back(measure(true, buffers, count, stream));
      } else {
        candidate_samples.push_back(measure(true, buffers, count, stream));
        baseline_samples.push_back(measure(false, buffers, count, stream));
      }
    }

    if (workload != 0) {
      std::cout << ',';
    }
    std::cout << "{\"id\":\"" << workload_id(count)
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
  int device = 0;
  CUDA_CHECK(cudaSetDevice(device));
  cudaDeviceProp properties{};
  CUDA_CHECK(cudaGetDeviceProperties(&properties, device));
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
