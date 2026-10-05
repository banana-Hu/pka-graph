"""知识发现与知识库自增（第六阶段）：只读接入 → 材料分级 → 确定性提取 →
与现有知识比较 → 建议队列 → 接受后的安全入库。

边界与诚实声明：
- 本模块的提取与比较是**确定性规则**（可解释、可回归），不是语义理解；
  提取质量标注为 deterministic_shallow，真实模型的增量判断在配好适配器后
  才可能验证——当前一律不声称"真实 AI 已验证"。
- 新闻只是寻找原始材料的线索：融资/热度/发布事件本身不构成知识节点。
- 三类新颖性分开标注：来源声称的新贡献 / 系统本次首次发现 / 相对知识库未覆盖。
  "本次没搜到旧节点"只等于"相对知识库未覆盖"，绝不写成"用户肯定不知道"。
- 写入只针对测试知识库管理区：接受 new_node 走"候选笔记文件 + state.json"
  两段提交（先笔记后状态，失败回滚删笔记，孤儿可扫描可恢复）；
  supplement/revision 只追加提案记录，不自动改范围、正文或掌握状态。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import ValidationError
from .file_lock import FileMutex
from .review_service import utcnow
from .service import NotFound, make_note_link

SUGGESTIONS_FILE = "个人学习状态/discovery_suggestions.json"
DISCOVERY_SECTION = "discovery"
CANDIDATE_NOTE_DIR = "候选笔记"
FORMAL_NOTE_DIR = "正式笔记"
_COMMIT_LOCKS = threading.local()


def _candidate_filename(title: str) -> str:
    """把加工产出的中文标题清洗成安全的笔记文件名主干。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:60]


def _fm_escape(value: str) -> str:
    """front-matter 双引号标量转义：去引号反斜杠与换行，避免破坏受限解析。"""
    return re.sub(r'["\\\n\r]', " ", str(value or "")).strip()


ACTION_MAP = {"new_node": "新增知识（自动入库）", "supplement": "补充已有节点",
              "revision": "保存更新提案", "duplicate": "不处理（重复）",
              "news_only": "不处理（仅新闻线索）", "insufficient": "待核查",
              "uncertain": "待核查（分析不确定）"}

NEWS_TITLE_SIGNALS = re.compile(
    r"融资|种子轮|[AB][\+]?轮|上市|收购|获得.*投资|估值|裁员|launch|funding|raises?\b|acquir|valuation", re.I)
NOVELTY_CLAIM_SIGNALS = re.compile(
    r"首次|提出|新方法|新机制|全新|开创|新研究|研究表明|实验表明|基准.*提升|优于现有|"
    r"\bnovel\b|\bfirst\b|introduc\w*|propos\w*|outperform", re.I)
MECHANISM_SIGNALS = re.compile(
    r"机制|方法|算法|架构|协议|原理|框架|基准|实验|论文|证明|数据结构|损失函数|"
    r"\bmethod\b|\bmechanism\b|\balgorithm\b|\barchitecture\b|\bbenchmark\b|\bpropos\w*", re.I)
CONFLICT_SIGNALS = re.compile(r"推翻|挑战了|修正|与.*相.*矛盾|相反|contradict|conflict|overturn|revisit", re.I)
VERSION_SIGNALS = re.compile(r"\bv?\d+\.\d+(\.\d+)?\b|新版本|版本更新|breaking change|major release", re.I)
CONDITION_SIGNALS = re.compile(
    r"仅|只能|适用于|限制|局限|前提|假设|在某些|cannot|limitation|only (?:works|applies)", re.I)
DIFFERENCE_SIGNALS = re.compile(r"相比|优于|区别|不同于|与.*不同|compared|than (?:previous|existing)", re.I)
PROBLEM_SIGNALS = re.compile(r"解决|针对|旨在|问题|痛点|challenge|aim|solve|address", re.I)


# ---------------- 文本工具 ----------------

def tokens(text: str) -> set:
    """ASCII 词 + 中文二元组；用于确定性的重叠比较（无向量库）。"""
    words = set(w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text or ""))
    cjk = re.findall(r"[\u4e00-\u9fff]", text or "")
    bigrams = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    return words | bigrams


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: set, b: set) -> float:
    """包含度：|交集| / min(|a|,|b|)。适合短候选 vs 长节点描述的比较，
    中文二元组下比 Jaccard 稳定（实测阈值：≥0.75 重复、0.25-0.55 补充/修订）。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？!?;\n]+", text or "")
    return [p.strip() for p in parts if len(p.strip()) >= 6]


def _find(sentences: list[str], pattern: re.Pattern, limit: int = 4) -> list[str]:
    hits = [s for s in sentences if pattern.search(s)]
    return hits[:limit]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------- 材料分级（任务 2） ----------------

def effective_len(text: str) -> int:
    """有效长度：CJK 字符按 2 计（信息密度约为拉丁字符的两倍），
    避免中文摘要被按拉丁字符阈值误判为过短。"""
    cjk = len(re.findall(r"[一-鿿]", text or ""))
    return cjk * 2 + max(0, len(text or "") - cjk)


def classify_material(summary: str) -> dict:
    text = (summary or "").strip()
    if not text:
        level = "clue_only"
        note = "仅标题线索：无摘要正文，无法判断知识增量"
    elif effective_len(text) < 100:
        level = "clue_only"
        note = "摘要过短：不足以提取可核查的知识贡献"
    else:
        level = "readable_summary"
        note = "上游摘要可读（正文未抓取；远程抓取默认关闭）"
    return {"level": level, "note": note, "text": text}


# ---------------- 确定性贡献提取（任务 3） ----------------

def extract_contribution(material_text: str, title: str) -> dict:
    """从可读摘要中提取结构化贡献声明。全部字段都是材料原文的子串或显式 None，
    不编造原理、实验或来源。质量标注 deterministic_shallow。"""
    sentences = _sentences(material_text)
    claims = _find(sentences, MECHANISM_SIGNALS, 5)
    problem_hits = _find(sentences, PROBLEM_SIGNALS, 1)
    extraction = {
        "problem": _clip(problem_hits[0], 200) if problem_hits else None,
        "claims": [_clip(c, 220) for c in claims],
        "differences": [_clip(d, 200) for d in _find(sentences, DIFFERENCE_SIGNALS, 2)],
        "conditions_limits": [_clip(c, 160) for c in _find(sentences, CONDITION_SIGNALS, 3)],
        "evidence_locators": [],  # 确定性提取无法定位页码；上游链接在 sources 中
        "maturity": "来源声称",
        "extraction_quality": "deterministic_shallow",
    }
    if re.search(r"实验表明|基准|benchmark|数据表明|实测", material_text or "", re.I):
        extraction["maturity"] = "来源声称（材料含实验/基准数据表述，未经独立验证）"
    extraction["has_mechanism"] = bool(claims)
    extraction["sufficient"] = effective_len(material_text or "") >= 100 and bool(claims)
    news_title = bool(NEWS_TITLE_SIGNALS.search(title or ""))
    extraction["news_title_signal"] = news_title
    extraction["contribution_summary"] = (
        _clip(claims[0], 180) if claims else None
    )
    return extraction


# ---------------- 确定性增量判定（任务 4） ----------------

def compare_with_vault(extraction: dict, title: str, material_text: str,
                       related: list[dict]) -> tuple[str, str, list[dict]]:
    """返回 (verdict, reason, related_used)。related 已按包含度排序。
    结构：可读性只区分"线索 vs 材料"；可读材料先看相关性动作（低风险的
    supplement），再看机制关键词（new_node 的门槛），最后才允许 news_only。
    阈值实测校准：包含度 ≥0.75 重复；0.30-0.55 相关（补充）；0.25-0.55 且
    含冲突/版本信号 → 修订。"""
    from .discovery import effective_len

    readable = effective_len(material_text or "") >= 100
    best_score = related[0]["score"] if related else 0.0
    cand_token_count = len(tokens(title or "") | tokens(material_text or ""))
    # 阈值只是预筛工具：短文本的包含度不可靠（短标题易被长文本包含），
    # token 过少时不用阈值下结论，转待核查。
    thresholds_reliable = cand_token_count >= 12

    if not readable:
        # 仅标题/过短线索：产品/事件公告 → 新闻线索；其余（可能含未知知识增量）→ 待核查
        title_text = title or ""
        if NEWS_TITLE_SIGNALS.search(title_text):
            return ("news_only", "标题为事件/商业信号且无摘要正文：仅新闻线索，无知识增量", related)
        product_announce = bool(re.search(
            r"上线|推出|下载|体验|APP|正式上线", title_text, re.I)) and not bool(re.search(
            r"模型|论文|研究|基准|算法|架构|协议|机制|框架|方法|API", title_text, re.I))
        if product_announce:
            return ("news_only", "仅为产品/事件公告线索，无知识增量预期", related)
        return ("insufficient", "材料为线索或摘要过短：可能存在未知知识增量但无法判断，进入待核查", related)

    # 可读材料：先处理确定性动作
    if best_score >= 0.75 and thresholds_reliable:
        return ("duplicate",
                f"规则预筛：与已有节点「{related[0]['name']}」包含度 {best_score:.2f}，疑似重复"
                "（待语义分析与人工确认）",
                related)
    if CONFLICT_SIGNALS.search(material_text or "") and best_score >= 0.15 and thresholds_reliable:
        return ("revision",
                f"规则预筛：材料含与「{related[0]['name']}」相关的冲突/修正表述，疑似影响旧结论；"
                "已生成更新提案（待人工确认，不自动覆盖）",
                related)
    if VERSION_SIGNALS.search((title or "") + " " + (material_text or "")) and best_score >= 0.25 \
            and thresholds_reliable:
        return ("revision",
                f"规则预筛：涉及版本变化且与「{related[0]['name']}」相关，疑似影响旧方法"
                "（生成更新提案，待人工确认）",
                related)
    if best_score >= 0.30:
        return ("supplement",
                f"规则预筛：与「{related[0]['name']}」相关，疑似补充解释/例证"
                "（待语义分析与人工确认）",
                related)
    # 与既有知识无关联：只有提取到机制/方法类贡献才值得新建节点
    if not extraction["has_mechanism"]:
        if NEWS_TITLE_SIGNALS.search(title or ""):
            return ("news_only",
                    "规则预筛：事件/商业信号为主且未提取到机制类贡献（可人工复核；材料提供了什么才是判定依据）",
                    related)
        return ("news_only",
                "规则预筛：材料未包含可核查的机制/方法类知识增量，待核查"
                "（如人工确认存在增量，可在审核后手动创建节点）",
                related)
    return ("new_node",
            "规则预筛：本次检索未找到相关节点，待确认；材料包含机制/方法类表述"
            "（待语义分析与人工确认后才算知识增量）",
            related)


def novelty_flags(source_summary_text: str, first_seen_now: bool, best_score: float,
                  published_at) -> dict:
    return {
        "source_claims_new": bool(NOVELTY_CLAIM_SIGNALS.search(source_summary_text or "")),
        "first_collected_at": None,  # 由调用方填入上游 discovered_at（首次采集时间）
        "first_seen_by_system": bool(first_seen_now),
        "retrieval_missed": best_score < 0.22,  # 本次检索未命中，不代表知识不存在
        "possible_increment": (
            "与本次检索到的已有节点相比可能存在增量（待语义分析与人工确认）"
            if best_score < 0.55 else "与已检索节点高度相似，增量待确认"),
        "unconfirmed": [
            "来源的新颖性主张未经独立验证",
            "与既有知识的历史关系未知",
            "摘要/摘录只能定位文字，不能证明结论成立",
        ],
        "published_at": published_at,  # 可能缺失：缺失即缺失，不补造
    }


def novelty_label(novelty: dict) -> str:
    """措辞边界：来源声称 ≠ 系统核实；未检索到 ≠ 不存在/用户不知道。"""
    parts = []
    if novelty["source_claims_new"]:
        parts.append("来源声称的新贡献（未经独立核实）")
    if novelty.get("retrieval_missed", novelty.get("retrieval_missed", False)):
        parts.append("本次检索未命中已有节点，待确认（不代表知识不存在）")
    if not parts:
        parts.append("尚不确定；本次检索未见明显增量")
    return "；".join(parts)


# ---------------- 上游接入（任务 1） ----------------

def parse_js_const(path: Path, const_name: str):
    """只解析 'const NAME = <JSON>;' 的明确数据格式；不执行任何代码（无 eval）。"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.match(rf"const {const_name} = (.*);\s*$", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


