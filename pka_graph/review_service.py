"""间隔复习服务（第五阶段）：设置、统一计划更新、到期列表、稍后提醒、暂停。

规则（试用默认，可在设置中修改，不宣称精确预测遗忘）：
- 默认间隔 7/21/45/90 天，"本次完成到下一次复习"的间隔；
- 新节点首次独立通过 → passed + 首档排期（仅当复习已启用并确认）；
- 到期复习独立通过 → 保持 passed，推进一档，90 天封顶；
- 独立复习失败 → failed（视野层），清除周期计划，进入"待重学"；
- failed 再通过 → passed，从首档重启；
- 提示/查看答案 → 辅助学习，状态与计划都不动；
- inconclusive / 取消 / 超时 / 模型错误 → 什么都不变；
- 未回应提醒：due 保留、不顺延、不自动判失败；
- snooze 只影响提醒，不改 due；
- 暂停/恢复明确分组。

时间：存储一律带时区 ISO（UTC）；"今天/逾期天数"按 Asia/Shanghai 日界计算；
"7 天后" = 当前时刻 + 7×24 小时（整日展示时按上海日界取日期）。
时钟可注入（测试不修改系统时间）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import ValidationError

SH_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_INTERVALS = [7, 21, 45, 90]
REVIEW_SECTION = "review"

REVIEW_FIELDS = ("interval_index", "review_paused", "snooze_until", "last_review_result")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sh_today(dt: datetime | None = None) -> str:
    """Asia/Shanghai 的今天，ISO 日期。"""
    d = dt or utcnow()
    return d.astimezone(SH_TZ).date().isoformat()


def add_days(base: datetime, days: int) -> datetime:
    """'N 天后'的统一实现：UTC 基点 + N×24h（存储带时区；展示按上海时区）。"""
    return base + timedelta(days=days)


def in_sh(dt: datetime) -> str:
    return dt.astimezone(SH_TZ).isoformat(timespec="minutes")


class ReviewService:
    def __init__(self, graph_service, clock=utcnow):
        self.svc = graph_service
        self.clock = clock  # 可注入时钟：返回带时区 datetime

    # ---------- 设置 ----------

    def _section(self, state: dict) -> dict:
        section = state.get(REVIEW_SECTION) or {}
        section.setdefault("enabled", False)
        section.setdefault("intervals", list(DEFAULT_INTERVALS))
        section.setdefault("confirmed_at", None)
        if not isinstance(section["intervals"], list) or not (1 <= len(section["intervals"]) <= 6):
            section["intervals"] = list(DEFAULT_INTERVALS)
        section["intervals"] = [int(x) for x in section["intervals"]]
        return section

    def settings(self) -> dict:
        return self._section(self.svc.state())

    def update_settings(self, *, enabled: bool | None = None, intervals: list | None = None,
                        confirm: bool = False, recalc: bool = False,
                        recalc_confirm: bool = False) -> dict:
        """修改设置。规则：
        - 首次启用必须 confirm=true（界面展示默认间隔后由用户确认）；
        - 修改间隔默认只影响后续排期；
        - recalc=true 会重算已有安排，必须先看 recalc-preview 再带 recalc_confirm=true。
        """
        state = self.svc.state()
        section = self._section(state)
        result = {"changed": [], "note": []}
        if enabled is not None:
            if enabled and not section["enabled"]:
                if not confirm:
                    raise ValidationError(
                        f"启用复习需要确认。当前默认间隔：{'/'.join(str(i) for i in section['intervals'])} 天"
                        "（本次完成到下一次复习的间隔，可在设置中修改）。请先查看再确认启用。"
                    )
                section["confirmed_at"] = self.clock().isoformat(timespec="seconds")
            section["enabled"] = bool(enabled)
            result["changed"].append("enabled")
        if intervals is not None:
            cleaned = []
            for x in intervals:
                v = int(x)
                if not 1 <= v <= 365:
                    raise ValidationError(f"间隔天数需在 1-365 之间：{v}")
                cleaned.append(v)
            if not cleaned:
                raise ValidationError("间隔列表不能为空")
            section["intervals"] = cleaned
            result["changed"].append("intervals")
            if recalc:
                if not recalc_confirm:
                    raise ValidationError(
                        "重算已有安排需要先查看差异预览（/api/review/recalc-preview）并确认"
                    )
                diff = self.recalc_preview(intervals=cleaned)
                for item in diff:
                    node = next(n for n in state["nodes"] if n["id"] == item["node_id"])
                    node["next_review_at"] = item["new_due"]
                result["note"].append(f"已按新间隔重算 {len(diff)} 个节点")
            else:
                result["note"].append("间隔修改只影响后续排期，已有到期日未变")
        state[REVIEW_SECTION] = section
        self.svc.store.save(state, base_version=state["version"])
        result["settings"] = section
        return result

    def recalc_preview(self, intervals: list[int] | None = None) -> list[dict]:
        """按新间隔重算已有安排的差异预览（不写入）。"""
        section = self._section(self.svc.state())
        new_intervals = [int(x) for x in (intervals or section["intervals"])]
        now = self.clock()
        diff = []
        for node in self.svc.state()["nodes"]:
            if node["archived"] or node["status"] != "passed":
                continue
            idx = node.get("interval_index")
            if idx is None or node.get("last_quiz_at") is None:
                continue
            idx = min(int(idx), len(new_intervals) - 1)
            try:
                base = datetime.fromisoformat(str(node["last_quiz_at"]))
            except ValueError:
                continue
            new_due = add_days(base, new_intervals[idx])
            old_due = node.get("next_review_at")
            if old_due and str(old_due)[:19] == new_due.isoformat(timespec="seconds")[:19]:
                continue
            diff.append({
                "node_id": node["id"],
                "name": node["name"],
                "old_due": old_due,
                "new_due": new_due.isoformat(timespec="seconds"),
            })
        _ = now
        return diff

    # ---------- 计划更新（由测验受控提交调用；界面不直接写权威日期） ----------

    def on_quiz_submitted(self, state: dict, node: dict, *, kind: str, verdict: str) -> None:
        """测验受控提交成功后统一维护计划。state 为待写回的最新状态（原地修改）。
        - assisted_learning / inconclusive 不会进入这里（调用方保证）。
        - failed（任何 kind）→ 清计划、进待重学；
        - mastery pass → enabled 时首档排期；
        - review pass → 推进一档（90 天档封顶）。
        """
        section = self._section(state)
        now = self.clock()
        if verdict == "failed":
            node["status"] = "failed"
            node["last_quiz_at"] = now.isoformat(timespec="seconds")
            node["last_review_result"] = "failed"
            for field in REVIEW_FIELDS:
                if field != "last_review_result":
                    node[field] = None if field != "review_paused" else False
            node["next_review_at"] = None
            node["interval_index"] = None
            node["snooze_until"] = None
            node["review_paused"] = False
            return
        # verdict == passed
        node["status"] = "passed"
        node["last_quiz_at"] = now.isoformat(timespec="seconds")
        node["last_review_result"] = "passed"
        node["snooze_until"] = None
        if not section["enabled"]:
            # 未启用复习：只标记通过，不排期（显示"尚未安排"）
            node["next_review_at"] = None
            node["interval_index"] = None
            return
        if kind == "review":
            old_idx = node.get("interval_index")
            idx = 0 if old_idx is None else min(int(old_idx) + 1, len(section["intervals"]) - 1)
        else:
            idx = 0  # 首次/重新掌握：首档
        node["interval_index"] = idx
        node["next_review_at"] = add_days(now, section["intervals"][idx]).isoformat(timespec="seconds")

    # ---------- 待复习列表 ----------

    def due_groups(self) -> dict:
        state = self.svc.state()
        section = self._section(state)
        now = self.clock()
        today = sh_today(now)
        groups = {"overdue": [], "today": [], "upcoming": [], "unarranged": [], "paused": [], "relearn": []}
        for node in state["nodes"]:
            if node["archived"]:
                continue  # 归档节点不进入待复习列表，也不提醒
            if node["status"] == "failed" and node.get("last_review_result") == "failed":
                groups["relearn"].append(self._item(node, now))
                continue
            if node["status"] != "passed":
                continue
            due = node.get("next_review_at")
            if node.get("review_paused"):
                groups["paused"].append(self._item(node, now))
                continue
            if not due:
                groups["unarranged"].append(self._item(node, now))
                continue
            item = self._item(node, now)
            due_date = sh_today(datetime.fromisoformat(str(due)))
            if due_date < today:
                item["overdue_days"] = (datetime.fromisoformat(today) - datetime.fromisoformat(due_date)).days
                groups["overdue"].append(item)
            elif due_date == today:
                groups["today"].append(item)
            else:
                groups["upcoming"].append(item)
        for key in groups:
            groups[key].sort(key=lambda i: (i.get("due") or "9999", i["name"]))
        return {"settings": section, "today": today, "groups": groups,
                "counts": {k: len(v) for k, v in groups.items()},
                "notice": "应用关闭或服务未运行时不会主动通知；再次打开后可看到积压事项。已读提醒不等于完成复习。"}

    def _item(self, node: dict, now: datetime) -> dict:
        snooze = node.get("snooze_until")
        snoozed = bool(snooze) and datetime.fromisoformat(str(snooze)) > now
        return {
            "node_id": node["id"],
            "name": node["name"],
            "domain": node["domain"],
            "status": node["status"],
            "due": node.get("next_review_at"),
            "due_sh": in_sh(datetime.fromisoformat(str(node["next_review_at"]))) if node.get("next_review_at") else None,
            "overdue_days": None,
            "interval_index": node.get("interval_index"),
            "review_paused": bool(node.get("review_paused")),
            "snooze_until": snooze,
            "snoozed": snoozed,
            "last_review_result": node.get("last_review_result"),
            "status_origin": node.get("status_origin"),
        }

    # ---------- 稍后提醒 / 暂停 ----------

    def snooze(self, node_id: str, hours: float = 8.0) -> dict:
        """稍后提醒：只影响下一次提醒时间，绝不修改真实到期日。"""
        if not 0.1 <= hours <= 24 * 30:
            raise ValidationError("稍后提醒时长需在 0.1 小时 ~ 720 小时之间")
        state = self.svc.state()
        node = self._node(state, node_id)
        node["snooze_until"] = (self.clock() + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.svc.store.save(state, base_version=state["version"])
        return {"node_id": node_id, "snooze_until": node["snooze_until"], "due_unchanged": node.get("next_review_at")}

    def set_paused(self, node_id: str, paused: bool) -> dict:
        state = self.svc.state()
        node = self._node(state, node_id)
        node["review_paused"] = bool(paused)
        self.svc.store.save(state, base_version=state["version"])
        return {"node_id": node_id, "review_paused": node["review_paused"], "due_unchanged": node.get("next_review_at")}

    def _node(self, state: dict, node_id: str) -> dict:
        node = next((n for n in state["nodes"] if n["id"] == node_id), None)
        if node is None:
            raise ValidationError(f"节点不存在：{node_id}")
        return node

    # ---------- 旧 passed 节点批量启用排期 ----------

    def backfill_preview(self) -> dict:
        state = self.svc.state()
        section = self._section(state)
        if not section["enabled"]:
            raise ValidationError("复习尚未启用：先确认启用（含默认间隔展示），再预览补排期")
        now = self.clock()
        plan = []
        for node in state["nodes"]:
            if node["archived"] or node["status"] != "passed":
                continue
            if node.get("next_review_at"):
                continue
            plan.append({
                "node_id": node["id"], "name": node["name"],
                "last_quiz_at": node.get("last_quiz_at"),
                "proposed_due": add_days(now, section["intervals"][0]).isoformat(timespec="seconds"),
                "note": "以当前时间为基点排首档（不补造历史测验时间）",
            })
        return {"plan": plan, "count": len(plan)}

    def backfill_apply(self, confirm: bool = False) -> dict:
        if not confirm:
            raise ValidationError("批量补排期需要先查看预览并确认（confirm=true）")
        preview = self.backfill_preview()
        state = self.svc.state()
        by_id = {p["node_id"]: p for p in preview["plan"]}
        changed = 0
        for node in state["nodes"]:
            if node["id"] in by_id:
                node["next_review_at"] = by_id[node["id"]]["proposed_due"]
                node["interval_index"] = 0
                changed += 1
        self.svc.store.save(state, base_version=state["version"])
        return {"scheduled": changed}

    # ---------- 归档联动 ----------

    def on_node_archived(self, node: dict) -> None:
        """归档节点：清除提醒（due/snooze 置空、暂停标记），计划信息留在 archive_note。"""
        node["next_review_at"] = None
        node["snooze_until"] = None
        node["review_paused"] = False
