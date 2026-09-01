# -*- coding: utf-8 -*-
"""boot_role 漏斗语义单测（v2 V1）——mock 四个 probe/heal，断言：
健康路径 / heal 路径 / 重启路径 / heal+重启用尽进名单 / 重启后 tasker 作废（P0-2）/
探针 None 不触发 heal 冒进（三态）/ 台阶轨迹。

跑法：python tools/_test_boot_role.py（不碰设备、不加载 MaaFw 资源——run_5r 顶层
import maa 需已安装；mock 掉 _adb_ready/_game_activity_alive/_ready_main 等）。
"""
import os
import sys
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "agent"))

import run_5r as R

# ---- mock 掉一切设备依赖（不实例化 MaaFw 对象）----
R.LAUNCH_STAGGER = 0          # 测试不等待错峰
R.BOOT_STAGE_BUDGET = {"GAME_ALIVE": 0.5, "CONNECTED": 0.5, "LOGGED_IN": 0.5}

CALLS = {"heal_booted": 0, "heal_game": 0, "heal_connected": 0, "heal_logged_in": 0,
         "restart": 0}

class FakeTasker:
    def __init__(self):
        self.inited = True
        self.stopped = False
    def post_stop(self):
        self.stopped = True
        return self

def make_rb(probe_map, heal_results=None):
    """probe_map: {stage_name: callable}；heal_results: {heal_name: fn}"""
    rb = R.RoleBoot.__new__(R.RoleBoot)
    rb.role, rb.addr, rb.idx, rb.package = "测试", "127.0.0.1:16448", 2, "com.netease.my"
    rb.cancel = None
    rb.stage = R.BootStage.OFF
    rb.tasker = None
    rb.restarts_used = 0
    rb.degraded = False
    rb.adb = "adb"
    rb._probe_map = probe_map
    rb._heal_map = heal_results or {}
    return rb

def _patch_stage_spec(rb):
    def stage_spec(stage):
        m = {R.BootStage.BOOTED: ("BOOTED", rb._probe_map.get("BOOTED", lambda: True),
                                  lambda: _heal(rb, "heal_booted"), 0.5),
             R.BootStage.GAME_ALIVE: ("GAME_ALIVE", rb._probe_map.get("GAME_ALIVE", lambda: True),
                                      lambda: _heal(rb, "heal_game"), 0.5),
             R.BootStage.CONNECTED: ("CONNECTED", rb._probe_map.get("CONNECTED", lambda: True),
                                     lambda: _heal(rb, "heal_connected"), 0.5),
             R.BootStage.LOGGED_IN: ("LOGGED_IN", rb._probe_map.get("LOGGED_IN", lambda: True),
                                     lambda: _heal(rb, "heal_logged_in"), 0.5)}
        name, probe, heal, budget = m[stage]
        return probe, heal, budget, name, (stage == R.BootStage.LOGGED_IN)
    rb.stage_spec = stage_spec

def _heal(rb, name):
    CALLS[name] += 1
    fn = rb._heal_map.get(name)
    return fn() if fn else True

def restart_ok(rb):
    CALLS["restart"] += 1
    # P0-2 模拟：重启后 stage 回 BOOTED
    return True

# 只 mock 双检的冷却等待，加速测试
R._double_check_orig = R._double_check
def fast_double_check(check, cool_down=10, come_up=60, cancel=None):
    # 冷却压到 0：轮询命中即过（漏斗语义不变——冷却复查由 probe_map 自己模拟）
    if not R._wait_for(check, come_up, interval=0.05, cancel=cancel):
        return False
    return check()
R._double_check = fast_double_check
R.boot_role.__globals__["_double_check"] = fast_double_check

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))

# ---------- T1 健康路径：全部秒过 ----------
print("T1 健康路径")
CALLS.update({k: 0 for k in CALLS})
rb = make_rb({"BOOTED": lambda: True, "GAME_ALIVE": lambda: True,
              "CONNECTED": lambda: True, "LOGGED_IN": lambda: True})
_patch_stage_spec(rb)
ok = R.boot_role(rb, R.BootStage.LOGGED_IN)
check("健康路径达 LOGGED_IN", ok)
check("健康路径零 heal（前三级）", all(CALLS[k] == 0 for k in ("heal_booted", "heal_game", "heal_connected")))
check("LOGGED_IN act-first：登录必须无条件执行 1 次", CALLS["heal_logged_in"] == 1,
      f"got {CALLS['heal_logged_in']}（2026-08-30 假阳性根因：probe-first 永不触发登录）")
check("健康路径零重启", CALLS["restart"] == 0)

# ---------- T2 heal 路径：GAME_ALIVE 首轮不过、heal 后过 ----------
print("T2 heal 路径")
CALLS.update({k: 0 for k in CALLS})
def game_probe():           # 持续 False，直到 heal_game 被调用过才转 True（probe 依赖 heal 事件）
    return CALLS["heal_game"] >= 1
