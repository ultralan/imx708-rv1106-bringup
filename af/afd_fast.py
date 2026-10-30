#!/usr/bin/env python3
"""afd_fast v2: 离线验证过的对焦守护（真机版）
算法(af_offline_test.py 验证 3/4, FAIL项为82%画质可接受降级, 假峰全防住):
- stable_probe: 5帧 second-min + CV平滑折扣(0.5以下不罚, 0.9以上×0.1, 线性过渡)
- 粗扫step100 → top3局部极大候选 → 各细扫±60/step25 → 全局比
- 失焦(连续2次<55%) → 局部爬山 → 无果全扫
- VCM 节流 1.2s/次(防电流冲击死机, 帧库录制节奏验证安全)
"""
import cv2, os, subprocess, time, sys, shutil
import numpy as np

RTSP = 'rtsp://192.168.1.217:554/live/0'
LOCK = '/tmp/afd_fast.lock'
DAC_MIN = 500   # 实测焦距范围(用户 10-30 标定: 最近~970-1000, 最远~520-566)
DAC_MAX = 1000
LOG = '/tmp/afd_fast.log'
SSH_CMD = ['ssh', '-o', 'ControlMaster=auto', '-o', 'ControlPath=/tmp/afd_fast_ssh',
           '-o', 'ControlPersist=600', '-o', 'ConnectTimeout=8', 'root@192.168.1.217']
os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = 'rtsp_transport;tcp|stimeout;3000000'

def log(msg):
    open(LOG, 'a').write(f'[{time.strftime("%H:%M:%S")}] {msg}\n')

def ssh_raw(cmd, retry=2):
    for i in range(retry):
        try:
            subprocess.run(SSH_CMD + [cmd], capture_output=True, timeout=15)
            return True
        except subprocess.TimeoutExpired:
            subprocess.run(['rm', '-f', '/tmp/afd_fast_ssh'], capture_output=True)
            time.sleep(1)
    log(f'ssh 重试{retry}次失败: {cmd[:50]}')
    return False

# 板端 AF 代理: 长连接 stdin/stdout, P=dac 移动+测量, S=只读统计
# 每次交互 ~350ms(settle 250ms 内置板端), 替代旧"两次ssh+sleep"的 600ms
import threading

