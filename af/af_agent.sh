#!/bin/sh
# af-agent: 板端 AF 代理。Mac 通过 ssh 发单字符命令, 板内完成 VCM+统计。
# 协议(stdin→stdout, 每行一命令):
#   P<dac>      → 移动 VCM 并返回该点统计 (settle 0.25s 内置)
#   S           → 只读当前统计
#   V<dac>      → 只移 VCM 不测量
# 返回: 一行数值(统计和), 失败返回 ERR
AF=/sys/kernel/debug/rkisp_af/stats

mount -t debugfs none /sys/kernel/debug 2>/dev/null

read_stats() {
  # 中心加权: 15x15 网格, 中央7x7 权重1.0, 中环(距离4-5) 0.4, 外圈 0.1
  # 摄影惯例中央重点对焦; 防边角强纹理劫持对焦决策
  awk 'NR>2{
    i = NR-3
    gr = int(i/15); gc = i%15
    dr = gr-7; if(dr<0) dr=-dr
    dc = gc-7; if(dc<0) dc=-dc
    dist = (dr>dc)?dr:dc
    w = (dist<=3) ? 1.0 : ((dist<=5) ? 0.4 : 0.1)
    s += w*($1+$2+$3+$4)
  } END{printf "%.0f\n", s+0}' $AF 2>/dev/null
}

vcm_set() {  # $1=dac
  i2ctransfer -y -f 4 w2@0x0c 0x02 0x00 2>/dev/null
  i2ctransfer -y -f 4 w3@0x0c 0x03 $(( $1 >> 8 & 3 )) $(( $1 & 255 )) 2>/dev/null
}

while IFS= read -r line; do
  cmd=${line%${line#?}}
  arg=${line#?}
  case "$cmd" in
    P)
      vcm_set "$arg"
      sleep 0.12
      R=$(read_stats)
      [ -n "$R" ] && echo "$R" || echo ERR
      ;;
    V)
      vcm_set "$arg"
      echo OK
      ;;
    S)
      R=$(read_stats)
      [ -n "$R" ] && echo "$R" || echo ERR
      ;;
    *)
      echo ERR
      ;;
  esac
done