rb = make_rb({"GAME_ALIVE": game_probe}, {"heal_game": lambda: True})
_patch_stage_spec(rb)
ok = R.boot_role(rb, R.BootStage.GAME_ALIVE)
check("heal 后上台", ok and rb.stage == R.BootStage.GAME_ALIVE)
check("heal_game 计数 1", CALLS["heal_game"] == 1, f"got {CALLS['heal_game']}")

# ---------- T3 重启路径：GAME_ALIVE 永远不过 → heal×2 → 重启 → 从 GAME_ALIVE 重爬成功 ----------
print("T3 重启路径")
CALLS.update({k: 0 for k in CALLS})
fails_then_ok = {"ok": False}
rb = make_rb({"GAME_ALIVE": lambda: fails_then_ok["ok"]}, {"heal_game": lambda: True})
_patch_stage_spec(rb)
def fake_restart():
    CALLS["restart"] += 1
    fails_then_ok["ok"] = True     # 重启后 probe 过
    return True
rb.restart_instance = fake_restart
ok = R.boot_role(rb, R.BootStage.GAME_ALIVE)
check("重启后重爬成功", ok)
check("heal_game×2 后才重启", CALLS["heal_game"] == 2 and CALLS["restart"] == 1,
      f"heal={CALLS['heal_game']} restart={CALLS['restart']}")
check("重启后 stage 达 GAME_ALIVE", rb.stage == R.BootStage.GAME_ALIVE)

# ---------- T4 额度用尽：重启后仍不过 → 该台使命结束 ----------
print("T4 额度用尽")
CALLS.update({k: 0 for k in CALLS})
rb = make_rb({"GAME_ALIVE": lambda: False}, {"heal_game": lambda: True})
_patch_stage_spec(rb)
def t4_restart():
    CALLS["restart"] += 1
    rb.restarts_used += 1      # 复刻真实 restart_instance 的额度计数 + stage 回 BOOTED
    rb.stage = R.BootStage.BOOTED
    return True
rb.restart_instance = t4_restart
ok = R.boot_role(rb, R.BootStage.GAME_ALIVE)
check("重启额度用尽返回 False", ok is False)
check("重启只动用 1 次", CALLS["restart"] == 1)
check("heal 总计 2×2 轮（重启前后各一轮额度）", CALLS["heal_game"] == 4,
      f"got {CALLS['heal_game']}")
check("不 raise（名单由 boot_all 汇总）", True)

# ---------- T5 P0-2：重启作废 tasker ----------
print("T5 重启作废 tasker")
rb = R.RoleBoot.__new__(R.RoleBoot)
rb.role, rb.addr, rb.idx, rb.package = "测试", "127.0.0.1:16448", 2, "com.netease.my"
rb.cancel = None; rb.stage = R.BootStage.GAME_ALIVE; rb.restarts_used = 0; rb.degraded = False
rb.adb = "adb"
fake_t = FakeTasker()
rb.tasker = fake_t
R.be_restart_instance = lambda *a, **k: True
ok = rb.restart_instance()
check("重启成功", ok)
check("tasker 已作废（P0-2）", rb.tasker is None)
check("旧 tasker 已 post_stop", fake_t.stopped)
check("restarts_used 计 1", rb.restarts_used == 1)
check("stage 回 BOOTED", rb.stage == R.BootStage.BOOTED)

# ---------- T6 三态：probe None 不判死也不冒进 heal ----------
print("T6 探针 None 语义")
CALLS.update({k: 0 for k in CALLS})
none_then_true = {"n": 0}
def none_probe():
    none_then_true["n"] += 1
    return None if none_then_true["n"] <= 2 else True   # 前 2 次 None（adb 抖动），第 3 次过
rb = make_rb({"GAME_ALIVE": none_probe})
_patch_stage_spec(rb)
t0 = time.time()
ok = R.boot_role(rb, R.BootStage.GAME_ALIVE)
check("None 连发后自然恢复上台", ok)
check("恢复过程零 heal（None≠死，不该 monkey）", CALLS["heal_game"] == 0,
      f"got {CALLS['heal_game']}")
check("零重启", CALLS["restart"] == 0)

# ---------- T7 BOOTED heal 携带错峰（LAUNCH_STAGGER=0 时直过） ----------
print("T7 BOOTED 台阶")
CALLS.update({k: 0 for k in CALLS})
rb = make_rb({"BOOTED": lambda: CALLS["heal_booted"] >= 1},
             {"heal_booted": lambda: True})
_patch_stage_spec(rb)
ok = R.boot_role(rb, R.BootStage.BOOTED)
check("BOOTED heal 后上台", ok and rb.stage == R.BootStage.BOOTED)
check("heal_booted 计数 1", CALLS["heal_booted"] == 1)

print()
print(f"==== 通过 {len(PASS)} / 失败 {len(FAIL)} ====")
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
print("全部通过")
