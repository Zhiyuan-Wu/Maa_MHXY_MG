from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context
from utils import logger

import json
import os
import re
import threading
from datetime import datetime
from os.path import dirname, join, normpath

# 剥离千分位分隔符等非数字字符：游戏金币/银币显示成 "1,124,229"，但 OCR 常把分隔符读成
# 句点/空格等（实测读到 "1.124.229"）。账号ID/金币/银币都是纯数值，统一只保留 [0-9]。
_DIGIT_RE = re.compile(r"\D")


@AgentServer.custom_action("logOcr")
class LogOcr(CustomAction):
    """OCR 指定区域并把结果写入本地日志（agent/data/account_info.log）。

    分两次调用配合出"账号ID | 金币 | 银币"合并行：
        第一次 mode="id"    : OCR 账号ID，暂存（按 tasker 隔离），不写文件。
        第二次 mode="coins" : 一帧截图 OCR 金币+银币，取出暂存的账号ID，写合并行。

    custom_action_param:
        id 步:    {"mode":"id",    "roi":[334,119,125,41]}
        coins 步: {"mode":"coins", "gold_roi":[189,626,157,51], "silver_roi":[420,628,159,47]}

    所有 OCR 结果只保留数字（``_DIGIT_RE`` 去千分位分隔符，游戏显示 "1,124,229" 但 OCR 常读成
    "1.124.229"），日志里数值无任何分隔符。

    5 开并发下，每号独立 Tasker，id(context.tasker) 作为"每账号 key"保证 name↔金币↔银币
    不串号；文件追加由 _LOCK 保护。
    """

    _LOG_PATH = normpath(join(dirname(os.path.abspath(__file__)), "..", "..", "data", "account_info.log"))
    _PENDING = {}  # id(tasker) -> 账号ID
    _LOCK = threading.Lock()

    def _ocr(self, context: Context, image, roi):
        reco = context.run_recognition(
            "_logOcr_probe",
            image,
            pipeline_override={
                "_logOcr_probe": {"roi": roi, "expected": [""], "recognition": "OCR"}
            },
        )
        if reco and reco.hit and reco.best_result:
            digits = _DIGIT_RE.sub("", reco.best_result.text)   # 只留数字，去掉千分位分隔符
            if digits:
                return digits
        return "(未识别)"

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> CustomAction.RunResult:
        argv_dict: dict = json.loads(argv.custom_action_param or "{}")
        if not argv_dict:
            logger.warning("[logOcr] custom_action_param 为空，跳过")
            return CustomAction.RunResult(success=True)

        mode = argv_dict.get("mode", "")
        image = context.tasker.controller.post_screencap().wait().get()

        if mode == "id":
            roi = argv_dict.get("roi")
            if not roi:
                logger.warning("[logOcr] id 步缺少 roi")
                return CustomAction.RunResult(success=True)
            account_id = self._ocr(context, image, roi)
            self._PENDING[id(context.tasker)] = account_id
            logger.info(f"[logOcr] 暂存账号ID: {account_id}")
            return CustomAction.RunResult(success=True)

        if mode == "coins":
            gold_roi = argv_dict.get("gold_roi")
            silver_roi = argv_dict.get("silver_roi")
            if not (gold_roi and silver_roi):
                logger.warning("[logOcr] coins 步缺少 gold_roi/silver_roi")
                return CustomAction.RunResult(success=True)
            account_id = self._PENDING.pop(id(context.tasker), "(未知账号)")
            gold = self._ocr(context, image, gold_roi)
            silver = self._ocr(context, image, silver_roi)
            line = (
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
                f"账号ID: {account_id} | 金币: {gold} | 银币: {silver}"
            )
            with self._LOCK:
                os.makedirs(dirname(self._LOG_PATH), exist_ok=True)
                with open(self._LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            logger.info(f"[logOcr] 已写入: {line}")
            return CustomAction.RunResult(success=True)

        logger.warning(f"[logOcr] 未知 mode: {mode}")
        return CustomAction.RunResult(success=True)
