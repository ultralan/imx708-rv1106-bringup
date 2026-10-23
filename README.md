# IMX708 on RV1106 — 硬件接龙实录

> 从 RPi 内核树把 IMX708（树莓派 Camera Module 3）移植到 Rockchip RV1106
> （Luckfox Pico Zero），以及在"看似已经移植好了"的驱动里踩穿的 7 个静默陷阱。
>
> 本文是 [PieSoft《Porting a Camera Sensor Driver Between Vendor Kernel Trees: IMX708 on Rockchip RV1106》](https://piesoft.com/news/imx708-driver-port-rockchip-rv1106/)
> 的接龙与补充：他们讲了"从零写一个能跑的驱动"要跨什么栏，我们讲"拿到一份能出图的移植版之后"，
> 距离"3A 真正工作"之间还隔着什么——以及为什么**打补丁打不到那里**。

## TL;DR

| | |
|---|---|
| 硬件 | Luckfox Pico Zero（RV1106G3）+ 树莓派 Camera Module 3（IMX708 / TNBA1392） |
| 内核 | Luckfox BSP 5.10.160（uclibc，armv7） |
| 起点 | 一份"能出图"的 IMX708 驱动（RPi 5.15 源 + Luckfox RV1103 移植） |
| 终点 | Rockchip 生态全对齐的驱动，rkaiq 3A（AE）闭环工作，RTSP 稳定出流 |
| 教训 | **vendor 树间的移植，差异是系统性的。用目标树的驱动做骨架，源树只供寄存器表。** |

## 背景：我们已经"成功"了，直到打开日志

项目初期照常规路径完成了移植：RPi 内核的 `imx708.c` 为底、参照 Luckfox 的 RV1103 移植版适配
I²C/设备树/供电，Buildroot 全量构建刷机，`rkipc` 出 RTSP，VLC 能拉到 2304×1296 的画面。

和 PieSoft 描述的一样，这一步的每一步都是静默失败高危区（CROP_BOUNDS 语义、模块加载顺序、
pixel_rate 换算……），我们也都遇到了——细节他们已经写透，本文不重复。

真正的故事从**画面过曝**开始：拉流画面整片发白，`rkipc.ini` 里写了 `1/100s` 上限但传感器实测
曝光钉在帧长上限。翻开 `/userdata/rkipc.log`，看到了这篇文章的主角：

```
AEC:E:InputExposure == 0 (0.000000), return error
AEC:E:GAIN OUT OF RANGE: lasttime-gain: 0.009997-0.000000
AEC:E:RunAec AecRun failed: 7
ANALYZER:E:ae algo processing failed ret -1
```

每秒约 70 条，累计 **50 万+条**。rkaiq 的 AE 环从开机起就每帧失败：算法读到的"当前曝光"永远是 0，
于是 AecRun 拒绝执行，从不向传感器下发任何曝光修正。传感器停留在驱动 probe 时的默认值——
所谓"过曝"，其实是 **AE 从未运行过**。

PieSoft 的文章让我们少走了大量弯路（他们踩过的时钟/白平衡坑我们直接对照排查），但他们止步于
"驱动写完、3A 工作了"，没有展开讲：**如果 3A 没工作，从哪查起**。这正好是我们的部分。

## 排查方法：先画一条诚实的数据流

RK 的 3A 链路横跨三层，先承认它是个多组件系统，再在每一层边界上取证：

```
rkaiq 闭源算法库 (librkaiq.so, rk_aiq_ae_algo_ecm.cpp 的 EcmExecute*)
        │  ① 读"当前生效曝光" ← SensorHw::_effecting_exp_map（环形表）
        ▼
rkaiq 开源框架层 (SensorHw / RkAiqCore / RkAiqAeHandle)
        │  ② 算法成功 → 产出 RESULT_TYPE_EXPOSURE_PARAM
        │     → SensorHw::setLinearSensorExposure → V4L2 s_ctrl
        ▼
内核驱动 (imx708.ko)
        │  ③ s_ctrl → I²C 写传感器寄存器
        ▼
IMX708 硬件
```

在每层边界做一次"写进去/读出来"对照实验，很快钉死了断点位置：

**③ 驱动→硬件：通的。** 用 v4l2-ctl 写 `exposure=800`，立刻 i2ctransfer 回读寄存器
`0x0202=0x0320`——写入真实落地。（这一步同时证伪了我们对
`pm_runtime_get_if_in_use()` 静默吞写控的怀疑——它是 PieSoft 式"看似合理的第一个假设"，
被实验干掉了。）

**② rkaiq→V4L2：断的。** 给驱动 s_ctrl 加一行 `dev_info` 追踪，实测 rkipc 启动时只发生
一次控制写入（probe 默认值），**之后 AE 环再没下发过任何 s_ctrl**——不是写丢，是根本没写。

**① 算法→数据源：死锁。** 对照 camera_engine_rkaiq 开源镜像读框架源码：
`_effecting_exp_map` 由算法自己的输出填充（`setExposureParams`），而算法要读到表里的值才肯
输出——鸡生蛋。上游 `RkAiqCore.cpp` 里甚至留着一句官方注释：
`//TODO: this will lead no initial ae params`。正常设备的第一推动力从哪来，成了核心谜题。

## 我们踩的坑（按发现顺序）

### 坑 1：`RKMODULE_SET_HDR_CFG` 未实现 → rkaiq 拒绝配置 HDR 模式

**症状**：启动日志 `CAMHW:E:failed to set hdr mode 0`。

Rockchip 的 rkcif/ISP 通过一组非标准 `RKMODULE_*` ioctl 与传感器驱动交换元数据，这是 PieSoft
文中"the Rockchip questionnaire"的主体。我们的移植版实现了 `GET_MODULE_INFO`，但漏了 HDR
协商。补上（含 compat32 分支与模式表 `hdr_mode` 字段）后，这条错误消失。

**单独看它不是 AE 死因**——但它是第一课：RPi 驱动天然一个 RKMODULE 都没有，移植版补了几个
不等于补齐了。

### 坑 2：VBLANK 控件的 min 语义（RPi 与 Rockchip 对同一控件的两种读法）

**症状**：无直接报错，纯换算错误。

RPi 生态：`V4L2_CID_VBLANK` 的 min 是传感器物理最小消隐（我们抄来的值是 4）。
Rockchip 生态：rkaiq 的 `SensorHw::get_blank()` 拿 **`ctrl.minimum`** 当作"当前帧长"反推
`line_periods_per_field`（对照 BSP 自带的 imx415 驱动：它的 vblank min = 模式默认 vblank）。

min=4 让 rkaiq 算出帧长 1300 行（真实 1336），一切行↔秒换算从这里开始歪。

### 坑 3：帧长寄存器地址抄错 —— I²C 假成功，最阴的一个

**症状**：`v4l2-ctl -c vertical_blanking=2000` 返回 rc=0，传感器 VMAX 寄存器纹丝不动。

移植版把帧长寄存器写成 `0x0160/0x0161`（某处抄来的惯例），而 IMX708 的真实寄存器是
`0x0340/0x0341`。I²C 往一个"存在的错误地址"写数据不会报错——**驱动层 rc=0，硬件层无效**。
这意味着 rkaiq 每帧的帧长设置全部落空，时序模型与真实传感器永久脱钩。

> 这类坑 PieSoft 的总结完全适用：验证要看输出，不能看返回值。我们后来形成了肌肉记忆：
> 每次改完驱动，`v4l2-ctl` 写一次、`i2ctransfer` 读一次，两相对照才算数。

### 坑 4：模式时序三元组不自洽（PieSoft 时钟坑的变体）

**症状**：无。又是一个静默的。

修完 VBLANK 后核对三元组：`HMAX=7824 × VMAX=1336 × 30fps ≠ PIXEL_RATE=590M`。
按这三个数反推像素时钟只有 ~313MHz，与上报值差近一倍。RPi 官方该模式的用法是
`vblank_default=1198`（VMAX=2494）：`7824 × 2494 × 30 ≈ 585.6M`，与官方 pixel_rate
585.6MHz 严丝合缝。寄存器表里的 VMAX=1336 只是"最小帧长"（≈56fps 时序），不是 30fps 工作点。

这正是 PieSoft 那个"时钟错一倍、无任何报错、AE 上限全歪"的坑换了件衣服：**任何让
fps/HMAX/VMAX/pixel_rate 四个数乘除不闭合的错误，都会安静地毒死 AE 的换算**。

### 坑 5：增益码制 —— 两棵树说的是两种语言（压垮补丁路线的最后一根稻草）

**症状**：`GAIN OUT OF RANGE: lasttime-gain: 0.009997-0.000000` 每帧刷屏。

RPi 驱动：`V4L2_CID_ANALOGUE_GAIN` 的值是传感器原生增益码（0–960），直写寄存器。
Rockchip 生态：增益值 = **线性倍率 × 1024**（1024–16384），驱动内部换算回原生码
（armbian 树那份 Rockchip 风格 imx708 驱动：`again = 1024 - 1024*1024/val`）。

rkaiq 按 Rockchip 语义去解释我们的 0–960，MinGain/MaxGain 区间校验恒不通过。
日志里那个神秘的 `0.009997` 从此有了解释——两套计量单位打架的读数。

到这里，**5 个修复全部部署，AE 死循环纹丝不动**。

### 坑 6（架构级）：补丁打不完 —— 该换骨架了

第 5 次失败后我们停下来数了数：HDR ioctl、VBLANK 语义、寄存器地址、时序三元组、增益码制——
五个坑没有一个是孤立的，它们全是同一件事的切面：**RPi 驱动的"世界观"与 Rockchip BSP
的约定系统性地不同**。继续打补丁，等着的还有 `RKMODULE_GET_CHANNEL_INFO`、AWB_CFG、
`long_exp_shift`、mbus_config 结构体布局、remove 签名……列表还在变长。

回头看 PieSoft 文章里我们当初略读而过的一句话，此刻分量完全不同：

> "The result was two sources. The Raspberry Pi driver supplied everything about the sensor
> itself... **imx327.c from the Rockchip tree supplied the template for the wrapper.**
> The final file is a hybrid with Raspberry Pi internals and a Rockchip frame."

**用目标树的驱动做骨架，源树只供传感器知识（寄存器表/时序/初始化序列）。** 我们反着做了：
拿 RPi 的骨架往上贴 Rockchip 器官——每次手术都成功，病人还是不行。

换骨架的实操（以 [armbian/linux-rockchip#457](https://github.com/armbian/linux-rockchip/pull/457)
合入的 Rockchip 风格 imx708.c 为基）：

1. 模式表裁剪成单模式 2304×1296，**寄存器表沿用我们自己那份**（本板 MIPI 实测出流）；
2. regulator 功能禁用（模组供电固定，PieSoft 同款处理）；
3. 内核 API 回退 6.1→5.10（`v4l2_subdev_state*`→`v4l2_subdev_pad_config*`、
   `mbus_config` 平铺 flags、i2c remove 返回 int、
   `v4l2_async_register_subdev_sensor`→`..._sensor_common`）——即 PieSoft 迁移表的逆向；
4. 修移植引入的两个 bug（见坑 7）；
5. 设备树补 24MHz 固定时钟节点，内核树规则重打 boot.img（FIT 格式，注意
   `build.sh firmware` 不会重打 boot 分区镜像）。

刷机重启：**`InputExposure`/`GAIN OUT` 报错归零，AE 自动收敛**（曝光稳定在 3964 行、增益 1×），
RTSP 出流、转推正常。两天、50 万条日志的死循环，就此关闭。

### 坑 7：换骨架路上新踩的两个（前人指路也躲不掉的）

即便有基准驱动，树与树、板与板之间还有缝：

- **clock name 不匹配**：基准驱动 `devm_clk_get(dev, "xclk")`，Luckfox 设备树惯例是
  `clock-names = "xvclk"`（BSP 自带 imx415 节点即此名）。probe 直接 `-ENOENT`。
- **reset GPIO 极性**：设备树标 `GPIO_ACTIVE_LOW` 时，`gpiod_*` 的"1"是逻辑高=物理低=保持复位。
  基准驱动的 `gpiod_direction_output(reset, 1)` 在这张板上是把传感器按在复位态读 ID，
  报 `error -5`。对照旧驱动实测序列改成 0（=释放复位）+120ms 稳定等待后通过。

> 经验：**骨架解决"系统性差异"，但每个 GPIO 名字、时钟名、极性仍是板级事实**，
> 只能对着自己的原理图和旧工作代码逐项核。

### 附：两个非驱动侧的运维坑（同样吃了小时数）

- **`/userdata` 被 rkipc.log 写爆（238MB 分区塞了 227MB 日志）**：症状是推流服务起不来、
  配置写入 `Errno 28`——表面看像驱动问题，实际先 `df -h`。高频错误日志 + 小分区 = 定时炸弹。
- **rkipc 退出时把内存态配置写回 ini**：直接编辑 `/userdata/rkipc.ini` 会在进程退出时被
  覆盖回旧值。要改配置：kill 进程 → 改文件 →（必要时）`chattr +i` 锁住再拉起。

## 方法论小结（给下一个移植者）

1. **先分层取证，再动手修**。3A 链路三层，每层做一次写/读对照实验定位断点，
   比按直觉打补丁快得多——我们的前 5 个补丁都是"这个肯定有问题→修→真有问题→但没用"。
2. **静默失败是 vendor 树的常态**。I²C 写错地址不报错、控件 min 语义不同不报错、
   时序不自洽不报错。唯一可信的验证是**测输出**：v4l2 写 → i2c 读 → 拉流看画面。
3. **坑的分布揭示架构问题**。当第 N 个补丁成功部署但症状不动，且每个坑都属于同一类
   （"两棵树对同一事物的约定不同"），停止修补，换骨架。PieSoft 用 imx327.c 做壳的
   "hybrid"不是代码风格偏好，是这类移植的正解。
4. **参考驱动要选"活的"**。判断标准：目标树内、同生态、作者声明在真机跑通过。
   我们最终用的基准来自 armbian 树（其内核头文件与 Luckfox BSP 同源），
   且保留了它自带的 PDAF/ebd 通道声明——后续做自动对焦（DW9807 VCM, I²C 0x0c）
   直接受益。

## 硬件 / 环境快照

| 项 | 值 |
|---|---|
| 板 | Luckfox Pico Zero（RV1106G3，eMMC） |
| 传感器 | Raspberry Pi Camera Module 3（IMX708，模组号 TNBA1392） |
| 内核 | 5.10.160（Luckfox BSP，Buildroot uclibc） |
| ISP | RV1106 ISP 3.2（输入上限 3072×1728，故工作模式取 2304×1296@30fps 2×2 binning） |
| 应用 | rkipc（RTSP 554）+ ffmpeg 转推 |
| IQ | 手动白平衡 `mwbGain [2.52, 1.0, 1.0, 1.721]`（室内 LED，与 PieSoft 公开值一致，互为印证） |

## 仓库内容

```
imx708-rv1106-bringup/
├── README.md            # 本文
├── driver/imx708.c      # 最终驱动（Rockchip 骨架 + 本板实测寄存器表，5.10.160）
└── dts/                 # 设备树参考片段
    └── imx708-node.dtsi # imx708@1a 节点 + 24MHz fixed-clock
```

驱动以 GPL-2.0 发布（上游 RPi/Codemesh 同源）；文章内容 CC BY 4.0。

## 致谢与参考

- [PieSoft — Porting a Camera Sensor Driver Between Vendor Kernel Trees: IMX708 on Rockchip RV1106](https://piesoft.com/news/imx708-driver-port-rockchip-rv1106/)：
  本文的起点与骨架。他们的"silent failure"方法论、"vendor 驱动是问卷"比喻、时钟坑案例，
  在我们的排查中逐字应验。
- [armbian/linux-rockchip#457](https://github.com/armbian/linux-rockchip/pull/457)：
  我们最终采用的 Rockchip 风格 imx708 驱动来源（作者注明经 Rockchip 调过 ISP）。
- [raspberrypi/linux](https://github.com/raspberrypi/linux) `drivers/media/i2c/imx708.c`：
  寄存器表与时序真值的权威来源。
- [LuckfoxTECH/luckfox-pico](https://github.com/LuckfoxTECH/luckfox-pico)：BSP、构建系统与板级参考。
