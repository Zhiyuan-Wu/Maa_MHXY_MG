#!/usr/bin/env python3
"""MaaTaskerDestroy vs post_stop 处理线程竞争的最小复现器。

事发（2026-09-06 job 20260906_225154，Mac MaaFw 5.12.2，exit=-6）：
    tasker.post_stop()      # fire-and-forget：Job 丢弃、不 wait   ← run_5r.py:2243
    self.tasker = None      # refcount→0 同步跑 Tasker.__del__     ← run_5r.py:2246
    → MaaTaskerDestroy 与 native stop 处理线程并发操作同一批状态机锁
    → libc++abi: terminating ... system_error: mutex lock failed: Invalid argument
    → SIGABRT(-6)，整进程带掉全部 4 台。

前提认知：这是 µs 级纯时序竞争，Python 侧无法强制交错——本脚本能做的是**逐帧复刻
调用序列并循环/并发加压**把窗口叠出来。命中 = 铁证 + 修法验证 harness；
N 轮不命中 ≠ 证伪（race 没踩中而已）。

复刻的四要素（缺一不可）：
  1. 有 pipeline 任务在跑（状态机 + controller 在途 I/O）——空转 tasker 上 stop 秒完成；
  2. fire-and-forget post_stop（Job 丢弃，不 wait）；
  3. 立即 del 三个对象（tasker→res→ctrl，CPython refcount 同步 __del__）；
  4. 远端 adb 截图在途（Mac 经 Tailscale 打 Win:5038，天然带——正好是事发形态）。

用法（Mac，先确保目标设备已在 adb devices 列表——跑过一轮 boot/ensure 即满足）：
    ADB_SERVER_SOCKET=tcp:100.77.236.94:5038 \
      .venv/bin/python agent/repro_tasker_destroy_race.py --addr 127.0.0.1:16704

加压：--loops 100 --workers 4（模拟 4 台并发 boot 的竞争环境）
对照组（命中后跑，闭环因果）：
    --mode wait   post_stop().wait() 后再 del —— 诊断"wait 是否关死窗口"
                  （run_5r 不能对失效 handle wait，此模式仅证明竞争对，不是修法）
    --mode keep   graveyard：三元组退役进全局 list 永不销毁 —— 等价于 run_5r 的
                  停尸房修法，构造性不崩；命中 race 后跑它不崩即验证修法有效。

判定命中：stderr 出现
    libc++abi: terminating due to uncaught exception ... mutex lock failed
    且 faulthandler 栈含 maa/tasker.py __del__ ← 本脚本 del t 那行。
"""
import argparse
import faulthandler
import gc
import json
import sys
import threading
import time
from pathlib import Path

from maa.controller import AdbController
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit

REPO = Path(__file__).resolve().parent.parent
DEBUG_DIR = REPO / "debug" / "repro_destroy_race"

# graveyard：--mode keep 的退役三元组（持有到进程退出，永不 destroy）
_GRAVEYARD = []
_lock = threading.Lock()   # 仅护 print 计数
_build_lock = threading.Lock()   # 复刻 run_5r 的 _MAAFW_CONNECT_LOCK：find/connect/bind
                                 # 串行（多线程并发 post_connection/post_bundle 本身有崩面，
                                 # 是混杂因素要排除；事发时 connect 串行、并发的是任务运行）


def build_mini_resource(root: Path) -> Path:
    """最小 resource：单节点自循环（默认 DirectHit 识别 + 角落 Click 保证 controller
    持续有输入 I/O、每拍截图）。Click 打在 (20,20) 角落——对游戏无副作用。"""
    pipe = root / "base" / "pipeline"
    pipe.mkdir(parents=True, exist_ok=True)
    (pipe / "loop.json").write_text(json.dumps({
        "loop": {
            "action": "Click",
            "target": [20, 20, 10, 10],
            "next": ["loop"],
            "timeout": 3600000,
        }
    }, ensure_ascii=False), encoding="utf-8")
    return root


