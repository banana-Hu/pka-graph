# -*- coding: utf-8 -*-
"""发现模块补充批次三：接受意图日志与恢复、周期检查线程、语义分析挂接。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / "pka_graph" / "discovery.py"
t = p.read_text(encoding="utf-8")

# 1) accept：写入意图日志（note 写盘前登记，state 提交后清除）
a = """        if verdict == "new_node":
            name = (edits.get("name") or sug["name"]).strip()[:80]
            scope = (edits.get("scope") or self._default_scope(sug)).strip()
            domain = (edits.get("domain") or "待分类领域").strip()
            node = None"""
b = """        if verdict == "new_node":
            name = (edits.get("name") or sug["name"]).strip()[:80]
            scope = (edits.get("scope") or self._default_scope(sug)).strip()
            domain = (edits.get("domain") or "待分类领域").strip()
            intent_id = f"intent-{sug['id']}"
            node = None"""
assert a in t, "accept intent id anchor"
t = t.replace(a, b, 1)

a = """            note_result = self._write_candidate_note(sug, node, edits, note_name)
            # 两段提交：笔记已落盘 → 写 state；state 失败则回滚笔记文件
            try:"""
b = """            note_result = self._write_candidate_note(sug, node, edits, note_name)
            # 意图日志：笔记落盘后、state 提交前登记——进程崩溃后据此恢复
            intents = self._load_intents()
            intents[:] = [i for i in intents if i["intent_id"] != intent_id]
            intents.append({
                "intent_id": intent_id, "suggestion_id": sug["id"],
                "node": dict(node), "note_name": note_name,
                "note_id": note_result["note_id"],
                "note_fingerprint": hashlib.sha256(
                    note_result["path_obj"].read_bytes()).hexdigest()[:16],
                "stage": "note_written", "at": now_iso,
            })
            self._save_intents(intents)
            # 两段提交：笔记已落盘 → 写 state；state 失败则回滚笔记文件
            try:"""
assert a in t, "intent record anchor"
t = t.replace(a, b, 1)

a = """            except Exception:
                note_result["path_obj"].unlink(missing_ok=True)  # 回滚：不留孤儿
                raise
            sug["status"] = "accepted"
            sug["accepted"] = {"node_id": node["id"], "note": note_name,
                               "at": now_iso, "request_id": request_id,
                               "suggestion_note": note_name}"""
b = """            except Exception:
                note_result["path_obj"].unlink(missing_ok=True)  # 回滚：不留孤儿
                intents = [i for i in self._load_intents() if i["intent_id"] != intent_id]
                self._save_intents(intents)
                raise
            # state 提交成功：清除意图日志
            intents = [i for i in self._load_intents() if i["intent_id"] != intent_id]
            self._save_intents(intents)
            sug["status"] = "accepted"
            sug["accepted"] = {"node_id": node["id"], "note": note_name,
                               "at": now_iso, "request_id": request_id,
                               "suggestion_note": note_name}"""
assert a in t, "intent clear anchor"
t = t.replace(a, b, 1)

# 2) 恢复方法（挂在 accept 之前）
a = """    # ---------- 接受 / 稍后 / 忽略（任务 6） ----------"""
b = """    # ---------- 崩溃恢复（最小事务：意图日志） ----------

    def recover_intents(self) -> dict:
        \"\"\"重启后恢复未完成的两段提交。动作可解释、可回退：
        - state 已含节点与关联 → 操作实际已完成，仅清除意图；
        - 笔记完好（指纹一致）→ 补完 state 提交（不重建节点、不重复建笔记）；
        - 笔记被人工修改（指纹不一致）→ 不删除、不覆盖，标记 needs_manual；
        - 笔记丢失 → 用意图中保存的节点信息重建笔记后补完提交。
        \"\"\"
        results = []
        state = self.svc.state()
        changed_state = False
        for intent in self._load_intents():
            note_path = self.candidate_dir / intent["note_name"]
            node_id = intent["node"]["id"]
            node_in_state = next((n for n in state["nodes"] if n["id"] == node_id), None)
            linked = any(l["node_id"] == node_id and
                         l["ref"].get("note_id") == intent["note_id"]
                         for l in state["note_links"])
            if node_in_state and linked:
                results.append({"intent_id": intent["intent_id"],
                                "action": "already_committed", "node_id": node_id})
                changed_state = True  # 稍后统一保存清除
                continue
            current_fp = (hashlib.sha256(note_path.read_bytes()).hexdigest()[:16]
                          if note_path.exists() else None)
            if note_path.exists() and current_fp != intent["note_fingerprint"]:
                results.append({"intent_id": intent["intent_id"], "action": "needs_manual",
                                "reason": "候选笔记已被人工修改（指纹不一致）；"
                                          "未删除、未覆盖，请人工决定采纳或删除",
                                "note": intent["note_name"]})
                continue
            if not note_path.exists():
                self._write_candidate_note(
                    {"id": intent["suggestion_id"], "contribution": {},
                     "sources": [{"url": "", "label": "恢复重建"}],
                     "verdict": "new_node", "reason": "", "novelty_label": ""},
                    intent["node"], {}, intent["note_name"])
            if node_in_state is None:
                from .models import new_node as _new_node

                restored = _new_node(name=intent["node"]["name"],
                                     domain=intent["node"]["domain"],
                                     scope=intent["node"]["scope"])
                restored["id"] = intent["node"]["id"]  # 恢复原 ID，保证不重复生成
                state["nodes"].append(restored)
            if not linked:
                from .models import make_note_link as _make_link

                state["note_links"].append(
                    _make_link(node_id, {"kind": "note_id",
                                         "note_id": intent["note_id"],
                                         "path": f"{CANDIDATE_NOTE_DIR}/{intent['note_name']}"}))
            try:
                self.svc.store.save(state, base_version=state["version"])
            except Exception:
                results.append({"intent_id": intent["intent_id"], "action": "conflict",
                                "reason": "恢复提交遇到并发修改，请重试恢复"})
                continue
            results.append({"intent_id": intent["intent_id"], "action": "completed",
                            "node_id": node_id, "note": intent["note_name"]})
            changed_state = True
        if changed_state:
            remaining = [i for i in self._load_intents()
                         if i["intent_id"] not in
                         {r["intent_id"] for r in results if r["action"] != "needs_manual"}]
            remaining = [i for i in remaining
                         if i["intent_id"] not in
                         {r["intent_id"] for r in results if r["action"] == "needs_manual"}]
            self._save_intents(remaining)
        return {"recovered": results}

    # ---------- 接受 / 稍后 / 忽略（任务 6） ----------"""
