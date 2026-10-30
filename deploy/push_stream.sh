#!/bin/sh
# Luckfox / RV1106 摄像头 RTSP 中继守护脚本

SOURCE="${SOURCE:-rtsp://127.0.0.1:554/live/0}"
WIFI_IFACE="${WIFI_IFACE:-wlan0}"
RKIPC="${RKIPC:-/oem/usr/bin/rkipc}"
RKIPC_IQ_DIR="${RKIPC_IQ_DIR:-/userdata/iqfiles}"
LOG_DIR="${LOG_DIR:-/userdata}"
CONFIG_HELPER="${CONFIG_HELPER:-/userdata/device_config.py}"
DEVICE_CONFIG_ENV="${DEVICE_CONFIG_ENV:-/run/device_config.env}"
PROG="/tmp/push_stream.progress"
STATUS_FILE="/run/push_stream.status.json"

log() { echo "[$(date '+%F %T' 2>/dev/null || date)] $*"; }
fail() { log "错误：$*"; exit 1; }
write_status() {
  state="$1"
  now=$(date +%s 2>/dev/null || echo 0)
  printf '{"state":"%s","updated_at":%s}\n' "$state" "$now" >"$STATUS_FILE.tmp" && mv "$STATUS_FILE.tmp" "$STATUS_FILE"
}
cleanup() {
  write_status "stopped"
  if [ -n "${FPID:-}" ]; then
    kill "$FPID" 2>/dev/null || true
    wait "$FPID" 2>/dev/null || true
  fi
  exit 0
}
trap cleanup INT TERM

[ "$(id -u 2>/dev/null)" = 0 ] || fail "请以 root 运行"
[ -x "$RKIPC" ] || fail "找不到可执行的 rkipc：$RKIPC"
command -v ffmpeg >/dev/null 2>&1 || fail "PATH 中找不到 ffmpeg"
[ -d "/sys/class/net/$WIFI_IFACE" ] || fail "无线网卡不存在：$WIFI_IFACE"
mkdir -p "$LOG_DIR" || fail "无法创建日志目录：$LOG_DIR"

[ -r "$CONFIG_HELPER" ] || fail "找不到统一配置工具"
python3 "$CONFIG_HELPER" render || fail "无法读取设备配置"
[ -r "$DEVICE_CONFIG_ENV" ] || fail "统一配置环境文件不存在"
# 文件由 root 配置服务生成并限制为 600；配置内容不得进入日志。
. "$DEVICE_CONFIG_ENV"
# 供 rkipc 加载 OEM 下的 Rockchip 多媒体库。
export LD_LIBRARY_PATH="/oem/usr/lib:/oem/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

