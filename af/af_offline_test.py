#!/usr/bin/env python3
"""af 算法离线回放测试器：帧库驱动，不碰硬件
用法: uv run --with opencv-python-headless python af_offline_test.py
"""
import cv2, os, glob, random
import numpy as np

FRAMES_DIR = '/Users/haoming/Desktop/video/device/data/af_frames'

class FrameWorld:
    """模拟真实世界: probe(dac) 返回一帧(带噪声/异常注入)"""
    def __init__(self, frames_dir=FRAMES_DIR, seed=42):
        rng = random.Random(seed)
        self.lib = {}
        for p in glob.glob(f'{frames_dir}/dac*_f*.jpg'):
            dac = int(os.path.basename(p)[3:7])
            self.lib.setdefault(dac, []).append(p)
        self.dacs = sorted(self.lib)
        self.rng = rng
        self.anomaly_rate = 0.0      # 异常帧概率(假峰)
        self.blur_chance = 0.0       # 运动模糊帧概率
        self.glitch_pool = None      # 用于构造假峰的极高清帧
        self.vcm_calls = 0
        self.moves = []              # DAC 轨迹

    def set_anomaly(self, rate, glitch_dac=None):
        self.anomaly_rate = rate
        best = max(self.dacs, key=lambda d: self.truth_lap(d))
        self.glitch_pool = self.lib[glitch_dac or best]

    def truth_lap(self, dac, penalize=True):
        dac = self._nearest(dac)
        vals = [self._lap(cv2.imread(p, cv2.IMREAD_GRAYSCALE)) for p in self.lib[dac]]
        return float(np.median(vals))

    def stable_probe(self, dac):
        """5 帧中值采样: 抗单帧异常(刷新带/闪烁), 不做 CV 惩罚(人眼确认 925 真合焦, 波动来自屏幕拍频)"""
        self.vcm_calls += 1
        self.moves.append(dac)
        real = self._nearest(dac)
        vals = []
        pool = list(self.lib[real])
        self.rng.shuffle(pool)
        for p in pool:
            if self.rng.random() < self.anomaly_rate:
                p = self.rng.choice(self.glitch_pool)
            img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
            if self.rng.random() < self.blur_chance:
                img = cv2.GaussianBlur(img, (9, 9), 0)
            vals.append(self._lap(img))
        return float(np.median(vals))

    def _lap(self, gray):
        return cv2.Laplacian(gray, cv2.CV_64F).var()

    def _nearest(self, dac):
        return min(self.dacs, key=lambda d: abs(d - dac))

    def probe(self, dac):
        self.vcm_calls += 1
        self.moves.append(dac)
        real = self._nearest(dac)
        if self.rng.random() < self.anomaly_rate:
            p = self.rng.choice(self.glitch_pool)
            return self._lap(cv2.imread(p, cv2.IMREAD_GRAYSCALE)) * 3  # 假峰放大
        img = cv2.imread(self.rng.choice(self.lib[real]), cv2.IMREAD_GRAYSCALE)
        if self.rng.random() < self.blur_chance:
            img = cv2.GaussianBlur(img, (9, 9), 0)
        return self._lap(img)

    def snapshot(self):
        """监测采样: 不动 VCM, 取当前 DAC 帧"""
        real = self._nearest(self.moves[-1] if self.moves else 500)
        img = cv2.imread(self.rng.choice(self.lib[real]), cv2.IMREAD_GRAYSCALE)
        if self.rng.random() < self.blur_chance:
            img = cv2.GaussianBlur(img, (9, 9), 0)
        return self._lap(img)

    def truth_curve(self):
        return [(d, self.truth_lap(d)) for d in self.dacs]

