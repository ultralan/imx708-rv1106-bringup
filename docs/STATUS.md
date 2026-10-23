# 仓库定位补充：这是什么、不是什么

**是**：一份在真实硬件上完成闭环的移植成果（驱动源码 + 设备树片段 + 完整踩坑记录），
面向"要在 RV1106/RV1103 上跑 Camera Module 3"以及更广泛地"在 Rockchip vendor 树上移植
RPi 传感器"的开发者。

**不是**：一个开箱即用的 SDK。编译需要你自己的 Luckfox BSP 环境（见 README 环境快照），
设备树的 pinctrl/引脚要对照你的板子原理图核对（我们踩过的时钟名/reset 极性坑就是板级差异的例子）。

## 快速接入（假设你已有 Luckfox BSP 构建环境）

1. `driver/imx708.c` → `sysdrv/source/kernel/drivers/media/i2c/imx708.c`
   （Kconfig/Makefile 增加条目，参考树内任一 Rockchip 传感器驱动）
2. `dts/imx708-node.dtsi` 的两段并入你的板级 dtsi（时钟节点放顶层，传感器节点挂到你的 I²C 总线）
3. 编译模块：
   ```bash
   make ARCH=arm O=<objs_kernel> CROSS_COMPILE=arm-rockchip830-linux-uclibcgnueabihf- \
        M=drivers/media/i2c modules
   ```
4. 设备树变更需重打 boot.img（内核树规则，非 `build.sh firmware`）：
   ```bash
   make ARCH=arm O=<objs_kernel> BOOT_ITS=$KERNEL_DIR/boot.its rv1106g-luckfox-pico-zero.img
   ```

## 已验证状态（2026-10-23）

- rkaiq AE 闭环工作（`InputExposure`/`GAIN OUT` 报错归零，曝光自动收敛）
- rkipc RTSP 2304×1296 出流 + ffmpeg 转推稳定
- IQ：手动白平衡（室内 LED 校准值见 README）

## 未完成 / 已知边界

- **自动对焦未启用**：硬件为 DW9807 VCM（I²C 0x0c）。需要 DTS 加马达节点 +
  `CONFIG_VIDEO_DW9807_VCM` + IQ `af_v31.TuningPara.contrast_af.enable = 1`。
  本驱动已保留 PDAF/embedded data 第二 pad 声明（来自骨架），是 AF 的接口前置。
- IQ 仍为手动白平衡版本，自动 AWB 未调校。
- 曾观察到一次 ISP 内核态停流挂死（`rkisp_stream_stop timeout`，需重启恢复），
  复现条件未定位，怀疑与驱动 stop 流时序有关——留意。