BAD=0
AE_LAST=0
AE_FAIL=0
while :; do
  if [ -z "${WIFI_SSID:-}" ] || [ -z "${WIFI_PASSWORD:-}" ] || [ -z "${TARGET:-}" ]; then
    write_status "waiting_configuration"
    sleep 3
    continue
  fi
  if ! ip -4 -o addr show dev "$WIFI_IFACE" 2>/dev/null | grep -q ' inet '; then
    write_status "waiting_network"
    sleep 3
    continue
  fi
  iw dev "$WIFI_IFACE" set power_save off 2>/dev/null || true

  if ! pidof rkipc >/dev/null 2>&1; then
    log "启动 rkipc"
    "$RKIPC" -a "$RKIPC_IQ_DIR" >>"$LOG_DIR/rkipc.log" 2>&1 &
    sleep 8
    BAD=0
  fi

  log "开始转发到已配置目标"
  write_status "starting"
  rm -f "$PROG"
  ffmpeg -loglevel error -rtsp_transport tcp \
    -fflags nobuffer -max_delay 0 -probesize 32768 -analyzeduration 0 \
    -i "$SOURCE" -an -c copy -max_interleave_delta 0 \
    -f rtsp -rtsp_transport tcp -flush_packets 1 -progress "$PROG" \
    "$TARGET" 2>/dev/null &
  FPID=$!
  t0=$(cut -d. -f1 /proc/uptime 2>/dev/null || date +%s)
  last=""
  stall=0
  backlog=0

  while kill -0 "$FPID" 2>/dev/null; do
    state=$(awk '{print $3}' "/proc/$FPID/stat" 2>/dev/null)
    [ "$state" = Z ] && break
    sleep 2
    qhex=$(awk '$2=="0100007F:022A"{split($5,a,":"); print a[1]}' /proc/net/tcp 2>/dev/null | head -1)
    q=$((0x${qhex:-0}))
    if [ "$q" -gt 500000 ]; then backlog=$((backlog + 1)); else backlog=0; fi
    if [ "$backlog" -ge 2 ]; then
      log "本机 RTSP 连接发送队列积压 ${q}B，重启转发"
      kill "$FPID" 2>/dev/null
      break
    fi

    cur=$(grep -a '^out_time_ms=' "$PROG" 2>/dev/null | tail -1)
    if [ -n "$cur" ] && [ "$cur" = "$last" ]; then stall=$((stall + 1)); else
      stall=0
      if [ -n "$cur" ]; then write_status "streaming"; fi
    fi
    last="$cur"
    if [ "$stall" -ge 5 ]; then
      log "转发进度停滞 10 秒，重连"
      write_status "retrying"
      kill "$FPID" 2>/dev/null
      break
    fi
  done
  wait "$FPID" 2>/dev/null

  t1=$(cut -d. -f1 /proc/uptime 2>/dev/null || date +%s)
  dur=$((t1 - t0))
  log "转发退出（运行 ${dur}s），3 秒后重试"
  write_status "retrying"
  if [ "$dur" -lt 10 ]; then
    BAD=$((BAD + 1))
    if [ "$BAD" -ge 3 ]; then
      if netstat -tln 2>/dev/null | grep -q ":554 .*LISTEN"; then
        log "连续快速退出但 rkipc 存活（554 在监听），仅重连转发"
      else
        log "rkipc 无监听，判定真死，重启 rkipc"
        killall rkipc 2>/dev/null || true
      fi
      BAD=0
    fi
  else
    BAD=0
  fi

  # AE 亮度轮询（每 180s，内嵌本常驻服务；独立进程会被 init 孤儿回收）
  NOW=$(cut -d. -f1 /proc/uptime)
  if [ $((NOW - AE_LAST)) -ge 180 ] 2>/dev/null; then
    AE_LAST=$NOW
    rm -f /tmp/ae_p.raw
    ffmpeg -rtsp_transport tcp -analyzeduration 1000000 \
      -i rtsp://127.0.0.1:554/live/0 -frames:v 1 \
      -f rawvideo -pix_fmt gray -y /tmp/ae_p.raw </dev/null >/dev/null 2>&1 &
    AEPID=$!
    AEI=0
    while [ $AEI -lt 15 ]; do
      kill -0 $AEPID 2>/dev/null || break
      sleep 1
      AEI=$((AEI + 1))
    done
    kill -9 $AEPID 2>/dev/null
    if [ -s /tmp/ae_p.raw ]; then
      AE_FAIL=0
      AEM=$(od -An -tu1 -j200000 -N99000 /tmp/ae_p.raw 2>/dev/null | tr -s ' ' '\n' | grep -v '^$' | awk '{s+=$1;n++} END {printf "%d", (n>0)?s/n:0}')
      AEC=$(v4l2-ctl -d /dev/v4l-subdev2 -l 2>/dev/null | grep -oE 'exposure.*value=[0-9]+' | grep -oE '[0-9]+$')
      if [ -n "$AEM" ] && [ "$AEM" -gt 0 ] && [ -n "$AEC" ]; then
        AEE=$((120 - AEM))
        # 死区: |err|<12 不调(防振荡); KP=8 阻尼(30 会过冲引发曝光震荡)
        if [ "$AEE" -gt 12 ] || [ "$AEE" -lt -12 ]; then
          AEN=$((AEC + 8 * AEE))
          [ $AEN -gt 3900 ] && AEN=3900
          [ $AEN -lt 200 ] && AEN=200
          v4l2-ctl -d /dev/v4l-subdev2 -c exposure=$AEN >/dev/null 2>&1
        else
          AEN=$AEC
        fi
        echo "[$(date '+%T')] AE mean=$AEM exp $AEC->$AEN" >> /userdata/ae_daemon.log

        # ---- AF: 爬山对焦（相邻差绝对值和作清晰度, 与 AE 共帧）----
        aes_snap() { # 抓当前帧清晰度
          ffmpeg -rtsp_transport tcp -i rtsp://127.0.0.1:554/live/0 -frames:v 1 \
            -f rawvideo -pix_fmt gray -y /tmp/af_s.raw </dev/null >/dev/null 2>&1 &
          SP=$!; SI=0
          while [ $SI -lt 12 ]; do kill -0 $SP 2>/dev/null || break; sleep 1; SI=$((SI+1)); done
          kill -9 $SP 2>/dev/null
          [ -s /tmp/af_s.raw ] || { echo 0; return; }
          od -An -tu1 -j200000 -N99000 /tmp/af_s.raw 2>/dev/null | tr -s ' ' '\n' | grep -v '^$' | awk 'NR>1{d=$1-p; if(d<0)d=-d; s+=d} {p=$1} END {printf "%d", s}'
        }
        af_move() { # $1=DAC
          i2cset -y -f 4 0x0c 0x02 0x01 b >/dev/null 2>&1
          i2cset -y -f 4 0x0c 0x03 $(($1 >> 8 & 3)) b >/dev/null 2>&1
          i2cset -y -f 4 0x0c 0x04 $(($1 & 255)) b >/dev/null 2>&1
        }

        AES=$(od -An -tu1 -j200000 -N99000 /tmp/ae_p.raw 2>/dev/null | tr -s ' ' '\n' | grep -v '^$' | awk 'NR>1{d=$1-p; if(d<0)d=-d; s+=d} {p=$1} END {printf "%d", s}')
        # 曝开门限: AE 未稳(|mean-120|>25)时清晰度不可信(过曝/欠曝欺骗 AF 算子)
        if [ -n "$AES" ] && [ "$AES" -gt 0 ] && [ "$AEM" -gt 95 ] && [ "$AEM" -lt 145 ]; then
          if [ -z "$AF_DAC" ]; then
            # 首次: 粗扫三点定起点
            i2cset -y -f 4 0x0c 0x02 0x01 b >/dev/null 2>&1
            af_move 300; sleep 2; S3=$(aes_snap)
            af_move 500; sleep 2; S5=$(aes_snap)
            af_move 400; sleep 2; S4=$AES
            AF_BEST=400; AF_BS=$S4
            [ "$S3" -gt "$AF_BS" ] 2>/dev/null && { AF_BEST=300; AF_BS=$S3; }
            [ "$S5" -gt "$AF_BS" ] 2>/dev/null && { AF_BEST=500; AF_BS=$S5; }
            af_move $AF_BEST
            AF_DAC=$AF_BEST; AF_BASE=$AF_BS; AF_DIR=0
            echo "[$(date '+%T')] AF init scan S3=$S3 S4=$S4 S5=$S5 → dac=$AF_BEST" >> /userdata/ae_daemon.log
          elif [ "$AES" -lt $((AF_BASE * 6 / 10)) ]; then
            # 清晰度崩塌仅记日志（重扫开销大且单核下不可靠；
            # 吊坠场景对焦距离固定，开机一次粗扫足够，剧变时重启设备重扫）
            echo "[$(date '+%T')] AF sharp drop $AES (base $AF_BASE) — reboot to rescan" >> /userdata/ae_daemon.log
            AF_BASE=$AES
          else
            # 正常时更新基线(缓变跟踪)
            AF_BASE=$(( (AF_BASE * 3 + AES) / 4 ))
          fi
        fi
      fi
    else
      AE_FAIL=$((AE_FAIL + 1))
      echo "[$(date '+%T')] AE grab fail #$AE_FAIL" >> /userdata/ae_daemon.log
      # rkipc 老化自愈：AE 连续 8 次拉不到帧 → 重启 rkipc
      if [ "$AE_FAIL" -ge 8 ]; then
        log "AE 连续失败 $AE_FAIL 次，rkipc 疑似老化，主动重启"
        killall rkipc 2>/dev/null || true
        AE_FAIL=0
      fi
    fi
  fi
  sleep 3
done
