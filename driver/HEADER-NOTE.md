/*
 * IMX708（树莓派 Camera Module 3）驱动 — Luckfox Pico Zero / RV1106 移植版
 *
 * 骨架：armbian/linux-rockchip PR#457 的 Rockchip 风格实现（GPL-2.0），
 *       内核 API 已回退至 5.10（Luckfox BSP 5.10.160）。
 * 模式：单模式 2304x1296@30fps（2x2 binning），寄存器表为本板 MIPI 实测出流版本。
 * 板级适配：
 *   - clock name "xvclk"（Luckfox 设备树惯例，基准驱动原为 "xclk"）
 *   - reset GPIO：DT ACTIVE_LOW 下 gpiod 0 = 物理高 = 释放复位，上电后等 120ms
 *   - regulator 禁用（模组供电固定，参考 PieSoft 移植报告的处理）
 * 移植踩坑实录见仓库 README.md。
 *
 * SPDX-License-Identifier: GPL-2.0
 */