# 公开版默认不含任何私人数据源路径：上游候选池由环境变量
# PKA_DISCOVERY_UPSTREAM 指向 JSON 配置提供（如
# [{"path": "…/feed_candidates.js", "const": "FEED_CANDIDATES", "label": "…"}]）。
DEFAULT_UPSTREAM = []


def read_upstream(source: dict) -> dict:
    """只读读取一个上游候选文件；缺失/损坏返回明确状态，不抛异常。"""
    path = Path(source["path"])
    if not path.exists():
        return {"status": "missing", "label": source.get("label", path.name), "candidates": []}
    data = parse_js_const(path, source["const"])
    if data is None:
        return {"status": "unparseable", "label": source.get("label", path.name), "candidates": []}
    return {"status": "ok", "label": source.get("label", path.name),
            "candidates": data.get("candidates", []), "generated_at": data.get("generatedAt")}


# ---------------- 建议队列与接受（任务 5/6） ----------------

class DiscoveryService:
    def __init__(self, graph_service: GraphService, upstream: list[dict] | None = None,
                 clock=utcnow, analyzer=None, analyzer_budget=None, analyzer_mode="unconfigured",
                 note_processor=None, auto_accept: bool = False, note_reviewer=None,
                 require_ai_review: bool = False):
        self.svc = graph_service
        self.analyzer = analyzer                # 语义分析器；None = 未配置（只做规则预筛）
        self.analyzer_budget = analyzer_budget
        self.analyzer_mode = analyzer_mode
        self.note_processor = note_processor    # 笔记加工器（模块二）；None = 走确定性模板
        self.note_reviewer = note_reviewer
        self.require_ai_review = bool(require_ai_review)
        # 免逐条审核（模块六）：check() 语义判定为有增量即自动入库；
        # 必须配置语义分析器才生效——规则预筛结果绝不直接入库。
        self.auto_accept = bool(auto_accept)
        self.intents_path = self.svc.store.state_path.parent / "discovery_intents.json"
        self.check_lock = threading.Lock()      # 防重叠：一轮未完成不叠加下一轮
        self._periodic_thread = None
        self._periodic_stop = threading.Event()
        if upstream is not None:
            self.upstream = upstream
        else:
            # 测试/演示可用环境变量替换上游（合成数据隔离；不改变默认真实上游）
            override = os.environ.get("PKA_DISCOVERY_UPSTREAM")
            if override and Path(override).exists():
                try:
                    self.upstream = json.loads(Path(override).read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    self.upstream = DEFAULT_UPSTREAM
            else:
                self.upstream = DEFAULT_UPSTREAM
        self.clock = clock
        self.suggestions_path = self.svc.store.state_path.parent / "discovery_suggestions.json"
        self.candidate_dir = self.svc.store.vault_root / "候选笔记"

    # ---------- 接受意图日志（最小事务记录） ----------

    @contextmanager
    def _commit_guard(self):
        """material -> OS commit lock -> StateStore; nested calls reuse ownership."""
        with self.svc.material_lock:
            path = self.intents_path.with_name("discovery_commit.lock").resolve()
            held = getattr(_COMMIT_LOCKS, "paths", None)
            if held is None:
                held = _COMMIT_LOCKS.paths = set()
            if path in held:
                yield
                return
            with FileMutex(path):
                held.add(path)
                try:
                    yield
                finally:
                    held.remove(path)

    def _load_intents(self) -> list[dict]:
        if not self.intents_path.exists():
            return []
        try:
            data = json.loads(self.intents_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValidationError("接受意图日志损坏或无法读取，停止提交与恢复") from exc
        if not isinstance(data, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("intent_id"), str)
                or not isinstance(item.get("suggestion_id"), str) for item in data):
            raise ValidationError("接受意图日志结构损坏，停止提交与恢复")
        return data

    def _save_intents(self, intents: list[dict]) -> None:
        with self._commit_guard():
            self._load_intents()  # never overwrite a damaged journal with an empty one
            self.intents_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.intents_path.with_name(f".discovery_intents.json.tmp-{uuid.uuid4().hex}")
            try:
                with tmp.open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps(intents, ensure_ascii=False, indent=2))
                    handle.flush()
                    os.fsync(handle.fileno())
                tmp.replace(self.intents_path)
            finally:
                tmp.unlink(missing_ok=True)

    # ---------- 存储 ----------

    def _load_suggestions(self) -> list[dict]:
        if not self.suggestions_path.exists():
            return []
        try:
            data = json.loads(self.suggestions_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValidationError("建议队列文件损坏或无法读取，停止提交；图谱主状态保留") from exc
        if not isinstance(data, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str)
                for item in data):
            raise ValidationError("建议队列结构损坏，停止提交；图谱主状态保留")
        return data

    def _save_suggestions(self, suggestions: list[dict]) -> None:
        with self._commit_guard():
            self._load_suggestions()
            self.suggestions_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.suggestions_path.with_name(f".discovery_suggestions.json.tmp-{uuid.uuid4().hex}")
            try:
                with tmp.open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps(suggestions, ensure_ascii=False, indent=2))
                    handle.flush()
                    os.fsync(handle.fileno())
                tmp.replace(self.suggestions_path)
            finally:
                tmp.unlink(missing_ok=True)

    def _discovery_state(self, state: dict) -> dict:
        section = state.setdefault(DISCOVERY_SECTION, {})
        section.setdefault("seen", {})          # upstream_id -> 内容指纹
        section.setdefault("pending", [])       # 失败/待重试项（不因游标推进丢失）
        section.setdefault("last_check", None)
        section.setdefault("last_success", None)
        section.setdefault("auto_check_minutes", 0)   # 周期检查默认关闭
        section.setdefault("per_check_limit", 10)
        section.setdefault("remote_fetch", {"enabled": False})
        section.setdefault("model_budget", {"daily_model_calls_limit": 0, "used_today": 0, "date": None})
        return section

    def config(self) -> dict:
        state = self.svc.state()
        section = self._discovery_state(state)
        return {
            "last_check": section["last_check"],
            "last_success": section["last_success"],
            "auto_check_minutes": section["auto_check_minutes"],
            "per_check_limit": section["per_check_limit"],
            "remote_fetch": section["remote_fetch"],
            "model_budget": section["model_budget"],
            "pending": len(section["pending"]),
            "seen": len(section["seen"]),
            "auto_accept": bool(self.auto_accept and self.analyzer is not None),
            "semantic_calls_today": (section.get("model_calls") or {}).get("used", 0),
        }

    # ---------- 手动 / 周期检查（任务 7） ----------

    def start_periodic_if_configured(self) -> dict:
        """在**服务进程内**启动周期检查线程（不是浏览器页面；不是新的守护进程）。
        auto_check_minutes=0 表示关闭。多标签页共享同一进程实例，不会叠加。"""
        if self._periodic_thread is not None and not self._periodic_thread.is_alive():
            self._periodic_thread = None
        state = self.svc.state()
        section = self._discovery_state(state)
        minutes = int(section.get("auto_check_minutes") or 0)
        if minutes <= 0 or self._periodic_thread is not None:
            return {"periodic": "off" if minutes <= 0 else "already_running", "minutes": minutes}
        def loop():
            while True:
                # 每轮醒来先重读配置：间隔调整即时生效；关闭（0）则线程自退，
                # 再次开启时由 /api/discovery/config 路由重新拉起。
                try:
                    minutes = int(self._discovery_state(self.svc.state())
                                  .get("auto_check_minutes") or 0)
                except Exception:  # noqa: BLE001 —— 读配置失败按关闭处理
                    self._periodic_thread = None
                    return
                if minutes <= 0:
                    self._periodic_thread = None
                    return
                if self._periodic_stop.wait(max(10, minutes) * 60):
                    return
                try:
                    self.check(request_id=None)
                except Exception:
                    pass  # 失败项留在 pending，不丢弃

        self._periodic_stop.clear()
        self._periodic_thread = threading.Thread(target=loop, daemon=True, name="discovery-periodic")
        self._periodic_thread.start()
        return {"periodic": "started", "minutes": minutes}

    def stop_periodic(self, timeout=5):
        self._periodic_stop.set()
        if self._periodic_thread:
            self._periodic_thread.join(timeout)
        stopped = not (self._periodic_thread and self._periodic_thread.is_alive())
        if stopped:
            self._periodic_thread = None
        return {"stopped": stopped}

    def check(self, request_id: str | None = None) -> dict:
        """执行一次增量检查：读取上游 → 过滤已见 → 分级提取比较 → 生成建议。
        确定性实现，无模型调用；达到每批上限的条目留在 pending，不丢弃。"""
        if not self.check_lock.acquire(blocking=False):
            return {"skipped": True, "reason": "上一轮检查尚未完成，本轮跳过（不叠加）"}
        try:
            with self.svc.material_lock:
                return self._check_serial(request_id)
        finally:
            self.check_lock.release()

    def _check_serial(self, request_id: str | None = None) -> dict:
        out = self._check_inner(request_id)
        # 每条独立读最新状态并提交；与收件箱共用串行边界。
        if self.analyzer is not None and self.auto_accept:
            accept_out = self._auto_accept_phase()
            out["auto_published"] = len(accept_out["published"])
            out["auto_clued"] = len(accept_out["clued"])
            out["auto_deferred"] = len(accept_out["deferred"])
            out["auto_failed"] = len(accept_out["failed"])
        return out

    def _check_inner(self, request_id: str | None = None) -> dict:
        state = self.svc.state()
        section = self._discovery_state(state)
        now = self.clock()
        sources_status = []
        new_items = []
        seen = section["seen"]
        for source in self.upstream:
            result = read_upstream(source)
            sources_status.append({"label": result["label"], "status": result["status"],
                                   "count": len(result["candidates"])})
            if result["status"] != "ok":
                continue
            for cand in result["candidates"]:
                uid = str(cand.get("id") or "").strip()
                if not uid:
                    continue
                fp = hashlib.sha256(
                    json.dumps([cand.get("title"), cand.get("summary"), cand.get("canonicalUrl")],
                               ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()[:16]
                first_seen_now = uid not in seen
                if not first_seen_now and seen[uid] == fp:
                    continue  # 同一输入重复读取：不重复生成
                new_items.append({"cand": cand, "uid": uid, "fp": fp,
                                  "first_seen_now": first_seen_now,
                                  "source_label": result["label"]})
        limit = int(section["per_check_limit"])
        batch, deferred = new_items[:limit], new_items[limit:]
        suggestions = self._load_suggestions()
        created = []
        checked = 0
        for item in batch:
            checked += 1
            made = self._build_suggestions_for_item(
                suggestions, item["cand"], item["uid"], item["fp"],
                item["first_seen_now"], item["source_label"], now)
            created.extend(made)
            seen[item["uid"]] = item["fp"]

        # ---------- 语义阶段（模块六：机会判断的语义层） ----------
        # 预筛判为疑似增量（new_node/supplement）的建议，逐条做语义分析复核；
        # 未轮到的（每轮上限/每日预算耗尽）保持 pending，下轮优先补——不丢弃。
        semantic_done = []
        task_mode = getattr(self.svc, "task_mode", None) == "task"
        if self.analyzer is not None and not task_mode:
            backlog = created + [
                s for s in suggestions
                if s.get("status") == "pending" and s.get("verdict") in ("new_node", "supplement")
                and not s.get("semantic") and s not in created
            ]
            cap = self._semantic_cap(section)
            for sug in backlog[:cap]:
                self._semantic_analyze_suggestion(sug, now, section)
                semantic_done.append(sug["id"])

        # 先落盘预筛+语义结论；自动入库由 check() 的后置阶段逐条执行
        # （accept 每次独立加载最新 state——不与本批量写共享副本，防版本冲突）
        self._save_suggestions(suggestions)

        # 断点恢复：deferred 与处理失败项留在 pending（下次 check 优先重试）
        pending = [d["uid"] for d in deferred]
        section["seen"] = seen
        section["pending"] = pending
        section["last_check"] = now.isoformat(timespec="seconds")
        section["last_success"] = now.isoformat(timespec="seconds")
        self.svc.store.save(state, base_version=state["version"])
        self._save_suggestions(suggestions)
        return {
            "checked": checked, "created": len(created), "deferred": len(deferred),
            "suggestion_ids": [s["id"] for s in created],
            "semantic_done": len(semantic_done),
            "sources": sources_status,
            "last_success": section["last_success"],
            "analysis": self.analysis_status(),
            "note": ("规则预筛完成（deterministic_shallow）；语义分析"
                     + ("将在统一任务中依据完整原文执行" if task_mode
                        else "由配置的模型执行" if self.analyzer else "未配置、未执行")
                     + "；预筛结果不代表知识增量已核实"),
        }

    # ---------- 语义阶段辅助（模块六） ----------

    def _auto_accept_phase(self) -> dict:
        """check 后置阶段：语义判定有增量的 pending 建议 → 逐条 accept。

        每个 accept 独立加载最新 state 并原子提交；失败留队打日志不阻断。
        语义失败（semantic_error）或 uncertain/insufficient 的建议不进入本阶段。
        """
        published, clued, deferred, idempotent, failed = [], [], [], [], []
        suggestions = self._load_suggestions()
        task_mode = getattr(self.svc, "task_mode", None) == "task"
        targets = [s["id"] for s in suggestions
                   if s.get("status") == "pending"
                   and ((task_mode and s.get("verdict") not in ("news_only", "duplicate"))
                        or (not task_mode and s.get("semantic") is not None
                            and s.get("semantic_error") is None
                            and s.get("verdict") in ("new_node", "supplement", "revision")))]
        by_id = {s["id"]: s for s in suggestions}
        now = self.clock()
        for sid in targets:
            sug = by_id.get(sid) or {}
            adm = sug.get("admission") or {}
            # 分级跳过：暂时性故障按退避时间；材料不足类不自动重试（内容不变时）
            if adm.get("blocked_fingerprint") == self._content_fingerprint(sug):
                outcome = adm.get("outcome", "retry_transient")
                not_before = adm.get("not_before")
                if outcome == "needs_source":
                    continue
                if not_before and self.clock() < self._parse_local(not_before):
                    continue
            try:
                if getattr(self.svc, "task_mode", None) == "task" and getattr(self.svc, "workflow", None) is not None:
                    # 发现只登记任务；全文加工/审核由唯一任务执行者处理，不开第二套模型链。
                    source = next((s for s in sug.get("sources") or [] if s.get("path") or s.get("url")), None)
                    if source is None:
                        deferred.append(sid)
                        continue
                    from .workflow import Principal
                    request_source = ({"kind": "file", "path": str(self.svc.store.safe_read_path(source["path"]))}
                                      if source.get("path") else {"kind": "url", "url": source["url"]})
                    task = self.svc.workflow.submit({"contract_version": 1,
                        "request_id": "discovery:" + sid + ":" + self._content_fingerprint(sug),
                        "source": request_source,
                        "options": {"pipeline": "note", "publish_to": None, "origin_suggestion_id": sid}},
                        principal=Principal("local:owner", frozenset({"tasks:submit", "tasks:read:self", "tasks:control:self"})))
                    deferred.append(sid)
                    continue
                out = self.accept(None, sid, request_id=f"discovery-auto-{sid}", strict=True)
                if out.get("published"):
                    published.append(sid)
                elif out.get("clue"):
                    clued.append(sid)
                elif out.get("deferred"):
                    deferred.append(sid)
                elif out.get("already_accepted"):
                    idempotent.append(sid)
                else:
                    deferred.append(sid)
            except Exception as exc:  # noqa: BLE001
                failed.append(sid)
                print(f"[discovery] 自动入库失败（{type(exc).__name__}）", flush=True)
        return {"published": published, "clued": clued, "deferred": deferred,
                "idempotent": idempotent, "failed": failed}

    @staticmethod
    def _parse_local(value: str):
        from datetime import datetime as _dt

        try:
            return _dt.fromisoformat(str(value))
        except ValueError:
            return None

    def _semantic_cap(self, section: dict) -> int:
        """本轮语义分析条数上限：每轮上限与每日剩余额度取小。"""
        budget = self.analyzer_budget
        per_check = int(getattr(budget, "per_check_model_calls", 6) or 0) if budget else 6
        if per_check <= 0:
            per_check = 6
        ledger = section.setdefault("model_calls", {"date": "", "used": 0})
        today = self.clock().date().isoformat()
        if ledger.get("date") != today:
            ledger["date"], ledger["used"] = today, 0
        daily = int(getattr(budget, "daily_model_calls_limit", 0) or 0) if budget else 0
        if daily <= 0:
            return per_check
        return max(0, min(per_check, daily - int(ledger.get("used", 0))))

    def _semantic_spend(self, section: dict) -> None:
        """语义调用记账（含失败调用）：按自然日记账，存于 discovery 状态区。"""
        ledger = section.setdefault("model_calls", {"date": "", "used": 0})
        today = self.clock().date().isoformat()
        if ledger.get("date") != today:
            ledger["date"], ledger["used"] = today, 0
        ledger["used"] = int(ledger.get("used", 0)) + 1

    def _semantic_analyze_suggestion(self, sug: dict, now, section: dict) -> None:
        """对预筛建议执行语义复核：改写 verdict/semantic/action/reason。

        section 由调用方传入（check 自己的 state 副本），预算账本随之在该副本
        中累计并随 _check_inner 收尾统一落盘。
        失败（网络/解析/校验异常）时如实标注 semantic_error 并维持预筛结论，
        消耗预算不重试——下一轮 check 会因无 semantic 字段而重新排队。
        """
        from .discovery_model import SemanticModelError, validate_semantic_output

        self._semantic_spend(section)
        material = {"title": sug.get("name") or "",
                    "text": (sug.get("material") or {}).get("text") or ""}
        sources = [{"upstream_id": s.get("upstream_id"),
                    "source_name": s.get("source_name") or s.get("label") or "",
                    "url": s.get("url"), "path": None,
                    "read_status": s.get("read_status") or "readable_summary",
                    "published_at": s.get("published_at")}
                   for s in (sug.get("sources") or [])]
        related = [{"node_id": r.get("node_id"), "name": r.get("name"),
                    "scope": r.get("scope") or ""}
                   for r in (sug.get("related") or [])]
        try:
            raw = self.analyzer.analyze(material, sources, related, [])
            result = validate_semantic_output(raw, material["text"], related)
        except SemanticModelError as exc:
            sug["semantic_error"] = f"{exc.category}: {exc}"
            sug["reason"] = (sug.get("reason") or "") +                 f"；语义分析失败（{exc.category}），维持预筛结论待复核"
            return
        except Exception as exc:  # noqa: BLE001 —— 网络等异常不阻断本轮；只记本机异常信息
            sug["semantic_error"] = f"{type(exc).__name__}: {exc}"[:120]
            sug["reason"] = (sug.get("reason") or "") + "；语义分析异常，维持预筛结论待复核"
            return
        old = sug.get("verdict")
        sug.setdefault("prescreen_verdict", old)
        sug["verdict"] = result["classification"]
        sug["semantic"] = {
            "differences_vs_related": result["differences_vs_related"],
            "source_excerpts": result["source_excerpts"],
            "unlocated_excerpts": result["unlocated_excerpts"],
            "excerpt_note": result.get("excerpt_note"),
            "unknowns": result["unknowns"],
            "validation_issues": result.get("validation_issues"),
        }
        sug["action"] = ACTION_MAP.get(result["classification"], "待核查")
        note = "语义分析确认" if result["classification"] == old else "语义分析改判"
        sug["reason"] = (sug.get("reason") or "") + f"；{note}（模型）"

    def _candidate_tokens(self, cand: dict) -> set:
        # 相关检索只基于标题+摘要：topics 作为标签会稀释包含度（AI 等高频短词）
        return tokens(" ".join([cand.get("title") or "", cand.get("summary") or ""]))

    def _related_nodes(self, state: dict, cand_tokens: set, cand_topics: set) -> list[dict]:
        related = []
        for node in state["nodes"]:
            if node["archived"]:
                continue
            node_text = " ".join([node["name"], *(node.get("aliases") or []),
                                  node.get("scope") or "", " ".join(node.get("topics") or [])])
            score = containment(cand_tokens, tokens(node_text))
            if score >= 0.15:
                related.append({"node_id": node["id"], "name": node["name"],
                                "scope": node.get("scope") or "",
                                "domain": node["domain"], "score": round(score, 3),
                                "content_version": node["content_version"]})
        related.sort(key=lambda r: r["score"], reverse=True)
        return related[:5]

    def _build_suggestions_for_item(self, suggestions: list[dict], cand: dict, uid: str,
                                    fp: str, first_seen_now: bool, source_label: str,
                                    now: datetime) -> list[dict]:
        title = str(cand.get("title") or "").strip()
        summary = str(cand.get("summary") or "")
        material = classify_material(summary)
        extraction = extract_contribution(material["text"], title)
        state = self.svc.state()
        cand_tokens = self._candidate_tokens(cand)
        related = self._related_nodes(state, cand_tokens, set(cand.get("topics") or []))
        verdict, reason, related_used = compare_with_vault(extraction, title, material["text"], related)
        novelty = novelty_flags(summary, first_seen_now, related_used[0]["score"] if related_used else 0.0,
                                cand.get("publishedAt"))
        if first_seen_now:
            novelty["first_collected_at"] = cand.get("discoveredAt")
        made = []
        # 聚合：同一事件多来源 → 若已存在标题高度相似的 pending 建议则合并来源
        title_tokens = tokens(title)
        target = None
        for sug in suggestions:
            if sug["status"] != "pending" or sug["verdict"] != verdict:
                continue
            if jaccard(title_tokens, tokens(sug["name"])) >= 0.5:
                target = sug
                break
        source_record = {
            "upstream_id": uid, "fingerprint": fp, "label": source_label,
            "url": cand.get("canonicalUrl"), "source_name": cand.get("sourceName"),
            "source_type": cand.get("sourceType"), "published_at": cand.get("publishedAt"),
            "discovered_at": cand.get("discoveredAt"),
            "summary_excerpt": _clip(summary, 220),
            "read_status": material["level"],
        }
        if target is not None:
            if all(src["upstream_id"] != uid for src in target["sources"]):
                target["sources"].append(source_record)
                target["aggregation_note"] = "同一事件多来源聚合为一条建议；来源差异保留于 sources"
            return []
        suggestion = {
            "id": "sug-" + uuid.uuid4().hex[:12],
            "created_at": now.isoformat(timespec="seconds"),
            "status": "pending",
            "verdict": verdict,
            "name": _clip(title, 80) if title else "（无标题候选）",
            "novelty": novelty,
            "novelty_label": novelty_label(novelty),
            "contribution": extraction,
            "related": related_used,
            "sources": [source_record],
            "reason": reason,
            "material": material,
            "action": {"new_node": "新增候选节点", "supplement": "补充资料关联",
                       "revision": "保存更新提案", "duplicate": "不处理（重复）",
                       "news_only": "不处理（仅新闻线索）", "insufficient": "待核查"}[verdict],
            "accepted": None,
        }
        suggestions.append(suggestion)
        made.append(suggestion)
        return made

    # ---------- 队列视图 ----------

    def list_suggestions(self, status: str = "pending", limit: int = 20) -> dict:
        suggestions = self._load_suggestions()
        rows = [s for s in suggestions if s["status"] == status] if status != "all" else list(suggestions)
        rows = sorted(rows, key=lambda s: s["created_at"], reverse=True)[:limit]
        orphans = self._orphan_notes(suggestions)
        return {"suggestions": rows, "count": len(rows), "orphan_notes": orphans,
                "analysis": self.analysis_status(),
                "banner": "待审核建议。规则预筛结果不等于语义分析结论，更不代表用户已经了解或掌握。"}

    def analysis_status(self) -> dict:
        """能力三层区分：规则预筛（本轮已做）/ 语义分析（按配置）/ 用户审核（始终必需）。"""
        if self.analyzer is not None:
            semantic = {"status": "configured", "adapter": self.analyzer.describe()}
        else:
            semantic = {"status": "unconfigured",
                        "reason": "未配置发现模块模型（PKA_DISCOVERY_ADAPTER=http + "
                                  "PKA_DISCOVERY_API_BASE/API_KEY/MODEL）；语义分析未执行，"
                                  "规则预筛结果仅供初筛参考"}
        wired = "已接入 check() 主流程（预筛→语义→自动入库）" if self.analyzer is not None             else "未接入：仅规则预筛，候选池信息不会自动入库"
        return {"rule_prescreen": "已执行（确定性规则：可解释、可回归；不能认定知识增量已核实）",
                "semantic_analysis": dict(semantic, main_flow=wired),
                "auto_accept": bool(self.auto_accept and self.analyzer is not None),
                "user_review": ("免审直推模式：语义判定有增量即自动入库（uncertain 不入库）；"
                                "节点保持 untested，掌握以测验为准"
                                if self.auto_accept and self.analyzer is not None
                                else "必需：所有建议必须经人工审核，接受也不代表用户已了解或掌握")}

    def export_migration_manifest(self) -> dict:
        """导出迁移清单：本模块创建的所有对象及其路径，供精确回退（不整目录删除）。"""
        suggestions = self._load_suggestions()
        accepted = [s for s in suggestions if s.get("accepted")]
        manifest = {
            "nodes": [{"id": s["accepted"]["node_id"], "name": s["name"]}
                      for s in accepted if s["verdict"] == "new_node"],
            "notes": [{"path": f"{CANDIDATE_NOTE_DIR}/{s['accepted']['note']}",
                       "suggestion_id": s["id"]}
                      for s in accepted if s["accepted"].get("note")],
            "supplements": [{"node_id": s["accepted"]["node_id"],
                             "suggestion_id": s["id"]}
                            for s in accepted if s["verdict"] == "supplement"],
            "revisions": [{"node_id": s["accepted"]["node_id"],
                           "suggestion_id": s["id"]}
                          for s in accepted if s["verdict"] == "revision"],
            "state_file": str(self.svc.store.state_path),
        }
        return manifest

    def _orphan_notes(self, suggestions: list[dict]) -> list[dict]:
        """两段提交的孤儿扫描：候选笔记文件存在但建议未处于 accepted 状态。"""
        accepted_ids = {s["accepted"]["suggestion_note"] for s in suggestions
                        if s.get("accepted") and s["accepted"].get("suggestion_note")}
        orphans = []
        for path in sorted(self.candidate_dir.glob("discovery-*.md")):
            if path.name not in accepted_ids:
                orphans.append({"path": path.name, "hint": "笔记文件已生成但建议未确认入库；可重新接受（将复用该文件）或手动删除"})
        return orphans

    # ---------- 崩溃恢复（最小事务：意图日志） ----------

    def recover_clue_intents(self) -> dict:
        """线索分支的崩溃恢复：识别自己创建的文件并补完建议提交。

        - 文件存在且指纹一致 → 补完建议保存（幂等，不重建）；
        - 文件被人工修改（指纹不一致）→ needs_manual（不覆盖、不删除）；
        - 文件丢失 → 用确定性路径重建线索笔记后补完提交。
        """
        results = []
        intents = self._load_intents()
        clue_intents = [i for i in intents if i.get("kind") == "clue"]
        if not clue_intents:
            return {"recovered": 0, "results": []}
        remaining = [i for i in intents if i.get("kind") != "clue"]
        suggestions = self._load_suggestions()
        for intent in clue_intents:
            sug = next((s for s in suggestions if s["id"] == intent["suggestion_id"]), None)
            if sug and sug.get("status") == "accepted":
                results.append({"intent_id": intent["intent_id"],
                                "action": "already_committed"})
                continue
            if sug is None or sug.get("status") != "pending":
                results.append({"intent_id": intent["intent_id"], "action": "needs_manual",
                                "reason": "建议缺失或已改变状态，不自动恢复"})
                remaining.append(intent)
                continue
            path = self.svc.store.safe_read_path(intent["clue_relpath"])
            current = (hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                       if path.exists() else None)
            if current and current != intent["clue_fingerprint"]:
                results.append({"intent_id": intent["intent_id"], "action": "needs_manual",
                                "reason": "线索笔记已被人工修改（指纹不一致）；未覆盖未删除"})
                remaining.append(intent)
                continue
            if not path.exists():
                sug_src = sug or {"id": intent["suggestion_id"], "name": "线索",
                                  "contribution": {}, "sources": [],
                                  "verdict": "new_node", "reason": "", "novelty_label": ""}
                admission = (sug.get("admission") if sug else None) or {
                    "reason": "崩溃恢复重建", "processing_status": "unknown"}
                rebuilt = self._write_clue_note(sug_src, intent.get("domain", "待分类领域"),
                                                admission, None, prior=intent)
                if not rebuilt.get("reused"):
                    results.append({"intent_id": intent["intent_id"],
                                    "action": "rebuilt_clue"})
            if sug is not None:
                sug["status"] = "accepted"
                sug["accepted"] = {"as": "clue", "note": intent["clue_relpath"],
                                   "at": self.clock().isoformat(timespec="seconds")}
            results.append({"intent_id": intent["intent_id"], "action": "completed"})
        if any(r["action"] == "completed" for r in results):
            self._save_suggestions(suggestions)   # 保存包含本轮修改的同一列表
        self._save_intents(remaining)
        return {"recovered": len([r for r in results if r["action"] in ("completed", "rebuilt_clue")]),
                "results": results}

    def recover_intents(self) -> dict:
        """Recover exact recorded bytes, then state, then suggestion, then journal."""
        with self._commit_guard():
            intents = self._load_intents()
            suggestions = self._load_suggestions()
            results, completed = [], set()
            unique = {}
            for item in intents:
                if item.get("kind") == "clue":
                    continue
                if not isinstance(item.get("node"), dict) or not item["node"].get("id"):
                    raise ValidationError("入库意图缺少节点身份，停止恢复")
                previous = unique.get(item["intent_id"])
                if previous and previous["node"]["id"] != item["node"]["id"]:
                    raise ValidationError("同一入库意图存在冲突节点，停止恢复")
                if previous is None or item.get("note_payload") is not None:
                    unique[item["intent_id"]] = item
            for intent in unique.values():
                sug = next((s for s in suggestions if s["id"] == intent["suggestion_id"]), None)
                if sug is None or sug.get("status") not in ("pending", "accepted"):
                    results.append({"intent_id": intent["intent_id"], "action": "needs_manual",
                                    "reason": "建议缺失或状态已改变，不自动提交"})
                    continue
                state = self.svc.state()
                node_id = intent["node"]["id"]
                node = next((n for n in state["nodes"] if n["id"] == node_id), None)
                links = [l for l in state["note_links"] if l["node_id"] == node_id
                         and l["ref"].get("note_id") == intent["note_id"]]
                linked = bool(links)
                expected_cv = intent["node"].get("content_version")
                if node and expected_cv is not None and node["content_version"] != expected_cv:
                    results.append({"intent_id": intent["intent_id"], "action": "needs_manual",
                                    "reason": "目标节点内容版本已变化，不恢复旧内容"})
                    continue
                relpath = intent.get("note_relpath") or (
                    links[0]["ref"].get("path") if links else None)
                # Legacy already-committed records may have no path or payload.
                if not relpath and node and linked and sug.get("status") == "accepted":
                    completed.add(intent["intent_id"])
                    results.append({"intent_id": intent["intent_id"], "action": "already_committed",
                                    "node_id": node_id})
                    continue
                if not relpath:
                    results.append({"intent_id": intent["intent_id"], "action": "needs_manual",
                                    "reason": "旧意图缺少真实笔记路径，不能猜测恢复"})
                    continue
                path = self.svc.store.safe_read_path(relpath)
                payload = intent.get("note_payload")
                if payload is not None:
                    if not isinstance(payload, str):
                        raise ValidationError("入库意图正文类型损坏，停止恢复")
                    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                    if digest[:16] != intent.get("note_fingerprint") or (
                            intent.get("note_sha256") and digest != intent["note_sha256"]):
                        raise ValidationError("入库意图正文指纹不一致，停止恢复")
                current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
                if current and (current[:16] != intent.get("note_fingerprint") or (
                        intent.get("note_sha256") and current != intent["note_sha256"])):
                    results.append({"intent_id": intent["intent_id"], "action": "needs_manual",
                                    "reason": "笔记已被人工修改，未删除未覆盖", "note": relpath})
                    continue
                if not path.exists():
                    if payload is None:
                        results.append({"intent_id": intent["intent_id"], "action": "needs_manual",
                                        "reason": "原文件缺失且旧意图无完整正文，不用模板重建"})
                        continue
                    path = self.svc.store.safe_write_path(relpath)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
                    try:
                        with tmp.open("xb") as handle:
                            handle.write(payload.encode("utf-8"))
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.link(tmp, path)
                    finally:
                        tmp.unlink(missing_ok=True)
                if linked and intent.get("note_relpath") and any(
                        l["ref"].get("path") != relpath for l in links):
                    results.append({"intent_id": intent["intent_id"], "action": "needs_manual",
                                    "reason": "已存笔记关联路径与意图冲突"})
                    continue
                was_committed = bool(node and linked)
                if not was_committed:
                    review = (intent.get("note_processing") or {}).get("ai_review") or {}
                    if self.require_ai_review and review.get("verdict") != "pass":
                        results.append({"intent_id": intent["intent_id"], "action": "needs_manual",
                                        "reason": "旧意图缺少AI审核通过记录，未补提交"})
                        continue
                    if node is None:
                        from .models import new_node
                        restored = new_node(name=intent["node"]["name"], domain=intent["node"]["domain"],
                                            scope=intent["node"]["scope"])
                        restored.update(intent["node"])
                        state["nodes"].append(restored)
                    if not linked:
                        state["note_links"].append(make_note_link(node_id, {
                            "kind": "note_id", "note_id": intent["note_id"], "path": relpath}))
                    try:
                        self.svc.store.save(state, base_version=state["version"])
                    except Exception:
                        results.append({"intent_id": intent["intent_id"], "action": "conflict",
                                        "reason": "恢复提交失败，保留原笔记与意图供再次恢复"})
                        continue
                sug["status"] = "accepted"
                sug["accepted"] = dict(intent.get("accepted") or {
                    "node_id": node_id, "note": intent["note_name"], "at": intent["at"]},
                    note_relpath=relpath)
                if intent.get("note_processing") is not None:
                    sug["note_processing"] = intent["note_processing"]
                if intent.get("admission") is not None:
                    sug["admission"] = intent["admission"]
                self._save_suggestions(suggestions)  # must succeed before clearing the journal
                completed.add(intent["intent_id"])
                results.append({"intent_id": intent["intent_id"],
                                "action": "already_committed" if was_committed else "completed",
                                "node_id": node_id, "note": relpath})
            if completed:
                self._save_intents([i for i in intents if i["intent_id"] not in completed])
            clues = self.recover_clue_intents()
            results.extend(clues["results"])
            return {"recovered": results}

    # ---------- 接受 / 稍后 / 忽略（任务 6） ----------

    def analyze_frozen_material(self, *, context):
        """逐段分析完整冻结来源；结果只新增建议，不发布笔记。"""
        from .discovery_model import validate_semantic_output
        from .note_processor import segment_material
        from .workflow import WorkflowError
        if self.analyzer is None:
            raise WorkflowError("model_unconfigured", "知识增量分析模型未配置")
        source_artifact = next((a for a in reversed(context.artifacts)
                               if a.get("kind") == "source_text" and a.get("complete")), None)
        if source_artifact is None:
            raise WorkflowError("source_missing", "缺少完整冻结原文")
        context._service._verify([source_artifact])
        source_text = (context.vault_root / source_artifact["path"]).read_text(encoding="utf-8")
        title = Path(context.source.get("path") or context.source.get("url") or "原始材料").stem
        with self.svc.material_lock:
            related = self._related_nodes(self.svc.state(),
                        self._candidate_tokens({"title": title, "summary": source_text}), set())
        sources = [{"upstream_id": context.task_id, "source_name": "冻结原始材料",
                    "path": source_artifact["path"], "url": context.source.get("url"),
                    "read_status": "full_text", "published_at": None}]
        binding = {"source": source_artifact["sha256"], "analyzer": self.analyzer.describe(),
                   "related": [{"node_id": r["node_id"], "content_version": r["content_version"]}
                               for r in related]}
        checkpoint = context.load_checkpoint() or {}
        if checkpoint.get("binding") != binding:
            checkpoint = {"binding": binding, "parts": {}}
        parts, _ = segment_material(source_text, max_chars=16000)
        for index, part in enumerate(parts):
            if context.cancel_requested():
                raise WorkflowError("cancelled", "任务已请求取消")
            key = str(index)
            if key not in checkpoint["parts"]:
                raw = self.analyzer.analyze({"title": title, "text": part["text"]}, sources, related, [])
                checkpoint["parts"][key] = validate_semantic_output(raw, part["text"], related)
                context.save_checkpoint(checkpoint)
            context.report_progress(index + 1, len(parts))
        results = list(checkpoint["parts"].values())
        classifications = {r["classification"] for r in results}
        useful = classifications & {"new_node", "supplement", "revision", "duplicate"}
        verdict = ("uncertain" if "uncertain" in classifications or len(useful) > 1
                   else next(iter(useful)) if useful
                   else "news_only" if classifications == {"news_only"} else "insufficient")
        contribution = {"problem": None, "claims": [], "differences": [], "conditions_limits": []}
        for result in results:
            current = result["contribution"]
            contribution["problem"] = contribution["problem"] or current.get("problem")
            for key in ("claims", "differences", "conditions_limits"):
                for value in current.get(key) or []:
                    if value not in contribution[key]:
                        contribution[key].append(value)
        suggestion_id = "sug-task-" + context.task_id
        suggestion = {"id": suggestion_id, "created_at": self.clock().isoformat(), "status": "pending",
            "verdict": verdict, "name": title[:80], "sources": sources, "related": related,
            "contribution": contribution, "content_sha256": source_artifact["sha256"],
            "material": {"level": "full_text", "text": "", "path": source_artifact["path"]},
            "reason": "完整冻结原文分段完成语义分析；检索未命中不等于知识不存在",
            "novelty_label": "相对已有节点的可能增量，未经独立核实",
            "semantic": {"segments": len(parts), "complete": True}, "accepted": None,
            "action": ACTION_MAP.get(verdict, "待核查")}
        with self.svc.material_lock:
            suggestions = self._load_suggestions()
            if not any(s["id"] == suggestion_id for s in suggestions):
                suggestions.append(suggestion)
                self._save_suggestions(suggestions)
        path = context.work_dir / "知识增量分析.json"
        path.write_text(json.dumps(suggestion, ensure_ascii=False), encoding="utf-8")
        artifact = dict(self._prepared_artifact(context, path, "suggestion"), suggestion_id=suggestion_id)
        if verdict != "new_node":
            return {"outcome": "needs_review", "artifacts": [artifact], "result": {"suggestion_id": suggestion_id},
                    "error": {"code": verdict, "message": "该材料不是可独立新增的知识笔记，已保留分析结果",
                              "retryable": False}}
        return {"suggestion_id": suggestion_id, "artifacts": [artifact]}

    @staticmethod
    def _preparation_fingerprint(suggestion):
        fields = {key: suggestion.get(key) for key in
                  ("id", "name", "verdict", "sources", "material", "contribution", "related")}
        return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def _suggestion_snapshot(self, suggestion_id):
        with self.svc.material_lock:
            suggestion = next((s for s in self._load_suggestions() if s["id"] == suggestion_id), None)
            if suggestion is None:
                raise NotFound("建议", suggestion_id)
            return json.loads(json.dumps(suggestion, ensure_ascii=False))

    def _prepared_artifact(self, context, path, kind):
        artifact = {"kind": kind, "path": path.relative_to(context.vault_root).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "complete": True}
        # 注册在服务端账本，不接受客户端自称可信的产物路径。
        def register(task):
            if artifact not in task["outputs"]:
                task["outputs"].append(artifact)
        context._service.store.update(context.task_id, register)
        return artifact

    def _prepare_result(self, suggestion, proc_result, context, revision_round=0):
        if context.cancel_requested():
            from .workflow import WorkflowError
            raise WorkflowError("cancelled", "任务已请求取消")
        proc = json.loads(json.dumps(proc_result, ensure_ascii=False))
        proc.get("source", {}).pop("text", None)  # 原文另有冻结文件，不复制到技术账本。
        tag = uuid.uuid4().hex
        body_path = context.work_dir / f"笔记草稿-{tag}.md"
        body_path.write_text(proc.get("body") or "", encoding="utf-8")
        envelope = {"suggestion_id": suggestion["id"], "processing_result": proc,
                    "preview_path": body_path.relative_to(context.vault_root).as_posix(),
                    "preview_sha256": hashlib.sha256(body_path.read_bytes()).hexdigest(),
                    "suggestion_fingerprint": self._preparation_fingerprint(suggestion),
                    "revision_round": revision_round}
        prepared_path = context.work_dir / f"prepared-{tag}.json"
        prepared_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        artifacts = [self._prepared_artifact(context, body_path, "note_body"),
                     self._prepared_artifact(context, prepared_path, "prepared_note")]
        artifacts[-1]["prepared_ref"] = artifacts[-1]["path"]
        context._service.store.update(context.task_id,
            lambda task: task["outputs"].append(artifacts[-1]) if artifacts[-1] not in task["outputs"] else None)
        admission = self._admission(proc)
        result = {"prepared_ref": artifacts[-1]["path"], "artifacts": artifacts,
                  "admission": admission, "revision_round": revision_round}
        if not admission["publishable"]:
            category = proc.get("category") or admission["processing_status"]
            retryable = admission["outcome"] == "retry_transient" and category not in (
                "authentication", "http", "context_limit", "input_limit", "checkpoint_corrupt", "skill_missing")
            result.update(outcome="failed" if retryable else "blocked",
                error={"code": category, "message": admission["reason"], "retryable": retryable})
        return result

    def prepare_note(self, suggestion_id, *, context):
        """只生成任务区草稿；不持有正式库写锁、不创建知识节点。"""
        from .workflow import WorkflowError
        if self.note_processor is None:
            raise WorkflowError("model_unconfigured", "笔记生成模型未配置")
        suggestion = self._suggestion_snapshot(suggestion_id)
        if suggestion.get("status") != "pending" or suggestion.get("verdict") != "new_node":
            raise WorkflowError("state_conflict", "建议当前不能生成新笔记", http_status=409)
        result = self.note_processor.process(suggestion=suggestion,
                    vault_root=self.svc.store.vault_root,
                    remote_fetch=self._remote_fetch_enabled(), task_context=context)
        return self._prepare_result(suggestion, result, context)

    def _load_prepared(self, prepared_ref, context):
        from .workflow import WorkflowError
        task = context._service.store.get(context.task_id)
        artifact = next((a for a in task["outputs"] if a.get("kind") == "prepared_note"
                         and a.get("path") == prepared_ref), None)
        if artifact is None:
            raise WorkflowError("permission_denied", "未登记的草稿引用", http_status=403)
        context._service._verify([artifact])
        data = json.loads((context.vault_root / prepared_ref).read_text(encoding="utf-8"))
        preview = next((a for a in task["outputs"] if a.get("kind") == "note_body"
                        and a.get("path") == data.get("preview_path")
                        and a.get("sha256") == data.get("preview_sha256")), None)
        if preview is None:
            raise WorkflowError("source_changed", "草稿预览与发布正文无法对应")
        context._service._verify([preview])
        return data, artifact

    def validate_prepared(self, prepared_ref, *, context):
        prepared, _ = self._load_prepared(prepared_ref, context)
        admission = self._admission(prepared["processing_result"])
        return dict(admission, prepared_ref=prepared_ref)

    def _review_result(self, proc, context=None):
        if self.note_reviewer is None:
            return {"verdict": "blocked", "issues": [{"reason": "AI审核模型未配置"}],
                    "coverage": {"complete": False}}
        if context is not None:
            source = next((a for a in context._service.store.get(context.task_id)["outputs"]
                           if a.get("kind") == "source_text" and a.get("complete")), None)
            if source is None:
                return {"verdict": "blocked", "issues": [{"reason": "缺少冻结原文全文"}],
                        "coverage": {"complete": False}}
            context._service._verify([source])
            source_text = (context.vault_root / source["path"]).read_text(encoding="utf-8")
        else:
            source_text = (proc.get("source") or {}).get("text") or ""
        if not source_text or hashlib.sha256(source_text.encode()).hexdigest() != proc.get("source_sha256"):
            return {"verdict": "blocked", "issues": [{"reason": "原文指纹不一致或缺失"}],
                    "coverage": {"complete": False}}
        return self.note_reviewer.review(source_text, proc["body"], proc.get("skill_text") or "",
                                          task_context=context)

    def review_prepared(self, prepared_ref, *, context):
        prepared, artifact = self._load_prepared(prepared_ref, context)
        proc = prepared["processing_result"]
        admission = self._admission(proc)
        if not admission["publishable"]:
            return {"verdict": "blocked", "issues": [{"reason": admission["reason"]}],
                    "coverage": {"complete": False}}
        review = self._review_result(proc, context)
        if review.get("verdict") == "pass" and not (review.get("coverage") or {}).get("complete"):
            review = dict(review, verdict="blocked", issues=[{"reason": "审核未覆盖完整原文"}])
        approval = {"prepared_sha256": artifact["sha256"], "review": review}
        context._service.store.update(context.task_id,
            lambda task: task.setdefault("review_approvals", {}).__setitem__(prepared_ref, approval))
        return dict(review, prepared_ref=prepared_ref, revision_round=prepared["revision_round"])

    def revise_prepared(self, prepared_ref, feedback, *, context):
        from .workflow import StageContext, WorkflowError
        prepared, _ = self._load_prepared(prepared_ref, context)
        if prepared["revision_round"] >= 2:
            raise WorkflowError("review_limit", "AI审核修订已达两轮，草稿保留待处理")
        suggestion = self._suggestion_snapshot(prepared["suggestion_id"])
        if self._preparation_fingerprint(suggestion) != prepared["suggestion_fingerprint"]:
            raise WorkflowError("source_changed", "建议材料已变化，旧草稿不能继续修订")
        suggestion["revision_feedback"] = feedback
        # 沿用生成阶段提取检查点，修订只重做成文而非重读全部来源。
        generate_context = StageContext(context._service,
                        context._service.store.get(context.task_id), "generate")
        result = self.note_processor.process(suggestion=suggestion,
                    vault_root=self.svc.store.vault_root,
                    remote_fetch=self._remote_fetch_enabled(), task_context=generate_context)
        return self._prepare_result(suggestion, result, context, prepared["revision_round"] + 1)

    def commit_prepared(self, prepared_ref, *, context):
        from .workflow import WorkflowError
        prepared, artifact = self._load_prepared(prepared_ref, context)
        task = context._service.store.get(context.task_id)
        approval = task.get("review_approvals", {}).get(prepared_ref) or {}
        review = approval.get("review") or {}
        if (approval.get("prepared_sha256") != artifact["sha256"] or review.get("verdict") != "pass"
                or not (review.get("coverage") or {}).get("complete")):
            raise WorkflowError("review_required", "没有针对本版草稿的完整AI审核通过记录")
        proc = prepared["processing_result"]
        # 审批后的文件、原文、建议仍须相符；前提拒绝发生在写入意图之前。
        with context._service._lock(context.task_id):
            with self.svc.material_lock:
                if context.cancel_requested():
                    raise WorkflowError("cancelled", "任务已请求取消")
                source = next((a for a in task["outputs"] if a.get("kind") == "source_text"
                               and a.get("complete")), None)
                if source is None:
                    raise WorkflowError("source_missing", "审核原文产物缺失")
                context._service._verify([source, artifact])
                text = (context.vault_root / source["path"]).read_text(encoding="utf-8")
                if hashlib.sha256(text.encode()).hexdigest() != proc.get("source_sha256"):
                    raise WorkflowError("source_changed", "审核后原文已变化")
                suggestion = self._suggestion_snapshot(prepared["suggestion_id"])
                if self._preparation_fingerprint(suggestion) != prepared["suggestion_fingerprint"]:
                    raise WorkflowError("source_changed", "来源建议已变化，不发布旧版草稿")
                def commit():
                    return self._accept_one(None, prepared["suggestion_id"],
                    request_id=task["request_id"], strict=True,
                    proc_result_override=dict(proc, ai_review={"verdict": "pass",
                        "reviewer_mode": review.get("reviewer_mode"), "coverage": review["coverage"]}))
                result = context.run_effect("library:" + prepared["suggestion_id"],
                                            kind="local_commit", action=commit)
                node_id = result.get("node_id") or (result.get("accepted") or {}).get("node_id")
                accepted = self._suggestion_snapshot(prepared["suggestion_id"]).get("accepted") or {}
                note_relpath = accepted.get("note_relpath")
                link = next((l for l in self.svc.state()["note_links"]
                             if l["node_id"] == node_id and l["ref"].get("path") == note_relpath), None)
                if link:
                    path = self.svc.store.safe_read_path(link["ref"]["path"])
                    result = dict(result, artifacts=[self._prepared_artifact(context, path, "note")])
                return result

    def accept(self, session_id_unused: None, suggestion_id: str, edits: dict | None = None,
               request_id: str | None = None, strict: bool = False) -> dict:
        proc_result = None
        if self.require_ai_review or self.note_reviewer is not None:
            suggestion = self._suggestion_snapshot(suggestion_id)
            if suggestion["status"] == "pending" and suggestion["verdict"] == "new_node":
                if self.note_processor is None:
                    raise ValidationError("生成模型未配置，不发布正式笔记")
                proc_result = self.note_processor.process(suggestion=suggestion,
                    vault_root=self.svc.store.vault_root, remote_fetch=self._remote_fetch_enabled())
                if self._admission(proc_result)["publishable"]:
                    review = self._review_result(proc_result)
                    if review.get("verdict") != "pass" or not (review.get("coverage") or {}).get("complete"):
                        raise ValidationError("AI审核未通过，未写入正式笔记")
                    proc_result["ai_review"] = review
        with self.svc.material_lock:
            return self._accept_one(session_id_unused, suggestion_id, edits,
                                    request_id, strict, proc_result_override=proc_result)

    def _accept_one(self, session_id_unused: None, suggestion_id: str, edits: dict | None = None,
                    request_id: str | None = None, strict: bool = False,
                    *, proc_result_override=None) -> dict:
        suggestions = self._load_suggestions()
        sug = next((s for s in suggestions if s["id"] == suggestion_id), None)
        if sug is None:
            raise NotFound("建议", suggestion_id)
        if request_id and sug.get("accepted") and sug["accepted"].get("request_id") == request_id:
            return dict(sug["accepted"], replayed=True)
        if sug["status"] == "accepted":
            return {"verdict": sug["verdict"],
                    "already_accepted": True,
                    "accepted": sug.get("accepted"),
                    "note": "该建议已接受过；返回既有结果，未重复创建"}
        if sug["status"] != "pending":
            raise ValidationError(f"建议当前状态为 {sug['status']!r}，不能接受")
        edits = edits or {}
        state = self.svc.state()
        verdict = sug["verdict"]
        now_iso = self.clock().isoformat(timespec="seconds")

        if verdict == "new_node":
            # 模块二+内容准入（2026-10-03 审查修复）：先加工，再按机器质量门槛
            # 判定是否可发布为正式笔记。未达门槛：自动路径(strict)留队待重试，
            # 人工路径降级为候选目录里的线索笔记（不建节点、不进正式区）。
            proc_result = proc_result_override
            if proc_result is None and self.note_processor is not None:
                try:
                    proc_result = self.note_processor.process(
                        suggestion=sug, vault_root=self.svc.store.vault_root,
                        remote_fetch=self._remote_fetch_enabled())
                except Exception as exc:  # noqa: BLE001 —— 加工器意外异常按失败处理
                    print(f"[note-processor] 加工异常：{type(exc).__name__}",
                          flush=True)
                    proc_result = {"ok": False, "reason": type(exc).__name__}
            admission = self._admission(proc_result)
            if self.require_ai_review and admission["publishable"] and not (proc_result.get("ai_review") or {}).get("verdict") == "pass":
                raise ValidationError("缺少AI审核通过记录，不发布正式笔记")
            if not admission["publishable"]:
                return self._accept_as_clue_or_defer(
                    suggestions, sug, admission, proc_result, now_iso,
                    request_id=request_id, strict=strict, edits=edits)
            if proc_result and proc_result.get("ok") and not edits.get("name"):
                name = proc_result["title"].strip()[:80]
            else:
                name = (edits.get("name") or sug["name"]).strip()[:80]
            # 领域自动推导（单动作直推，无人改领域）：edits > 相关度最高节点 > 待分类
            domain = (edits.get("domain") or "").strip()
            if not domain:
                for r in sug.get("related") or []:
                    if r.get("domain"):
                        domain = str(r["domain"]).strip()
                        break
            domain = domain or "待分类领域"
            scope = (edits.get("scope") or self._default_scope(sug)).strip()
            intent_id = f"intent-{sug['id']}"
            with self._commit_guard():
                if any(i["intent_id"] == intent_id for i in self._load_intents()):
                    self.recover_intents()
                suggestions = self._load_suggestions()
                sug = next((s for s in suggestions if s["id"] == suggestion_id), None)
                if sug is None:
                    raise NotFound("建议", suggestion_id)
                if sug["status"] == "accepted":
                    accepted = sug.get("accepted") or {}
                    return {"verdict": "new_node", "already_accepted": True,
                            "accepted": accepted, "node_id": accepted.get("node_id"),
                            "note": accepted.get("note"), "published": bool(accepted.get("node_id")),
                            "layer": "vision"}
                if sug["status"] != "pending":
                    raise ValidationError("建议状态已变化，停止提交")
                if any(i["intent_id"] == intent_id for i in self._load_intents()):
                    raise ValidationError("该建议存在待处理的恢复意图，未重复创建或覆盖")
                state = self.svc.state()
                node = next((n for n in state["nodes"]
                             if n["name"] == name and n["scope"] == scope), None)
                if node is None:
                    from .models import new_node
                    node = new_node(name=name, domain=domain, scope=scope)
                    node["topics"] = list(sug["contribution"].get("claims") or [])[:3]
                    state["nodes"].append(node)
                if proc_result and proc_result.get("ok"):
                    safe = _candidate_filename(proc_result["title"]) or "笔记"
                    note_name = f"{safe}-{sug['id'][-6:]}.md"
                else:
                    note_name = f"discovery-{sug['id']}.md"
                note_result = self._write_candidate_note(
                    sug, node, dict(edits, _intent_request_id=request_id), note_name,
                    proc_result=proc_result, admission=admission)
                note_relpath = note_result["path"]
                # The exact note payload is already journaled. A failed state
                # commit keeps both for recovery, including any human edit.
                link = make_note_link(node["id"], {"kind": "note_id", "note_id": note_result["note_id"],
                                                   "path": note_relpath})
                state["note_links"].append(link)
                if edits.get("request_id"):
                    state["idempotency"].setdefault(
                        f"discovery:{edits['request_id']}",
                        {"op": "discovery_accept", "at": now_iso, "result": {"suggestion_id": suggestion_id}},
                    )
                self.svc.store.save(state, base_version=state["version"])
                sug["status"] = "accepted"
                sug["accepted"] = {"node_id": node["id"], "note": note_name,
                                   "note_relpath": note_relpath, "at": now_iso,
                                   "request_id": request_id, "suggestion_note": note_name}
                if proc_result is not None:
                    sug["note_processing"] = {
                        "status": "ok", "title": proc_result.get("title"),
                        "skill_version": proc_result.get("skill_version"),
                        "skill_kind": proc_result.get("skill_kind"),
                        "calls": proc_result.get("calls"), "complete": proc_result.get("complete"),
                        "segments": proc_result.get("segments"), "ai_review": proc_result.get("ai_review")}
                sug["admission"] = dict(admission, at=now_iso)
                self._save_suggestions(suggestions)
                # Only close after both state and accepted suggestion are durable.
                self._save_intents([i for i in self._load_intents() if i["intent_id"] != intent_id])
                return {"verdict": "new_node", "node_id": node["id"], "status": node["status"],
                        "note": note_name, "layer": "vision", "published": True,
                        "note_status": "正式笔记（未测验；掌握以测验为准）"}

        if verdict == "supplement":
            node = self._target_node(state, sug, edits)
            entry = {
                "at": now_iso, "suggestion_id": suggestion_id,
                "source_url": sug["sources"][0]["url"] if sug["sources"] else None,
                "summary": edits.get("summary") or self._supplement_text(sug),
            }
            node.setdefault("supplement_suggestions", []).append(entry)
            node["updated_at"] = now_iso
            sug["status"] = "accepted"
            sug["accepted"] = {"node_id": node["id"], "kind": "supplement", "at": now_iso,
                               "request_id": request_id}
            self.svc.store.save(state, base_version=state["version"])
            self._save_suggestions(suggestions)
            return {"verdict": "supplement", "node_id": node["id"],
                    "note": "已追加资料关联建议；未改变节点范围、正文或掌握状态"}

        if verdict == "revision":
            node = self._target_node(state, sug, edits)
            proposal = {
                "at": now_iso, "suggestion_id": suggestion_id,
                "old_scope": node["scope"],
                "proposed_changes": sug["contribution"].get("claims") or [],
                "source_url": sug["sources"][0]["url"] if sug["sources"] else None,
                "status": "pending_review",
            }
            node.setdefault("update_proposals", []).append(proposal)
            node["updated_at"] = now_iso
            sug["status"] = "accepted"
            sug["accepted"] = {"node_id": node["id"], "kind": "revision", "at": now_iso,
                               "request_id": request_id}
            self.svc.store.save(state, base_version=state["version"])
            self._save_suggestions(suggestions)
            return {"verdict": "revision", "node_id": node["id"],
                    "note": "已保存更新提案（待审核）；未覆盖任何内容，未清除通过状态"}

        raise ValidationError(f"该建议类型（{verdict}）没有可接受的写入动作")

    def _target_node(self, state: dict, sug: dict, edits: dict) -> dict:
        node_id = edits.get("node_id") or (sug["related"][0]["node_id"] if sug["related"] else None)
        if not node_id:
            raise ValidationError("没有可关联的目标节点")
        node = next((n for n in state["nodes"] if n["id"] == node_id), None)
        if node is None:
            raise ValidationError(f"目标节点不存在：{node_id}")
        # 写入前版本检查（默认强制）：建议创建时快照的目标节点内容版本，
        # 与当前版本不一致 → 拒绝（旧提案不能静默覆盖变化后的知识）。
        expected = None
        for r in sug["related"]:
            if r["node_id"] == node_id:
                expected = r.get("content_version")
                break
        if expected is not None and int(expected) != node["content_version"]:
            raise ValidationError(
                f"目标节点内容版本已变化（现 v{node['content_version']}，提案基于 v{expected}）；请刷新后重新评估"
            )
        return node

    def _default_scope(self, sug: dict) -> str:
        c = sug["contribution"]
        parts = []
        if c.get("contribution_summary"):
            parts.append("来源声称：" + c["contribution_summary"])
        if c.get("conditions_limits"):
            parts.append("边界：" + c["conditions_limits"][0])
        return "；".join(parts) or sug["name"]

    def _supplement_text(self, sug: dict) -> str:
        c = sug["contribution"]
        if c.get("differences"):
            return "补充：" + c["differences"][0]
        if c.get("claims"):
            return "补充：" + c["claims"][0]
        return "补充来源：" + (sug["sources"][0]["url"] or "")

    def _formal_note_dir(self, domain: str) -> Path:
        """正式笔记落点：优先匹配库内已有领域目录（容忍编号前缀，如
        「AI技术领域」→「01-AI技术领域」），匹配不到落「待分类领域/」。"""
        root = self.svc.store.vault_root
        domain = (domain or "").strip()
        if domain:
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                if child.name.startswith((".", "_")) or child.name == "个人学习状态":
                    continue
                if "候选" in child.name or "正式笔记" == child.name:
                    continue
                stripped = re.sub(r"^\d+\s*[-_.、]?\s*", "", child.name)
                if domain == child.name or domain in child.name or domain == stripped:
                    return child / FORMAL_NOTE_DIR
        return root / "待分类领域" / FORMAL_NOTE_DIR

    def _remote_fetch_enabled(self) -> bool:
        """远程抓取开关：仅当发现配置显式开启时，上游候选才会抓取来源正文。"""
        section = self._discovery_state(self.svc.state())
        return bool((section.get("remote_fetch") or {}).get("enabled"))

    @staticmethod
    def _admission(proc_result):
        """机器质量门槛（免人工审核不豁免）。正式入库需同时满足：
        加工成功 + 内容完整（无缺节/无未处理分段）+ 取得原文全文。
        摘要/片段/仅元信息级材料只作线索——它们不能冒充完整正文。"""
        if proc_result is None:
            # 配置修复（设置 PKA_NOTE_ADAPTER 等）后即可重试：transient
            return {"publishable": False, "processing_status": "unconfigured",
                    "source_scope": "unknown", "outcome": "retry_transient",
                    "reason": "笔记加工器未配置：无完整正文加工能力，不产出正式笔记"}
        src = proc_result.get("source") or {}
        if src and (not src.get("obtained") or not src.get("body_usable", src.get("obtained"))
                    or src.get("scope") == "pdf_partial"
                    or (src.get("pdf") or {}).get("truncated")):
            return {"publishable": False, "processing_status": "source_incomplete",
                    "source_scope": src.get("scope", "unknown"), "outcome": "needs_source",
                    "reason": "来源正文缺失、不足或被截断，需补齐来源范围"}
        if not proc_result.get("ok"):
            # 超时/网络/5xx/预算耗尽为暂时故障；解析类失败也可能瞬时（截断修复等）
            outcome = "retry_transient"
            return {"publishable": False, "processing_status": "failed",
                    "source_scope": src.get("scope", "unknown"), "outcome": outcome,
                    "reason": f"加工失败：{proc_result.get('reason')}"}
        if not src.get("obtained"):
            # 材料不足类：原文补齐/开启 remote_fetch 后由人工重试入口处理
            return {"publishable": False, "processing_status": "ok",
                    "source_scope": src.get("scope", "summary_only"),
                    "outcome": "needs_source",
                    "reason": "未取得原文全文（摘要/片段级材料只作线索）"}
        if not src.get("body_usable", src.get("obtained")):
            # 来源本身结构性不足：非人工处理不可变
            return {"publishable": False, "processing_status": "ok",
                    "source_scope": src.get("scope", "summary_only"),
                    "outcome": "needs_source",
                    "reason": "来源文本不像完整正文（目录/摘要/元信息级），只作线索"}
        if not proc_result.get("complete"):
            # 部分处理：可能有暂时性跳段，按可重试处理（退避）
            return {"publishable": False, "processing_status": "partial",
                    "source_scope": src.get("scope", "full"), "outcome": "retry_transient",
                    "reason": "仅处理部分内容：" + (proc_result.get("processed_range") or ""),
                    "processed_range": proc_result.get("processed_range")}
        return {"publishable": True, "processing_status": "ok", "outcome": "ok",
                "source_scope": src.get("scope", "full"),
                "processed_range": proc_result.get("processed_range", "全部"),
                "skill_version": proc_result.get("skill_version")}

    @staticmethod
    def _content_fingerprint(sug):
        return (sug.get("content_sha256")
                or (sug.get("sources") or [{}])[0].get("fingerprint") or "")

    def _accept_as_clue_or_defer(self, suggestions, sug, admission, proc_result,
                                 now_iso, *, request_id, strict, edits):
        """未达正式准入的分流：
        - 自动路径（strict=True）：留队待重试；登记 blocked_fingerprint，
          内容不变则不反复烧预算——内容变化会产生新建议，自然获得新机会。
        - 人工路径：降级为候选目录里的线索笔记（status: candidate，不建节点、
          不进正式区、不改掌握状态），建议标记为已接受为线索。"""
        domain = ((edits or {}).get("domain") or "").strip()
        if not domain:
            for r in sug.get("related") or []:
                if r.get("domain"):
                    domain = str(r["domain"]).strip()
                    break
        domain = domain or "待分类领域"
        if strict:
            # 重试分级：transient（超时/网络/预算/部分处理）按退避自动重试；
            # needs_source（摘要/目录/元信息级）不自动重试，原文补齐或配置修复后
            # 由人工重新接受（accept 即重试入口）。
            outcome = admission.get("outcome", "retry_transient")
            fp = self._content_fingerprint(sug)
            prior = (sug.get("admission") or {})
            attempts = int(prior.get("attempts") or 0) + 1
            if outcome == "retry_transient":
                not_before = self._ts_after_now(60 * min(60, 2 ** min(attempts, 6)))
            else:
                not_before = None   # needs_source：不自动重试
            sug["admission"] = dict(admission, blocked_fingerprint=fp, at=now_iso,
                                    outcome=outcome, attempts=attempts,
                                    not_before=not_before)
            self._save_suggestions(suggestions)
            return {"published": False, "deferred": True, "admission": sug["admission"],
                    "suggestion_id": sug["id"],
                    "note_status": ("未达内容准入门槛（暂时性故障，将自动退避重试）"
                                    if outcome == "retry_transient" else
                                    "未达内容准入门槛（材料不足：原文补齐后可重新接受）")}
        # 人工路径：线索笔记 + 意图日志（幂等恢复）
        intent_id = f"clue-{sug['id']}"
        intents = self._load_intents()
        prior_intent = next((i for i in intents if i["intent_id"] == intent_id), None)
        clue = self._write_clue_note(sug, domain, admission, proc_result,
                                     prior=prior_intent)
        intents = self._load_intents()
        intents[:] = [i for i in intents if i["intent_id"] != intent_id]
        intents.append({
            "intent_id": intent_id, "kind": "clue", "suggestion_id": sug["id"],
            "clue_relpath": clue["relpath"],
            "clue_fingerprint": hashlib.sha256(
                clue["path_obj"].read_bytes()).hexdigest()[:16],
            "domain": domain, "note_id": clue["note_id"],
            "payload": clue["path_obj"].read_text(encoding="utf-8"),
            "stage": "clue_written", "at": now_iso,
        })
        self._save_intents(intents)
        sug["status"] = "accepted"
        sug["accepted"] = {"as": "clue", "note": clue["relpath"], "at": now_iso,
                           "request_id": request_id}
        if proc_result is not None:
            sug["note_processing"] = (
                {"status": "ok", "title": proc_result.get("title"),
                 "skill_version": proc_result.get("skill_version"),
                 "skill_kind": proc_result.get("skill_kind"),
                 "calls": proc_result.get("calls"), "complete": proc_result.get("complete"),
                 "segments": proc_result.get("segments")}
                if proc_result.get("ok")
                else {"status": "failed", "reason": proc_result.get("reason")})
        sug["admission"] = dict(admission, at=now_iso)
        self._save_suggestions(suggestions)
        # 建议已持久化：清除线索意图（崩溃恢复据此判断"仅差建议保存"）
        intents = [i for i in self._load_intents()
                   if i.get("intent_id") != f"clue-{sug['id']}"]
        self._save_intents(intents)
        return {"verdict": sug.get("verdict"), "published": False, "clue": clue["relpath"],
                "admission": admission, "suggestion_id": sug["id"],
                "note_status": "线索笔记（候选目录；未达正式准入，不建节点不改掌握状态）"}

    def _ts_after_now(self, seconds: int) -> str:
        from datetime import timedelta

        return (self.clock() + timedelta(seconds=seconds)).isoformat(timespec="seconds")

    def _write_clue_note(self, sug: dict, domain: str, admission: dict,
                         proc_result: dict | None, prior: dict | None = None) -> dict:
        """线索笔记：写入 {领域}/候选笔记/（status: candidate）。不建节点、不碰正式区。"""
        note_dir = self.svc.store.vault_root / CANDIDATE_NOTE_DIR / (domain or "待分类领域")
        base = _candidate_filename(sug.get("name") or "线索") or "线索"
        # 确定性命名（按建议 ID）：重试不会生成多个随机文件
        note_name = f"线索-{base}-{sug['id'][-6:]}.md"
        path = self.svc.store.safe_read_path(str(note_dir / note_name))
        if prior and prior.get("clue_relpath"):
            path = self.svc.store.safe_read_path(prior["clue_relpath"])
        # 幂等：崩溃/重试留下的同名文件，指纹一致则直接复用（不重复创建）
        if prior and path.exists():
            current = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            if current == prior.get("clue_fingerprint"):
                return {"path_obj": path, "note_id": prior.get("note_id") or "note-clue",
                        "relpath": path.relative_to(self.svc.store.vault_root).as_posix(),
                        "reused": True}
        # 文件已存在但与本次意图无关（人工文件或指纹不一致）→ 明确拒绝，不覆盖
        if path.exists():
            raise ValidationError(
                f"线索笔记已存在且非本次流程创建：{note_name}（不覆盖，请人工处理）")
        note_id = "note-clue-" + hashlib.sha256(sug["id"].encode("utf-8")).hexdigest()[:10]
        src_list = sug.get("sources") or [{"source_name": "（无来源）", "url": "",
                                           "read_status": "unknown"}]
        src_lines = "\n".join(
            f"- [{s.get('source_name') or s.get('label')}]({s.get('url') or s.get('path') or ''})"
            f"（{s.get('read_status')}）" for s in src_list)
        proc_ok = bool(proc_result and proc_result.get("ok"))
        if proc_ok:
            content = proc_result.get("body") or ""
            head = f"# {sug.get('name', '线索')}（线索笔记，部分处理）"
        else:
            claims = "\n".join(
                f"- {c}" for c in ((sug.get("contribution") or {}).get("claims")
                                   or ["（无明确机制表述）"]))
            content = ("## 来源声称的知识点（未核实）\n\n" + claims +
                       "\n\n## 未达正式准入的原因\n\n" +
                       str(admission.get("reason", "（未知）")))
            head = f"# {sug.get('name', '线索')}（线索笔记）"
        body = (f"---\nid: {note_id}\nstatus: candidate\n"
                f"title: \"{_fm_escape(sug.get('name'))}\"\n"
                f"source_type: \"discovery\"\n"
                f"domain: \"{_fm_escape(domain)}\"\n"
                f"created_at: \"{self.clock().date().isoformat()}\"\n"
                f"suggestion_id: \"{sug['id']}\"\n"
                f"admission_status: \"{admission.get('processing_status', 'unknown')}\"\n"
                f"source_scope: \"{admission.get('source_scope', 'unknown')}\"\n"
                f"---\n\n"
                f"{head}\n\n"
                f"> 本条未通过内容准入（{_fm_escape(admission.get('reason'))}），仅作线索保留；\n"
                f"> 不建节点、不进正式区，不代表掌握。补齐原文或修复加工后可重新处理。\n\n"
                f"{content}\n\n## 来源\n\n{src_lines}\n")
        if prior and prior.get("payload") is not None:
            body = prior["payload"]
            note_id = prior.get("note_id") or note_id
        relpath = path.relative_to(self.svc.store.vault_root).as_posix()
        intent_id = f"clue-{sug['id']}"
        intents = [i for i in self._load_intents() if i["intent_id"] != intent_id]
        intents.append({"intent_id": intent_id, "kind": "clue", "suggestion_id": sug["id"],
                        "clue_relpath": relpath, "domain": domain, "note_id": note_id,
                        "payload": body,
                        "clue_fingerprint": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
                        "stage": "prepared", "at": self.clock().isoformat(timespec="seconds")})
        self._save_intents(intents)
        self.svc.store.safe_write_path(relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{note_name}.tmp-{secrets.token_hex(4)}")
        try:
            tmp.write_bytes(body.encode("utf-8"))
            os.link(tmp, path)  # 发布完整文件，目标已存在时原子拒绝，不覆盖人工内容
        finally:
            tmp.unlink(missing_ok=True)
        return {"path_obj": path, "note_id": note_id,
                "relpath": relpath}

    def _write_candidate_note(self, sug: dict, node: dict, edits: dict, note_name: str,
                              proc_result: dict | None = None,
                              admission: dict | None = None) -> dict:
        """生成正式入库笔记（单动作直推；frontmatter 遵循统一笔记模型的受限格式）。

        2026-10-03 起用户取消逐条审核：接受建议即一步写入 `{领域}/正式笔记/`
        （status: accepted）。节点仍为 untested——内容入库不等于掌握，掌握以测验为准。
        配置了笔记加工器且成功时正文用加工产出（遵循当日 Skill，记录版本）；
        加工失败/未配置回退确定性提取模板并如实标注。
        """
        domain_dir = node.get("domain", "").strip() or "待分类领域"
        note_dir = self._formal_note_dir(domain_dir)
        self.svc.store.safe_write_path(str(note_dir / note_name))
        path = note_dir / note_name
        path.parent.mkdir(parents=True, exist_ok=True)
        note_id = "note-" + uuid.uuid4().hex[:4] + secrets.token_hex(2)
        c = sug["contribution"]
        src_list = sug["sources"] or [{"source_name": "（无来源）", "url": "", "read_status": "unknown"}]
        src_lines = "\n".join(
            f"- [{s.get('source_name') or s.get('label')}]({s.get('url') or s.get('path') or ''})"
            f"（{s.get('read_status')}）"
            for s in src_list
        )
        proc_ok = bool(proc_result and proc_result.get("ok"))
        if proc_ok:
            processor_version = f"note-skill-{proc_result.get('skill_version')}"
            head = f"# {node['name']}"
            extra_fm = (f'note_skill: "{_fm_escape(proc_result.get("skill_kind"))}'
                        f'/{_fm_escape(proc_result.get("skill_version"))}"\n'
                        f'original_title: "{_fm_escape(sug["name"])}"\n'
                        f'note_complete: {"true" if proc_result.get("complete") else "false"}\n'
                        f'note_calls: {int(proc_result.get("calls") or 0)}\n')
            proc_notice = ""
            content = proc_result["body"]
        else:
            processor_version = "pka-discovery-1"
            head = f"# {node['name']}（自动入库，未测验）"
            extra_fm = ""
            claims = "\n".join(f"- {claim}" for claim in (c.get("claims") or ["（材料未给出明确机制）"]))
            proc_notice = ""
            if proc_result is not None:
                proc_notice = (f"> ⚠ 自动加工未完成（{_fm_escape(proc_result.get('reason') or '未知原因')}），"
                               f"以下为确定性提取模板，质量 deterministic_shallow。\n\n")
            else:
                proc_notice = ("> 本笔记由知识发现建议生成（确定性提取，质量 deterministic_shallow）。\n"
                               "> 需人工审核后才能发布为正式笔记；接受建议不等于掌握。\n\n")
            content = (f"## 知识贡献（来源声称）\n\n{claims}\n\n"
                       f"## 适用条件与限制\n\n"
                       f"{chr(10).join('- ' + x for x in (c.get('conditions_limits') or ['（材料未明示）']))}")
        body = f"""---
id: {note_id}
status: accepted
title: "{_fm_escape(node['name'])}"
source_type: "discovery"
source_uri: "{(sug.get('sources') or [{}])[0].get('url') or (sug.get('sources') or [{}])[0].get('path') or ''}"
domain: "{_fm_escape(node['domain'])}"
created_at: "{self.clock().date().isoformat()}"
processor_version: "{processor_version}"
review_status: "{'passed' if (proc_result or {}).get('ai_review', {}).get('verdict') == 'pass' else 'not_run'}"
suggestion_id: "{sug['id']}"
related_notes: []
source_scope: "{_fm_escape((admission or {}).get('source_scope', 'unknown'))}"
processed_range: "{_fm_escape((admission or {}).get('processed_range', '未知'))}"
{extra_fm}---

{head}

{proc_notice}{content}

## 来源

{src_lines}
"""
        payload = body
        relpath = path.relative_to(self.svc.store.vault_root).as_posix()
        intent_id = f"intent-{sug['id']}"
        at = self.clock().isoformat(timespec="seconds")
        note_processing = None
        if proc_result is not None:
            note_processing = {
                "status": "ok" if proc_result.get("ok") else "failed",
                "title": proc_result.get("title"), "skill_version": proc_result.get("skill_version"),
                "skill_kind": proc_result.get("skill_kind"), "calls": proc_result.get("calls"),
                "complete": proc_result.get("complete"), "segments": proc_result.get("segments"),
                "ai_review": proc_result.get("ai_review")}
        with self._commit_guard():
            self._load_suggestions()  # a damaged queue must also block direct trusted writes
            intents = self._load_intents()
            if any(item["intent_id"] == intent_id for item in intents):
                raise ValidationError("该建议有未完成的入库意图，请先恢复；未覆盖原笔记")
            fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            intent = {
                "intent_id": intent_id, "suggestion_id": sug["id"], "kind": "new_node",
                "node": dict(node), "note_name": note_name, "note_relpath": relpath,
                "note_id": note_id, "note_fingerprint": fingerprint[:16],
                "note_sha256": fingerprint, "note_payload": payload,
                "stage": "prepared", "at": at,
                "accepted": {"node_id": node["id"], "note": note_name, "note_relpath": relpath,
                             "at": at, "request_id": edits.get("_intent_request_id"),
                             "suggestion_note": note_name},
                "admission": dict(admission or {}, at=at), "note_processing": note_processing,
            }
            intents.append(intent)
            self._save_intents(intents)  # journal the exact bytes before publishing any file
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{note_name}.tmp-{uuid.uuid4().hex}")
            try:
                with tmp.open("xb") as handle:
                    handle.write(payload.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.link(tmp, path)  # atomic create-only; an existing file is never replaced
            finally:
                tmp.unlink(missing_ok=True)
            intent["stage"] = "note_written"
            self._save_intents(intents)
        return {"path_obj": path, "note_id": note_id, "path": relpath, "created": True}

    # ---------- 稍后 / 忽略 ----------

    def set_status(self, suggestion_id: str, status: str) -> dict:
        if status not in ("later", "dismissed"):
            raise ValidationError(f"非法建议状态：{status!r}")
        suggestions = self._load_suggestions()
        sug = next((s for s in suggestions if s["id"] == suggestion_id), None)
        if sug is None:
            raise NotFound("建议", suggestion_id)
        if sug["status"] != "pending":
            raise ValidationError(f"建议当前状态为 {sug['status']!r}")
        sug["status"] = status
        self._save_suggestions(suggestions)
        return {"id": suggestion_id, "status": status}
