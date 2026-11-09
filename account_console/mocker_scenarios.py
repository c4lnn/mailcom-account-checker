from __future__ import annotations

import hashlib
from typing import Any


PLAN_VERSION = 1
OPERATIONS = (
    "login", "oauth", "alias_list", "alias_remove", "password_change",
    "imap_login", "imap_select", "imap_store", "imap_expunge",
)
SCENARIOS = {
    "success": "正常登录并清理合成邮箱",
    "login_rejected": "邮件登录被拒绝",
    "oauth_temporary_failure": "OAuth 暂时失败后恢复",
    "imap_cleanup_failure": "IMAP 清理失败",
    "imap_recheck_failure": "IMAP 清理后的复查失败",
    "alias_read_failure": "别名读取失败",
    "alias_delete_failure": "别名删除失败",
    "password_uncertain": "修改密码提交后响应丢失",
    "password_rejected": "修改密码被拒绝",
    "worker_interrupted": "测试 worker 意外退出",
    "api_restart": "测试 API 重启与任务恢复",
}


def _fault_for(operation: str, seed: str, email: str, sequence: int,
               probability: float) -> str | None:
    digest = hashlib.sha256(
        f"{PLAN_VERSION}\0{seed}\0{email.lower()}\0{operation}\0{sequence}".encode()
    ).digest()
    sample = int.from_bytes(digest[:8], "big") / 2**64
    if sample >= probability:
        return None
    return "drop" if digest[8] & 1 else "503"


def build_plan(config: dict[str, Any], emails: list[str]) -> dict[str, Any]:
    mode = config.get("mode", "scenario")
    faults: list[dict[str, Any]] = []
    if mode == "scenario":
        scenario = config.get("scenario", "success")
        if scenario not in SCENARIOS:
            raise ValueError("mocker_scenario_invalid")
        targets = {
            "oauth_temporary_failure": ("oauth", 1, "503"),
            "imap_cleanup_failure": ("imap_store", 1, "503"),
            "imap_recheck_failure": ("imap_select", 2, "503"),
            "alias_read_failure": ("alias_list", 1, "503"),
            "alias_delete_failure": ("alias_remove", 1, "503"),
        }
        if scenario in targets:
            operation, sequence, fault = targets[scenario]
            faults = [{"email": email.lower(), "operation": operation,
                       "sequence": sequence, "fault": fault} for email in sorted(emails)]
        plan = {"version": PLAN_VERSION, "mode": mode, "scenario": scenario, "faults": faults}
        if scenario == "worker_interrupted":
            plan["lifecycle"] = "worker_interrupt"
        elif scenario == "api_restart":
            plan["lifecycle"] = "api_restart"
        return plan

    if mode != "random":
        raise ValueError("mocker_mode_invalid")
    seed = str(config.get("seed", ""))
    operations = config.get("operations", [])
    probability = config.get("probability", 0.0)
    if not seed or not isinstance(operations, list) or not operations:
        raise ValueError("mocker_random_config_invalid")
    if any(operation not in OPERATIONS for operation in operations):
        raise ValueError("mocker_operation_invalid")
    if not isinstance(probability, (int, float)) or not 0 <= probability <= 1:
        raise ValueError("mocker_probability_invalid")
    for email in sorted(set(value.lower() for value in emails)):
        for operation in sorted(set(operations)):
            for sequence in range(1, 13):
                fault = _fault_for(operation, seed, email, sequence, float(probability))
                if fault:
                    faults.append({"email": email, "operation": operation,
                                   "sequence": sequence, "fault": fault})
    return {"version": PLAN_VERSION, "mode": mode, "seed": seed,
            "operations": sorted(set(operations)), "probability": float(probability),
            "faults": faults}
