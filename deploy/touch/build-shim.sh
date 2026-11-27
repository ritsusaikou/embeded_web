#!/bin/sh
# 编译触摸注入 shim（LD_PRELOAD 方式）
# 只依赖 GLIBC_2.4；不用 dlsym（glibc 2.34 才并进 libc，板子是 2.23）
set -e
cd "$(dirname "$0")"
CC="${CC:-arm-linux-gnueabihf-gcc}"
$CC -shared -fPIC -O2 -Wall -Wextra \
    -D_TIME_BITS=32 -fno-stack-protector \
    -march=armv7-a -mfpu=vfpv3-d16 -mfloat-abi=hard \
    -o touch-inject.so touch-inject.c
ls -l touch-inject.so
arm-linux-gnueabihf-readelf --version-info touch-inject.so \
  | grep -oE "GLIBC_[0-9.]+" | sort -u | tr '\n' ' '
echo