assert a in t, "recover anchor"
t = t.replace(a, b, 1)

# 3) 周期检查线程（服务进程内；锁防重叠；多标签页共用同一进程不会重复）
a = """    # ---------- 手动 / 周期检查（任务 7） ----------

    def check(self, request_id: str | None = None) -> dict:"""
b = """    # ---------- 手动 / 周期检查（任务 7） ----------

    def start_periodic_if_configured(self) -> dict:
        \"\"\"在**服务进程内**启动周期检查线程（不是浏览器页面；不是新的守护进程）。
        auto_check_minutes=0 表示关闭。多标签页共享同一进程实例，不会叠加。\"\"\"
        state = self.svc.state()
        section = self._discovery_state(state)
        minutes = int(section.get("auto_check_minutes") or 0)
        if minutes <= 0 or self._periodic_thread is not None:
            return {"periodic": "off" if minutes <= 0 else "already_running", "minutes": minutes}
        interval = max(10, minutes) * 60

        def loop():
            while True:
                time.sleep(interval)
                if not self.check_lock.acquire(blocking=False):
                    continue  # 上一轮未完成，跳过本轮（不叠加）
                try:
                    self.check(request_id=None)
                except Exception:
                    pass  # 失败项留在 pending，不丢弃
                finally:
                    self.check_lock.release()

        self._periodic_thread = threading.Thread(target=loop, daemon=True, name="discovery-periodic")
        self._periodic_thread.start()
        return {"periodic": "started", "minutes": minutes}

    def check(self, request_id: str | None = None) -> dict:"""
assert a in t, "periodic anchor"
t = t.replace(a, b, 1)

# 4) check() 加锁防重叠
a = """    def check(self, request_id: str | None = None) -> dict:
        \"\"\"执行一次增量检查：读取上游 → 过滤已见 → 分级提取比较 → 生成建议。
        确定性实现，无模型调用；达到每批上限的条目留在 pending，不丢弃。\"\"\"
        state = self.svc.state()"""
b = """    def check(self, request_id: str | None = None) -> dict:
        \"\"\"执行一次增量检查：读取上游 → 过滤已见 → 分级提取比较 → 生成建议。
        确定性实现，无模型调用；达到每批上限的条目留在 pending，不丢弃。\"\"\"
        if not self.check_lock.acquire(blocking=False):
            return {"skipped": True, "reason": "上一轮检查尚未完成，本轮跳过（不叠加）"}
        try:
            return self._check_inner(request_id)
        finally:
            self.check_lock.release()

    def _check_inner(self, request_id: str | None = None) -> dict:
        state = self.svc.state()"""
assert a in t, "check lock anchor"
t = t.replace(a, b, 1)

p.write_text(t, encoding="utf-8")
print("batch 3 applied")
