# Deployment glue

Runtime pieces that sit around the driver/AF code. Paths shown are the
board's (`/userdata`, `/oem/usr/ko`) unless noted.

| file | goes to | purpose |
|---|---|---|
| `iq_imx708_TNBA1392.json` | `/userdata/iqfiles/imx708_Raspberry Pi Camera Module 3_TNBA1392.json` | IQ tuning (RPi CM3 derived). AF stats (`contrast_af.enable=1`) and sharpening 15 live here |
| `load_imx708.sh` | `/oem/usr/ko/load_imx708.sh` | boot-time sensor load with backoff retry (I²C timing at cold boot fails ~50% of the time) |
| `af_agent_c_arm` | `/userdata/af_agent_c` (chmod +x) | board-side AF agent binary (static, uClibc-safe) |
| `af_agent.sh` (see `../af/`) | `/userdata/af_agent.sh` | shell reference agent |
| `push_stream.sh` | `/userdata/push_stream.sh` | restream watchdog: rkipc → RTSP → MediaMTX |
| `com.haoming.afd.plist` | Mac `~/Library/LaunchAgents/` | host-side AF daemon auto-start |

Startup order on the board: rkcif/imx708 modules → `load_imx708.sh`
(retry loop) → rkipc (RTSP) → `push_stream.sh` (restream). The host daemon
(`afd_fast.py`) talks to `af_agent_c` over one SSH channel.

## Frame library for offline testing

Record one: sweep DAC 500→1000 step 25, at each point settle ~1s then save
5 frames (`af/af_offline_test.py` expects `dacNNNN_fK.jpg` + `summary.csv`).
The offline tester never touches the board — iterate the algorithm against
the library, verify on hardware once.
