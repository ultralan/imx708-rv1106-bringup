#!/bin/sh
# imx708 强固加载: 开机早期 I2C 时序不稳时退避重试
cd /oem/usr/ko
N=0
while [ $N -lt 8 ]; do
  lsmod | grep -q imx708 && exit 0
  insmod imx708.ko 2>/dev/null
  lsmod | grep -q imx708 && exit 0
  # probe 失败: 等退避后重试 (2,4,6,8,10,12,14,16s)
  sleep $((N * 2 + 2))
  N=$((N + 1))
done
echo "imx708 加载 8 轮全失败" >&2
exit 1