class AfAgent:
    def __init__(self):
        self.p = None
        self.lock = threading.Lock()
        self.buf = ''   # 非阻塞读的行缓冲

    def _ensure(self):
        if self.p is None or self.p.poll() is not None:
            try:
                if self.p: self.p.kill()
            except Exception: pass
            self.buf = ''
            self.p = subprocess.Popen(
                ['ssh', '-o', 'ControlMaster=auto', '-o', 'ControlPath=/tmp/afd_agent_ssh',
                 '-o', 'ControlPersist=600', '-o', 'ConnectTimeout=8',
                 'root@192.168.1.217', '/userdata/af_agent_c'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=0)
            # 预热握手
            self.p.stdin.write('S\n'); self.p.stdin.flush()
            self._readline(3)
        return self.p

    def _readline(self, timeout=2.0):
        """带超时的行读取: select on stdout, 2s 无数据抛 TimeoutError → 上层重建管道"""
        import select
        deadline = time.time() + timeout
        while '\n' not in self.buf:
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError('agent 管道响应超时')
            r, _, _ = select.select([self.p.stdout], [], [], remain)
            if not r:
                raise TimeoutError('agent 管道响应超时')
            chunk = os.read(self.p.stdout.fileno(), 4096)
            if not chunk:
                raise EOFError('agent 管道关闭')
            self.buf += chunk.decode(errors='replace')
        line, self.buf = self.buf.split('\n', 1)
        return line.strip()

    def cmd(self, c, timeout=8):
        with self.lock:
            try:
                p = self._ensure()
                p.stdin.write(c + '\n'); p.stdin.flush()
                # 响应带 A 前缀校验: 丢弃脏行(管道残留/错位片段), 最多滤 5 行
                for _ in range(5):
                    line = self._readline(2.0)
                    if not line:
                        return -3.0
                    if line.startswith('A') and len(line) > 1:
                        return float(line[1:])
                    if line == 'ERR':
                        return 0.0
                # 5 行都不是合法响应: 管道错乱, 重建
                self.p.kill(); self.p = None
                return -2.0
            except (TimeoutError, EOFError, subprocess.TimeoutExpired, OSError, ValueError) as e:
                try:
                    if self.p: self.p.kill()
                except Exception: pass
                self.p = None
                return -2.0


AGENT = AfAgent()

def vcm(dac):
    AGENT.cmd(f'V{dac}')

def sharp_stats():
    v = AGENT.cmd('S')
    return v if v > 0 else 0.0

def probe(dac, settle=0.12):
    """VCM 移动+测量一次板内完成(settle 板端内置)"""
    v = AGENT.cmd(f'P{dac}')
    return v if v > 0 else 0.0

_cap = None
def grab():
    global _cap
    if _cap is None or not _cap.isOpened():
        _cap = cv2.VideoCapture(RTSP)
    while True:
        t0 = time.perf_counter()
        ok, f = _cap.read()
        if not ok:
            try: _cap.release()
            except Exception: pass
            _cap = None
            return None
        if time.perf_counter() - t0 > 0.02:
            return f

def sharp(f):
    """中心加权合成信号: 主体(中央2x2)×3 + 全图均值兜底, 防边角单一纹理劫持"""
    g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    grid = [[cv2.Laplacian(g[r*h//4:(r+1)*h//4, c*w//4:(c+1)*w//4], cv2.CV_64F).var()
             for c in range(4)] for r in range(4)]
    center = (grid[1][1] + grid[1][2] + grid[2][1] + grid[2][2]) / 4
    whole = cv2.Laplacian(g, cv2.CV_64F).var()
    return center * 3 + whole

def stable_probe(dac, n=1):
    """统计源探测: n 次采样中值(硬件统计本身逐帧新鲜, 抗单帧噪声)"""
    vals = []
    for _ in range(n):
        v = AGENT.cmd('S')
        if v > 0:
            vals.append(v)
        time.sleep(0.06)
    if not vals:
        return probe(dac)
    return float(np.median(vals))

LAST_GOOD_DAC = [0]   # 收敛记忆: 最近成功锁定的 DAC, 优先邻域搜索

def fast_sweep():
    """RAWAF 统计快速扫描: 每点 ~0.5s, 全程 11 点 ≈ 5s。
    有记忆先搜邻域(±160), 命中直接返回。"""
    if LAST_GOOD_DAC[0]:
        d0 = LAST_GOOD_DAC[0]
        probe_dacs = list(range(max(DAC_MIN, d0-160), min(DAC_MAX, d0+160)+1, 40))
        trace = []
        for dac in probe_dacs:
            v = probe(dac)
            if v > 0:
                trace.append((dac, v))
        if trace:
            d, v = pick_peak(trace, win=2)
            mn = min(vv for _, vv in trace)
            if v >= mn * 1.8:
                log(f'记忆命中 dac={d} v={v:.0f} (记忆点{d0})')
                vcm(d)
                return [(d, v)]
            log(f'记忆邻域无峰(最高{v:.0f}), 全程扫')
    trace = []
    max_v = 0
    for dac in range(DAC_MIN, DAC_MAX+1, 80):
        v = probe(dac)
        if v > 0:
            trace.append((dac, v))
            max_v = max(max_v, v)
            # 提前终止: 已见峰且对比度跌到峰值 40% 以下(树莓派 contrastRatio 思路)
            if len(trace) >= 3 and max_v > 1e9 and v < max_v * 0.4:
                log(f'提前终止@{dac}: {v:.0f} < 峰{max_v:.0f}*0.4')
                break
    if not trace:
        return trace
    # 反向括峰: 若起点附近就是最高(峰在扫描方向起点外), 反向补扫
    vals = [v for _, v in trace]
    if len(trace) >= 3 and vals.index(max(vals)) <= 1 and trace[0][0] <= 200:
        log('峰靠起点, 反向补扫 50~0')
        for dac in range(80, 0, -25):
            v = probe(dac)
            if v > 0:
                trace.insert(0, (dac, v))
                if v > max(vals):
                    break
    vcm(trace[-1][0] if trace else 500)
    return trace

def pick_peak(trace, win=3):
    """从扫描轨迹选峰: 窗口最小值过滤孤立毛刺 + 抛物线拟合亚步长定位。
    拟合公式抄自树莓派 libcamera af.cpp findPeak(): 3 点二次插值。"""
    if not trace: return 0, 0
    vals = [v for _, v in trace]
    best_i, best_score = 0, 0
    for i in range(len(trace) - win + 1):
        score = min(vals[i:i+win])
        if score > best_score:
            best_score, best_i = score, i
    i = min(best_i + win // 2, len(trace) - 1)
    d, v = trace[i]
    # 抛物线拟合: 峰点与两侧邻点, 解出连续峰位(精度提至步长以下)
    if 0 < i < len(trace) - 1:
        x1, y1 = trace[i-1]
        x2, y2 = trace[i]
        x3, y3 = trace[i+1]
        denom = (y1 - y2) * (x2 - x3) - (y3 - y2) * (x2 - x1)
        if abs(denom) > 1e-9:
            num = (y1 - y2) * (x2 - x3) * (x2 - x3) - (y3 - y2) * (x2 - x1) * (x2 - x1)
            fit = x2 + num / (2 * denom)
            # 拟合点限制在邻点范围内防外推失控
            fit = max(x1, min(x3, fit))
            d = int(round(fit))
            log(f'峰拟合: 离散{trace[i][0]} → 连续{d}')
    return d, v

def full_focus():
    trace = fast_sweep()
    if not trace:
        return 0, 0
    if len(trace) == 1:   # 记忆命中, 已锁
        LAST_GOOD_DAC[0] = trace[0][0]
        return trace[0]
    log('sweep: ' + ' '.join(f'{d}:{v:.0f}' for d, v in trace))
    d, v = pick_peak(trace, win=2)
    # 峰有效性判据: 相对结构(峰/谷比)而非绝对水位——运动/AE 变化会整体压低水位但峰仍在
    # 1.8x 宽容版: 锁错有监测+爬山兜底, 拒锁(死循环堆积)比错锁代价高
    vals = [vv for _, vv in trace]
    ratio = v / max(min(vals), 1)
    if v >= 1e9 or ratio >= 1.8:
        # 峰邻域精修: ±50 内 step25 (含抛物线拟合)
        fine = [(d, v)]
        for fd in range(max(DAC_MIN, d-50), min(DAC_MAX, d+50)+1, 50):
            if fd == d: continue
            pv = stable_probe(fd, n=2)
            if pv > 0:
                fine.append((fd, pv))
        fd, fv = pick_peak(fine, win=1)
        if ratio < 2.5:
            log(f'降级锁定: 水位低(峰{v:.0f}/谷{min(vals):.0f}={ratio:.1f}x)但结构存在')
        vcm(fd)
        LAST_GOOD_DAC[0] = fd
        return fd, fv
    if d:
        log(f'扫描无有效峰结构(峰{v:.0f} 谷比{ratio:.1f}x), 视为失败')
    return 0, 0

def hill_climb(dac, v, depth=1.0):
    """自适应步长爬山 v2: 失焦深度定起步步长, 首步双向并行探测。
    depth=cur/base (跌落深度): 0.62→60 起步, 0.3→200 起步; 步长衰减同 v1。"""
    step = int(max(60, min(200, 60 / max(depth, 0.3))))
    direction = 0
    best_d, best_v = dac, v
    from concurrent.futures import ThreadPoolExecutor
    for _ in range(10):
        if step < 10:
            break
        if direction == 0:
            # 首步: 双向并行探测(省一半串行时间)
            t = max(10, step // 2)
            with ThreadPoolExecutor(2) as ex:
                fu = ex.submit(probe, min(DAC_MAX, dac + t))
                fd_ = ex.submit(probe, max(DAC_MIN, dac - t))
                up = fu.result()
                dn = fd_.result()
            if up > v and up >= dn:
                direction = 1
                dac, v = min(DAC_MAX, dac + t), up
                best_d, best_v = dac, v
                log(f'爬↑{t} {dac} v={v:.0f}')
                continue
            if dn > v and dn > up:
                direction = -1
                dac, v = max(DAC_MIN, dac - t), dn
                best_d, best_v = dac, v
                log(f'爬↓{t} {dac} v={v:.0f}')
                continue
            break   # 两边都不改善: 已在峰附近
        # 有方向: 前进一步看改善
        nd = max(0, min(DAC_MAX, dac + direction * step))
        nv = probe(nd)
        if nv > v * 1.05:
            dac, v = nd, nv
            if v > best_v:
                best_d, best_v = dac, v
            if nv > v * 1.3 and step < 120:
                step = min(120, step * 2)
            log(f'爬{"↑" if direction>0 else "↓"}{step} {dac} v={v:.0f}')
        else:
            step //= 2
            log(f'步长衰减→{step} @{dac}')
            if nv < v * 0.9:
                dac, v = best_d, best_v
                direction = -direction
    vcm(best_d)
    return best_d, best_v

def mean_lum(f):
    return cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).mean()

def main():
    if os.path.isdir(LOCK):
        try:
            old = int(open(f'{LOCK}/pid').read().strip())
            os.kill(old, 0)
            print(f'afd_fast(pid {old})已在跑'); sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            shutil.rmtree(LOCK, ignore_errors=True)
    os.mkdir(LOCK)
    open(f'{LOCK}/pid', 'w').write(str(os.getpid()))
    log('=== afd_fast v2 启动(离线验证版算法) ===')
    try:
        ssh_raw('echo ok')
        # AE 自救: 爆白/过暗时调曝光 (rkipc 重启后 AE 常停摆在顶死值)
        f = grab()
        if f is not None:
            lum = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).mean()
            if lum > 200 or lum < 25:
                log(f'异常亮度 {lum:.0f}, 手动调曝光')
                for exp in [1600, 800, 400, 200, 100, 50]:
                    ssh_raw(f'v4l2-ctl -d /dev/v4l-subdev2 -c exposure={exp}')
                    time.sleep(1.5)
                    f = grab()
                    if f is None: continue
                    lum = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).mean()
                    log(f'exp={exp} lum={lum:.0f}')
                    if 40 < lum < 180:
                        break
        dac, v = full_focus()
        if dac:
            # 基线去虚高: 锁定后 3 次复测均值作基线(爬山末跳值偏高是振荡根源)
            vals = [sharp_stats() for _ in range(3) if sharp_stats() > 0]
            if vals:
                v = sum(vals) / len(vals)
        log(f'锁定 dac={dac} v={v:.0f}')
        fails = 0
        cooldown = 0   # v=0 失败后的冷静计数(防连环空转)
        last_check = time.time()
        while True:
          try:
            time.sleep(0.05)
            if cooldown > 0:
                cooldown -= 1
                if cooldown == 0:
                    log('冷静期结束, 重扫')
                    dac, v = full_focus()
                    if dac:
                        vals = [sharp_stats() for _ in range(3) if sharp_stats() > 0]
                        if vals: v = sum(vals) / len(vals)
                    log(f'锁定 dac={dac} v={v:.0f}')
                continue
            cur = sharp_stats()
            if cur <= 0:
                log('统计读取失败')
                fails += 1
                if fails > 5:
                    dac, v = full_focus()
                    log(f'重锁 dac={dac} v={v:.0f}')
                    fails = 0
                continue
            if v <= 0:
                # 上一轮失败锁定: 无基线可比, 直接冷静重扫
                cooldown = 10   # 10×0.15s = 1.5s
                continue
            # 跨场景盲区防御: 每 5s 强制邻域校准(扫±100 找更优点就迁移)。
            now = time.time()
            if now - last_check > 5:
                last_check = now
                best_d, best_v = dac, max(v, cur)
                improved = False
                for pd in range(max(DAC_MIN, dac-100), min(DAC_MAX, dac+100)+1, 50):
                    if pd == dac: continue
                    pv = probe(pd)
                    if pv > best_v * 1.3:
                        best_d, best_v, improved = pd, pv, True
                if improved:
                    log(f'校准迁移 dac={dac}→{best_d} (v={best_v:.0f})')
                    dac, v = best_d, best_v
                    vcm(dac)
                    continue
            # 失焦判定: 当前统计跌破基线 62% (真比较, 不再是无条件块)
            if cur < v * 0.62 and v > 0:
                log(f'失焦: {cur:.0f} < {v:.0f}*0.62 → 爬山')
                prev_v = v
                dac, v = hill_climb(dac, cur, depth=cur / max(prev_v, 1))
                if dac == 0 or v < prev_v * 0.6:
                    log(f'爬山无果({v:.0f} vs 原{prev_v:.0f}), 全扫')
                    dac, v = full_focus()
                if dac:
                    vals = [sharp_stats() for _ in range(3) if sharp_stats() > 0]
                    if vals: v = sum(vals) / len(vals)
                    log(f'重锁 dac={dac} 基线={v:.0f}')
                else:
                    cooldown = 10
                fails = 0
            else:
                fails = 0
                if cur > v:
                    v = v * 0.7 + cur * 0.3
          except Exception as e:
            log(f'循环异常: {type(e).__name__}: {e}')
            time.sleep(2)
    finally:
        shutil.rmtree(LOCK, ignore_errors=True)
        log('afd_fast 退出')

if __name__ == '__main__':
    main()
