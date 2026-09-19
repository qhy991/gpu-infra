#include <metal_stdlib>
using namespace metal;
kernel void vector_double(device const float *x [[buffer(0)]], device float *y [[buffer(1)]], uint i [[thread_position_in_grid]]) {
    y[i] = x[i] * 2.0f;
}
