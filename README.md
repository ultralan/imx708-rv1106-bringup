# imx708-rv1106-bringup

Porting the Raspberry Pi Camera Module 3 sensor (**Sony IMX708**) to the
Rockchip RV1106 (Luckfox Pico Zero), and the seven silent traps we hit
**after** the driver already "worked" — a continuation of
[PieSoft's IMX708-on-RV1106 porting report](https://piesoft.com/news/imx708-driver-port-rockchip-rv1106/).

They covered what it takes to write a driver that streams. This repo covers
what comes after: **when rkaiq's 3A loop silently refuses to run**, where to
look — and why patching will not get you there.

> TL;DR — The differences between vendor kernel trees are *systemic*, not
> point bugs. Use a driver from the target tree as the skeleton and let the
> source tree contribute nothing but register tables. We proved this the hard
> way: five correct fixes, zero effect on the symptom.

## Background: "successful" port, broken 3A

The port followed the usual path: RPi kernel `imx708.c` as the base, adapted
against a Luckfox RV1103 bring-up, full Buildroot firmware, and `rkipc`
serving RTSP. VLC pulled 2304×1296 frames. Done — except the image was
blown out, and `/userdata/rkipc.log` was flooding:

```
AEC:E:InputExposure == 0 (0.000000), return error
AEC:E:GAIN OUT OF RANGE: lasttime-gain: 0.009997-0.000000
AEC:E:RunAec AecRun failed: 7
ANALYZER:E:ae algo processing failed ret -1
```

~70 lines/second, 500k+ lines total. rkaiq's AE loop had failed on every
frame since boot: the algorithm reads "current exposure" as zero, refuses to
run, and never sends exposure updates to the sensor. The "overexposure" was
simply **AE never having run at all** — the sensor sat at probe defaults.

## Method: an honest data-flow diagram first

The RK 3A chain spans three layers. Draw it, then gather evidence at each
boundary before touching code:

```
rkaiq closed-source algo (librkaiq.so, EcmExecute* in rk_aiq_ae_algo_ecm.cpp)
        │  ① reads "effective exposure" ← SensorHw::_effecting_exp_map
        ▼
rkaiq open framework (SensorHw / RkAiqCore / RkAiqAeHandle)
        │  ② on success → RESULT_TYPE_EXPOSURE_PARAM
        │     → setLinearSensorExposure → V4L2 s_ctrl
        ▼
kernel driver (imx708.ko)
        │  ③ s_ctrl → I²C writes sensor registers
        ▼
IMX708 hardware
```

One write/read experiment per boundary pinned the break immediately:

**③ driver → hardware: fine.** `v4l2-ctl -c exposure=800`, read back
register `0x0202 = 0x0320` via i2ctransfer. Writes really land. (This also
falsified our first theory — the `pm_runtime_get_if_in_use()` gate silently
dropping controls. A plausible-sounding first guess, killed by an experiment,
exactly the failure mode PieSoft warns about.)

**② rkaiq → V4L2: broken.** A one-line `dev_info` trace in `s_ctrl` showed
rkipc writes the controls **once** at startup, then the AE loop never issues
another one. Not lost writes — never written.

**① algo → data source: deadlock.** Reading the open-source camera_engine_rkaiq
framework: `_effecting_exp_map` is populated from the *algorithm's own output*
(`setExposureParams`), but the algorithm refuses to output until it can read
the table. Chicken-and-egg. Upstream `RkAiqCore.cpp` even carries the comment:
`//TODO: this will lead no initial ae params`. Where the first push comes from
on a *working* device became the central question.

## The traps, in order of discovery

### 1. `RKMODULE_SET_HDR_CFG` not implemented

Symptom: `CAMHW:E:failed to set hdr mode 0` at startup.

The Rockchip "questionnaire" (PieSoft's term) is a whole set of nonstandard
`RKMODULE_*` ioctls. Our port had `GET_MODULE_INFO` but no HDR negotiation.
Adding it (plus the compat32 branch and an `hdr_mode` field in the mode
table) killed this error. Not the AE cause by itself — but lesson one:
a port that implements *some* RKMODULE ioctls has not implemented *the*
RKMODULE contract.

### 2. VBLANK control minimum — two ecosystems, two readings of one control

No error; pure arithmetic poison.

RPi world: `V4L2_CID_VBLANK` min = physical minimum blanking (we had 4).
Rockchip world: rkaiq's `SensorHw::get_blank()` takes **`ctrl.minimum`** as
*the current frame length* to derive `line_periods_per_field` (check any
in-tree Rockchip sensor driver, e.g. imx415: vblank min == mode default).

min=4 made rkaiq compute a 1300-line frame (truth: 1336). Every
lines↔seconds conversion downstream was quietly wrong.

### 3. Wrong frame-length register address — the I²C false success

Symptom: `v4l2-ctl -c vertical_blanking=2000` returns rc=0; the sensor's
VMAX register does not move.

The port carried `0x0160/0x0161` as the frame-length register. IMX708's real
register is `0x0340/0x0341`. Writing I²C to a *valid-but-wrong* address does
not error — the driver reports success, the hardware ignores it. Every
per-frame frame-length setting from rkaiq fell into this void.

> PieSoft's rule applies verbatim: verify the output, not the return code.
> Our resulting habit: after every driver change, `v4l2-ctl` write once,
> `i2ctransfer` read once, compare.

### 4. The timing triple doesn't close (a variant of PieSoft's clock bug)

No symptom. Again.

After fixing VBLANK we checked the arithmetic: `HMAX 7824 × VMAX 1336 × 30fps`
≈ 313 MHz pixel clock — nowhere near the reported 590 MHz. The RPi driver
runs this mode with `vblank_default = 1198` (VMAX = 2494):
`7824 × 2494 × 30 ≈ 585.6 MHz` = the official `pixel_rate`. The register
table's VMAX=1336 is merely the *minimum* frame length (a ~56 fps timing),
not the 30 fps operating point.

Anything that breaks `fps × HMAX × VMAX ≈ pixel_rate` silently poisons every
AE time conversion. Same disease as PieSoft's "clock wrong by a factor,
nothing reported" — different rash.

### 5. Gain code semantics — two trees speak different units

Symptom: `GAIN OUT OF RANGE: lasttime-gain: 0.009997-0.000000` every frame.

RPi driver: `V4L2_CID_ANALOGUE_GAIN` carries the sensor's native gain code
(0–960), written straight through. Rockchip ecosystem: gain value =
**linear multiplier × 1024** (1024–16384), converted inside the driver
(`again = 1024 - 1024*1024/val`, as in the Rockchip-style imx708 driver
merged in armbian). rkaiq interpreted our 0–960 through Rockchip semantics;
its MinGain/MaxGain range check could never pass. The mysterious `0.009997`
in the log was the sound of two unit systems colliding.

At this point: **five fixes deployed, all real bugs, zero effect on the
death loop.**

### 6. Architectural: the patches don't converge — swap the skeleton

After the fifth failure we stopped and counted. HDR ioctls, VBLANK
semantics, register address, timing triple, gain units — five non-bugs-in-
isolation, all facets of one thing: **the RPi driver's worldview differs
systematically from the Rockchip BSP's conventions.** The queue behind them
(`RKMODULE_GET_CHANNEL_INFO`, AWB_CFG, `long_exp_shift`, `mbus_config`
layout, `remove` signature, …) was still growing.

And a sentence from PieSoft we had skimmed past now read completely
differently:

> "The result was two sources. The Raspberry Pi driver supplied everything
> about the sensor itself... **imx327.c from the Rockchip tree supplied the
> template for the wrapper.** The final file is a hybrid with Raspberry Pi
> internals and a Rockchip frame."

Use the target tree's driver as the skeleton; the source tree contributes
sensor knowledge only. We had done it backwards — RPi skeleton with Rockchip
organs stitched on. Every surgery succeeded; the patient stayed dead.

The swap, concretely — base: the Rockchip-style `imx708.c` from
[armbian/linux-rockchip#457](https://github.com/armbian/linux-rockchip/pull/457):

1. Mode table cut down to a single 2304×1296 mode, **register table kept
   ours** (proven on this board's MIPI link);
2. Regulator support disabled (module rails are fixed — PieSoft's own
   treatment);
3. Kernel API back-ported 6.1 → 5.10 (`v4l2_subdev_state*` →
   `v4l2_subdev_pad_config*`, flat `mbus_config` flags, int-returning
   i2c remove, `v4l2_async_register_subdev_sensor_common`) — PieSoft's
   migration table, in reverse;
4. Fix the two port bugs below;
5. Add a 24 MHz fixed-clock node to the DTS and rebuild boot.img (FIT
   format — note `build.sh firmware` does *not* rebuild the boot partition
   image; use the kernel-tree rule with `BOOT_ITS=`).

First boot after flashing: **`InputExposure` and `GAIN OUT` errors at zero,
AE converging** (exposure settles at 3964 lines, gain 1×), RTSP streaming
and restreaming. Two days and half a million log lines, closed.

### 7. Two fresh traps on the swap path (a guide doesn't cross the street for you)

Even with a working reference, board-level facts remain yours to verify:

- **Clock name mismatch**: reference driver does `devm_clk_get(dev, "xclk")`;
  the Luckfox DTS convention is `clock-names = "xvclk"` (see the in-tree
  imx415 node). Probe fails with `-ENOENT` until they agree.
- **Reset GPIO polarity**: with `GPIO_ACTIVE_LOW` in the DTS, gpiod's logical
  1 is physically low — *holding reset*. The reference's
  `gpiod_direction_output(reset, 1)` kept the sensor in reset while reading
  its ID (`error -5`). Aligned with the old driver's proven sequence —
  0 (= released) plus a 120 ms settle — and probe passed.

> A skeleton solves the *systemic* differences; every GPIO name, clock name
> and polarity is still a board fact. Cross-check against your schematic and
> any previously-working code, one by one.

### Appendix: two ops traps that cost hours and looked like driver bugs

- **`/userdata` filled by rkipc.log** (227 MB of log on a 238 MB partition):
  symptoms were "push service won't start" and config-write `Errno 28`.
  `df -h` first. High-frequency error logs + tiny partitions = time bomb.
- **rkipc writes its in-memory config back to the ini on exit**: editing
  `/userdata/rkipc.ini` directly gets reverted when the process dies. Kill
  first, edit, and (if needed) `chattr +i` before restarting.

## Method notes for the next porter

1. **Instrument the boundaries before patching.** Three layers, one
   write/read experiment each, locates the break far faster than five
   "this must be it" fixes — ours were all real bugs, and all useless.
2. **Silent failure is the vendor-tree default.** Wrong I²C address, wrong
   control minimum, non-closing timing arithmetic — none of them log a word.
   The only trustworthy verification is *measuring the output*: v4l2 write,
   i2c read, pull the stream, look at the picture.
3. **The shape of the holes reveals architecture.** When the Nth fix deploys
   cleanly and the symptom doesn't move, and every hole belongs to the same
   family ("the two trees disagree about the same thing"), stop patching and
   swap skeletons.
4. **Pick a living reference**: same ecosystem as your target, claimed
   working on real hardware. Ours (armbian tree, same kernel lineage as the
   Luckfox BSP) also shipped the PDAF/embedded-data second pad — which the
   upcoming autofocus work (DW9807 VCM at I²C 0x0c) directly inherits.

## Hardware / environment snapshot

| Item | Value |
|---|---|
| Board | Luckfox Pico Zero (RV1106G3, eMMC) |
| Sensor | Raspberry Pi Camera Module 3 (IMX708, module TNBA1392) |
| Kernel | 5.10.160 (Luckfox BSP, Buildroot uclibc) |
| ISP | RV1106 ISP 3.2 — input capped at 3072×1728, hence 2304×1296@30 (2×2 binning) |
| Userspace | rkipc (RTSP :554) + ffmpeg restream |
| IQ | Manual white balance `mwbGain [2.52, 1.0, 1.0, 1.721]` (indoor LED; matches the value PieSoft published — mutual confirmation) |

## Repo contents

```
imx708-rv1106-bringup/
├── README.md            # this document
├── af/                  # Part 3: autofocus system (host daemon + board C agent + tester)
├── driver/imx708.c      # final driver (Rockchip skeleton + board-proven register table, 5.10.160)
├── driver/patches/      # af_snap RAWAF debugfs patch
└── dts/imx708-node.dtsi # reference DTS fragment: sensor node + 24 MHz fixed clock
```

To integrate into your own Luckfox BSP: copy the driver into
`sysdrv/source/kernel/drivers/media/i2c/` (add Kconfig/Makefile entries like
any in-tree sensor), merge the two DTS fragments into your board dtsi, and
build with `make ARCH=arm O=<objs> CROSS_COMPILE=<toolchain> M=drivers/media/i2c modules`.
DTS changes require a boot.img (FIT) rebuild — see trap 6 step 5.

Driver code: GPL-2.0 (same as upstream RPi/armbian sources). This document: CC BY 4.0.

**Verified status (2026-10-23)**: rkaiq AE loop working (errors at zero,
auto convergence), RTSP 2304×1296 + restream stable. Not yet done: autofocus
(DW9807 VCM node + `CONFIG_VIDEO_DW9807_VCM` + IQ `af_v31 contrast_af.enable`),
auto AWB tuning. One observed kernel-side ISP stall on stream stop
(`rkisp_stream_stop timeout`, recovered by reboot) — trigger not yet isolated.

## Part 3: Autofocus — three architectures in one day

2026-10-30. With the 3A loop stable, autofocus was the last missing piece.
The module carries a DW9800W-variant VCM at address `0x0c` (chip ID register
`0x00` reads `0xEB`, not the `0xF2` of genuine Dongwoon parts). The day went
through three architectures; the final one is what ships.

**The VCM control one-liner** (after a full day of "the lens is dead"):

```
i2ctransfer -y -f 4 w3@0x0c 0x03 <MSB-decimal> <LSB-decimal>
```

Busybox `i2ctransfer` **only accepts decimal** data bytes. Our first daemon
formatted DAC values as hex (`:02x`); every DAC value containing an a–f digit
silently failed, and the daemon happily continued on stale readings. This
single bug masqueraded as, in order: "the lens is stuck", "the VCM drifted",
"the VCM is broken", "the chip is fried". We nearly binned two healthy
modules over it. Check your tool's return codes.

**Architecture v3.1** (all three stages ran the same day):

1. **Image-space CDAF** — grab RTSP frames, Laplacian, hill-climb. Locked in
   60s, oscillated forever: RTSP buffering means the frame you score is
   always frames behind the lens position you just wrote. The lag is the
   oscillation.
2. **RAWAF hardware stats** — the RV1106 ISP computes a 225-zone RawAF grid;
   a tiny kernel patch (`driver/patches/afsnap-rawaf-debugfs.patch`) snapshots
   it to `/sys/kernel/debug/rkisp_af/stats`. 50ms per sample, no RTSP
   involved, 4.6× peak/valley discrimination. Locked in 6s. Two landmines:
   - The af_snap fill runs in the ISP **hard-IRQ context** — the first patch
     used a mutex and hard-locked the SoC under load (spin_lock_irqsave
     fixes it). Sleeping locks in IRQ context hard-lock silently.
   - The RAWAF level couples to AE gain and scene texture. **Absolute
     levels mean nothing across scenes**; only the peak/valley *structure*
     within one sweep carries focus information.
3. **On-board agent** (final) — a small C daemon on the board owns `/dev/i2c-4`
   and the stats file directly (`af/af_agent_c.c`); the host speaks a
   line protocol over one SSH connection. Zero process spawns per sample:
   **145ms per move+measure**, full-range sweep + lock in **~1s**,
   defocus recovery ~1s. The C agent also carries the **center-weighted**
   statistics (center 5×5 grid = 1.0, mid-ring 0.2, outer 0.02) — corner
   textures were hijacking the focus decision.

**Other traps hit on the way** (Part 3 continuation of the silent-trap list):

- `launchd`'s KeepAlive resurrects the daemon 30s after any kill. A hardware
  A/B experiment run while the daemon is alive measures *the daemon*, not the
  hardware. Bootout the service before touching hardware.
- The lens **controller** and the **AE** are coupled through the ISP: raising
  IQ `sharp_ratio` 10→15 changed the statistics the AE sees and blew the
  exposure. Any IQ edit must be verified against brightness *and* clarity.
- `rkipc` (via rockit) holds every rkcif channel's buffer queue. A second
  v4l2 opener of `stream_cif_mipi_id1` gets EBUSY forever; killing rkipc
  just gets you a watchdog restart. Embedding data (PDAF) therefore has **no
  zero-change path** under rkipc.
- The MAC-side pipeline reader must `select()` with a timeout. A half-dead
  SSH pipe (TCP alive, peer wedged) blocks `readline()` forever at 0% CPU —
  the daemon looks alive and does nothing. The board-side C agent also
  self-exits after 5s of stdin silence.

**PDAF status**: the sensor transmits phase data as MIPI embedded data
(DT `0x12`, one line, inside a 4-line metadata bundle — measured, not
guessed). We added `RKMODULE_GET_CHANNEL_INFO` to the driver declaring pad 1
as `EBD_1X8 5760×1`, and `stream_cif_mipi_id1` now enumerates `EBD8`. The
remaining blocker is enabling the media link while rkipc holds the pipeline
(see above). Data format per PieSoft's measurements: 12×16 grid, confidence
+ phase per zone, phase Q4 (divide by 16 = signed defocus in pixels).

**Resolution ceiling** (confirming PieSoft): the RV1106 ISP v3.2 caps input
at 3072×1728 (~5.3 MP, `CIF_ISP_INPUT_*_MAX_V32`), the two-pass stitching
that lifts this on bigger Rockchips is disabled, and the IMX708 has no native
mode between 2304 and 4608. 2304×1296@25 is the ceiling on this SoC.

## Part 3 repo layout

```
├── af/afd_fast.py          # host-side daemon: sentinel + image fine-scan + adaptive hill climb
├── af/af_agent_c.c         # board-side C agent (i2c-dev + stats, center-weighted)
├── af/af_agent.sh          # shell agent (reference; slow, superseded)
├── af/af_offline_test.py   # frame-library replay tester (blur/glitch injection)
├── driver/imx708.c         # final driver (+ RKMODULE_GET_CHANNEL_INFO, EBD pad)
└── driver/patches/afsnap-rawaf-debugfs.patch  # RAWAF → debugfs (spinlock version)
```

Deploy: `af_agent_c` runs on the board (launchd/`/userdata`), `afd_fast.py`
runs on the host via launchd (`KeepAlive`). Point both at the board's IP.

## Verified status (2026-10-30)

Autofocus **working end-to-end**: sweep+lock ~1s, defocus recovery ~1s,
center-weighted, range-limited (DAC 500–1000, user-calibrated), full
self-healing chain (AE rescue / pipe-timeout rebuild / agent suicide /
blackout suspend). Restream + RTSP stable. Resolution ceiling documented
above.

## (old verified status, kept for reference)

## Credits

- [PieSoft — Porting a Camera Sensor Driver Between Vendor Kernel Trees: IMX708 on Rockchip RV1106](https://piesoft.com/news/imx708-driver-port-rockchip-rv1106/):
  the starting point and backbone of this write-up. Their "silent failure"
  methodology, the "vendor driver is a questionnaire" framing, and the clock
  bug all came true, verbatim, in our debugging.
- [armbian/linux-rockchip#457](https://github.com/armbian/linux-rockchip/pull/457):
  the Rockchip-style imx708 driver we ultimately adopted as skeleton.
- [raspberrypi/linux](https://github.com/raspberrypi/linux) `drivers/media/i2c/imx708.c`:
  authoritative register tables and timing values.
- [LuckfoxTECH/luckfox-pico](https://github.com/LuckfoxTECH/luckfox-pico): BSP,
  build system and board references.
