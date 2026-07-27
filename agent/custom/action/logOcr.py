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
# 句点/空格等（实测读到 "1.124.229"）。金币/银币都是纯数值，统一只保留 [0-9]。
_DIGIT_RE = re.compile(r"\D")


@AgentServer.custom_action("logOcr")
class LogOcr(CustomAction):
    """OCR 金币/银币并把结果写入本地日志（agent/data/account_info.log）。

    账号身份用控制器的 ``adb_serial``（如 ``127.0.0.1:16448``）标识——它是 MaaFw 通过
    C handle 查回来的构造参数，与 Python wrapper 对象身份无关，5 开并发下跨节点稳定。

    custom_action_param:
        {"gold_roi": [189, 626, 157, 51], "silver_roi": [420, 628, 159, 47]}

    OCR 结果只保留数字（``_DIGIT_RE`` 去千分位分隔符）。单次 ROI 失败写 "(未识别)"。
    文件追加由 _LOCK 保护（5 开并发串行写）。
    """

    _LOG_PATH = normpath(join(dirname(os.path.abspath(__file__)), "..", "..", "data", "account_info.log"))
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

    @staticmethod
    def _account_tag(context: Context) -> str:
        """稳定的账号标识：优先 adb_serial（如 127.0.0.1:16448），回退 uuid，再回退 ?。
        """
        try:
            ctrl = context.tasker.controller
            serial = (ctrl.info or {}).get("adb_serial")
            if serial:
                return serial
            uuid = ctrl.uuid
            return uuid or "?"
        except Exception as e:
            logger.warning(f"[logOcr] 取账号标识失败: {type(e).__name__}: {e}")
            return "?"

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> CustomAction.RunResult:
        argv_dict: dict = json.loads(argv.custom_action_param or "{}")
        gold_roi = argv_dict.get("gold_roi")
        silver_roi = argv_dict.get("silver_roi")
        if not (gold_roi and silver_roi):
            logger.warning("[logOcr] 缺少 gold_roi/silver_roi，跳过")
            return CustomAction.RunResult(success=True)

        image = context.tasker.controller.post_screencap().wait().get()
        tag = self._account_tag(context)
        gold = self._ocr(context, image, gold_roi)
        silver = self._ocr(context, image, silver_roi)
        line = (
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
            f"账号: {tag} | 金币: {gold} | 银币: {silver}"
        )
        with self._LOCK:
            os.makedirs(dirname(self._LOG_PATH), exist_ok=True)
            with open(self._LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        logger.info(f"[logOcr] 已写入: {line}")
        return CustomAction.RunResult(success=True)