def find_device(addr: str):
    devs = Toolkit.find_adb_devices()
    for d in devs:
        if d.address == addr:
            return d
    print(f"!! {addr} 不在 find_adb_devices 列表（共 {len(devs)} 台）。"
          f"先跑一轮 boot/ensure 注册设备，或检查 ADB_SERVER_SOCKET。")
    for d in devs:
        print(f"   可见: {d.address}  {d.name}")
    return None


def one_round(dev, res_dir: Path, mode: str, run_sec: float, rid: int) -> bool:
    """一轮复刻。返回是否走完（进程若崩在这轮里就直接死了，不会返回）。"""
    t0 = time.time()
    with _build_lock:
        ctrl = AdbController(
            adb_path=str(dev.adb_path), address=dev.address,
            screencap_methods=dev.screencap_methods, input_methods=dev.input_methods,
            config=dev.config,
        )
        if not ctrl.post_connection().wait():
            print(f"  #{rid} post_connection 失败，跳过"); return False
        res = Resource()
        if not res.post_bundle(str(res_dir)).wait():
            print(f"  #{rid} post_bundle 失败，跳过"); return False
        t = Tasker()
        if not (t.bind(res, ctrl) and t.inited):
            print(f"  #{rid} tasker 未就绪，跳过"); return False

    t.post_task("loop")            # Job 丢弃——复刻 run_5r 不持 job 引用
    time.sleep(run_sec)            # 让任务真跑起来（状态机+controller 在途 I/O）

    if mode == "wait":
        t.post_stop().wait()       # 对照组：等 stop 处理完再销毁（Job 随语句丢弃）
    else:
        t.post_stop()              # ←← run_5r.py:2243 的 fire-and-forget stop。
                                   #     绝不能接变量：Job 持 bound method→Tasker 引用，
                                   #     接了 del t 不掉 refcount、__del__ 不跑，复现失效

    if mode == "keep":
        _GRAVEYARD.append((t, res, ctrl))   # graveyard：永不销毁
    else:
        del t                      # ←← run_5r.py:2246：refcount→0 同步 __del__
        del res                    #     → MaaTaskerDestroy 竞争 stop 处理线程
        del ctrl
        gc.collect()
    with _lock:
        print(f"  #{rid} mode={mode} 完成（{time.time()-t0:.1f}s）")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", required=True, help="如 127.0.0.1:16704（离歌）")
    ap.add_argument("--loops", type=int, default=50)
    ap.add_argument("--workers", type=int, default=1, help="并发 worker 数（4=模拟 4 台 boot）")
    ap.add_argument("--mode", choices=["race", "wait", "keep"], default="race")
    ap.add_argument("--run-sec", type=float, default=3.0, help="stop 前让任务跑多久")
    args = ap.parse_args()

    faulthandler.enable(all_threads=True)   # SIGABRT 时 dump 全线程栈（同事发日志形态）
    stamp = time.strftime("%Y%m%d_%H%M%S")
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    Toolkit.init_option(str(DEBUG_DIR / stamp))   # 开原生日志，崩了有 native 侧可查
    Library.open()

    dev = find_device(args.addr)
    if dev is None:
        sys.exit(1)
    res_dir = build_mini_resource(DEBUG_DIR / f"mini_res_{stamp}")

    print(f"=== repro mode={args.mode} addr={args.addr} loops={args.loops} "
          f"workers={args.workers} run_sec={args.run_sec} ===")
    print(f"=== 命中 = libc++abi mutex lock failed + 栈含 tasker.py __del__；"
          f"跑完 {args.loops} 轮未死 = 未复现（不代表证伪） ===")

    def worker(wid: int, n: int):
        for i in range(n):
            one_round(dev, res_dir, args.mode, args.run_sec, wid * 1000 + i)
            time.sleep(1.0)

    per = max(1, args.loops // args.workers)
    t0 = time.time()
    ths = [threading.Thread(target=worker, args=(w, per)) for w in range(args.workers)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    print(f"=== 存活 {args.loops} 轮（{time.time()-t0:.0f}s）：未复现。"
          f"可加 --workers 4 / 增大 --loops / 加宿主负载再试 ===")


if __name__ == "__main__":
    main()