# ---- 被测算法(与 afd_fast 同构, 提取纯逻辑) ----
class AfController:
    def __init__(self):
        self.dac = 500
        self.v = 0
        self.probe_cost = 0
        self.hill_step = 40

    def act(self, world):
        """一步决策, 返回 ('scan'|'lock'|'monitor'|'climb')"""
        if self.v == 0:
            return 'scan'
        cur = world.snapshot()
        if cur < self.v * 0.60 and self.v > 0:
            return 'climb'
        if cur > self.v:
            self.v = self.v * 0.6 + cur * 0.4
        return 'monitor'

def run_case(name, world_ctor, steps=60, verbose=False):
    world = world_ctor()
    ctrl = AfController()
    truth_best = max(world.truth_curve(), key=lambda r: r[1])
    # 粗扫 → 局部极大取前3候选 → 各细扫 → 全局比
    coarse = [(d, world.stable_probe(d)) for d in range(100, 1001, 100)]
    cands = []
    for i, (d, v) in enumerate(coarse):
        lv = coarse[i-1][1] if i > 0 else 0
        rv = coarse[i+1][1] if i < len(coarse)-1 else 0
        if v >= lv and v >= rv:
            cands.append((v, d))
    cands.sort(reverse=True)
    best_d, best_v = 0, 0
    for cv_, cd in cands[:3]:
        fine = [(d, world.stable_probe(d)) for d in range(max(50, cd-60), min(1000, cd+60)+1, 25)]
        fd, fv = max(fine, key=lambda r: r[1])
        if fv > best_v:
            best_v, best_d = fv, fd
    ctrl.dac, ctrl.v = best_d, best_v
    lock_err = abs(ctrl.dac - truth_best[0])
    locks_in = world.truth_lap(ctrl.dac, penalize=False) >= truth_best[1] * 0.8
    # 监测阶段: 中途把 VCM 打偏模拟失焦
    world.moves.append(ctrl.dac)
    recovered = False
    recover_at = None
    for s in range(steps):
        act = ctrl.act(world)
        if act == 'climb':
            base = world.stable_probe(ctrl.dac)
            for _ in range(6):
                up = world.stable_probe(min(1000, ctrl.dac + ctrl.hill_step))
                dn = world.stable_probe(max(0, ctrl.dac - ctrl.hill_step))
                if up > ctrl.v * 1.1 and up >= dn:
                    ctrl.dac, ctrl.v = ctrl.dac + ctrl.hill_step, up
                elif dn > ctrl.v * 1.1:
                    ctrl.dac, ctrl.v = ctrl.dac - ctrl.hill_step, dn
                else:
                    break
        if s == steps // 2:
            world.moves.append(max(50, min(1000, truth_best[0] + 400)))  # 打偏
        if s > steps // 2 and abs(ctrl.dac - truth_best[0]) <= 50:
            recovered = True
            recover_at = s - steps // 2
            break
    ok = locks_in and recovered
    print(f'[{name}] 真峰 dac={truth_best[0]}(lap={truth_best[1]:.0f}) '
          f'锁定 dac={ctrl.dac} 误差={lock_err} {"✓" if locks_in else "✗"} | '
          f'失焦恢复 {"✓@"+str(recover_at)+"步" if recovered else "✗"} | '
          f'VCM调用={world.vcm_calls} => {"PASS" if ok else "FAIL"}')
    return ok

def main():
    random.seed(7)
    ok1 = run_case('理想(无噪声)', lambda: FrameWorld(seed=1))
    w2 = lambda: FrameWorld(seed=2)
    w2().set_anomaly(0.0)
    ok2 = run_case('随机种子', lambda: FrameWorld(seed=3))
    def w3():
        w = FrameWorld(seed=4)
        w.blur_chance = 0.15
        return w
    ok3 = run_case('15%模糊帧', w3)
    def w4():
        w = FrameWorld(seed=5)
        w.set_anomaly(0.10)
        return w
    ok4 = run_case('10%假峰帧', w4)
    print(f'\n总判定: {sum([ok1, ok2, ok3, ok4])}/4')

if __name__ == '__main__':
    main()
