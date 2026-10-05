"""安全存储：单文件权威状态 + 原子替换 + 乐观锁 + 可恢复备份 + 路径硬限制。

设计（与当前 16 篇知识笔记的规模相称，刻意保持最小）：
- 权威状态是测试知识库"个人学习状态/state.json"一个文件。所有节点操作在内存中
  完成整体验证后一次性原子替换，因此合并/拆分等多节点变更天然没有"半完成"状态。
- 原子写：先写同目录临时文件并 fsync，再 os.replace 覆盖；中断只可能留下
  临时文件，state.json 要么是旧的、要么是新的。
- 乐观锁：调用方携带 base_version，与服务端当前 version 不一致即拒绝（写冲突）。
- 备份：每次成功写入前把旧文件复制到 backups/state-v{N}-{时间}.json，保留最近 20 份。
- 路径硬限制：所有写路径必须 resolve 后位于测试知识库根之内；真实知识库路径
  前缀被显式拉黑，即使配置错误也拒绝写入。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from .models import ValidationError, empty_state, now_iso, validate_state
from .file_lock import FileMutex

# 真实知识库路径由环境变量 PKA_REAL_VAULT 显式提供（公开版不含任何私人路径）。
# 未设置时"真实库受控写"功能整体关闭，所有库路径按普通库处理。
REAL_VAULT_PREFIX = (Path(os.environ["PKA_REAL_VAULT"].strip())
                     if os.environ.get("PKA_REAL_VAULT", "").strip() else None)
KEEP_BACKUPS = 20


class VersionConflict(RuntimeError):
    """乐观锁冲突：base_version 落后于服务端当前版本。"""

    def __init__(self, base_version: int, current_version: int):
        super().__init__(f"版本冲突：请求基于 v{base_version}，服务端已是 v{current_version}")
        self.base_version = base_version
        self.current_version = current_version


class PathOutsideVault(RuntimeError):
    """写入路径越出测试知识库（或命中真实知识库黑名单）。"""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class StateStore:
    def __init__(self, vault_root: Path, state_relpath: str = "个人学习状态/state.json",
                 allow_real_vault: bool = False):
        """allow_real_vault：受控的真实知识库写入开关。

        默认 False —— 真实库路径一律拒绝（含"配置错误指向真实库"的双保险）。
        显式传 True 时（经用户批准的真实库接入）才允许；即便如此，写入仍被
        _ensure_within 限制在 vault_root 之内，且状态文件固定落在
        "个人学习状态/" 管理子目录，绝不触碰笔记文件本身。
        """
        self.vault_root = Path(vault_root).resolve()
        self.allow_real_vault = bool(allow_real_vault)
        self.state_path = (self.vault_root / state_relpath).resolve()
        self.backup_dir = (self.state_path.parent / "backups").resolve()
        self._mutex = FileMutex(self.state_path.with_name(self.state_path.name + ".lock"))
        self._real_vault_is_target = (
            REAL_VAULT_PREFIX is not None
            and (_is_within(self.vault_root, REAL_VAULT_PREFIX)
                 or self.vault_root == REAL_VAULT_PREFIX))
        if self._real_vault_is_target and not allow_real_vault:
            raise PathOutsideVault(
                "目标是真实知识库，但未开启受控写入（allow_real_vault/--allow-real-vault）。"
                "如需接入，请使用显式开关并确认变更清单。")
        self._ensure_within(self.state_path)

    # ---------- 路径限制 ----------

    def _ensure_within(self, path: Path) -> Path:
        resolved = Path(path).resolve()
        if not _is_within(resolved, self.vault_root):
            raise PathOutsideVault(f"路径越出知识库根：{resolved}")
        # 真实库前缀防护保留：仅在"目标不是真实库"时生效（防止 test 模式下
        # 路径穿越写到真实库）；目标本身就是真实库时由构造开关负责把关。
        if (not self._real_vault_is_target and REAL_VAULT_PREFIX is not None
                and _is_within(resolved, REAL_VAULT_PREFIX)):
            raise PathOutsideVault(f"禁止访问真实知识库路径：{resolved}")
        return resolved

    def safe_write_path(self, relpath: str) -> Path:
        """把相对路径解析为知识库内绝对路径；越界抛 PathOutsideVault。

        真实库模式下允许的写入范围（用户批准的受控扩展，2026-10-03）：
        - 管理区（个人学习状态/）全部；
        - 「正式笔记/」「候选笔记/」子树——知识入库落点；仅允许**新建**文件，
          绝不覆盖该区域内任何已存在文件（人工内容受保护）。
        笔记 frontmatter 的状态镜像走 mirror.py 的独立受控通道，不经此函数。
        """
        candidate = (self.vault_root / relpath).resolve()
        resolved = self._ensure_within(candidate)
        if self._real_vault_is_target:
            state_dir = self.state_path.parent
            if not _is_within(resolved, state_dir):
                # 入库落点 = 任意层级下名为「正式笔记」「候选笔记」的目录子树
                rel_parts = resolved.relative_to(self.vault_root).parts
                allowed = any(part in ("正式笔记", "候选笔记") for part in rel_parts[:-1])
                if not allowed:
                    raise PathOutsideVault(
                        f"真实库模式：仅允许写入管理区与「正式笔记/」「候选笔记/」目录，拒绝 {relpath}")
                if resolved.exists():
                    raise PathOutsideVault(
                        f"真实库模式：拒绝覆盖已有笔记文件 {relpath}（人工内容受保护）")
        return resolved

    def safe_read_path(self, relpath: str) -> Path:
        """安全读取入口：只做路径校验，绝不创建目录或写文件。

        与 safe_write_path 的区别（读写分离，2026-10-03 审查修复）：
        - 校验范围 = 解析后在知识库根内（含真实库本身；test 模式下真实库前缀
          仍被 _ensure_within 拉黑，防穿越读）；
        - 不含任何写保护（新增不覆盖、白名单目录）——「不能覆盖既有笔记」
          是写侧规则，不应拦截读侧；
        - 不检查文件是否存在：由调用方按"缺失/身份冲突"等业务语义处理。
        """
        candidate = (self.vault_root / relpath).resolve()
        return self._ensure_within(candidate)

    # ---------- 读取 ----------

    def exists(self) -> bool:
        return self.state_path.exists()

    def load(self) -> dict:
        with self._mutex:
            return self._load_locked()

    def _load_locked(self) -> dict:
        if not self.state_path.exists():
            state = empty_state()
            self._atomic_write(state)
            return state
        text = self.state_path.read_text(encoding="utf-8")
        try:
            state = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"状态文件损坏（JSON 解析失败）：{exc}；可用 backups 恢复") from exc
        validate_state(state)
        return state

    # ---------- 写入 ----------

    def save(self, state: dict, *, base_version: int | None = None, backup: bool = True) -> dict:
        """校验 → 乐观锁（对比磁盘当前版本）→ 备份 → 原子替换。返回写后的新状态。"""
        validate_state(state)
        with self._mutex:
            current_version = self._disk_version()
            if base_version is not None and base_version != current_version:
                raise VersionConflict(base_version, current_version)
            if backup and self.state_path.exists():
                self._backup_current()
            state["version"] = current_version + 1
            state["updated_at"] = now_iso()
            self._atomic_write(state)
        return state

    def _disk_version(self) -> int:
        if not self.state_path.exists():
            return 0
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            validate_state(state)
            return int(state.get("version", 0))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValidationError(
                f"状态文件损坏（version 无法读取），拒绝覆盖写入；请先 recover 或 restore_latest_backup：{exc}"
            ) from exc

    def _backup_current(self) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        current_version = json.loads(self.state_path.read_text(encoding="utf-8")).get("version", 0)
        target = self.backup_dir / f"state-v{current_version}.json"
        shutil.copy2(self.state_path, target)
        backups = self._sorted_backups()
        for old in backups[: max(0, len(backups) - KEEP_BACKUPS)]:
            old.unlink(missing_ok=True)
        return target

    def _atomic_write(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".{self.state_path.name}.tmp-{uuid.uuid4().hex}")
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        with open(tmp, "x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.state_path)  # 失败时保留任务自有临时文件供显式 recover

    # ---------- 恢复 ----------

    def pending_tmp_files(self) -> list[Path]:
        return sorted(self.state_path.parent.glob(f".{self.state_path.name}.tmp-*"))

    def recover(self) -> dict:
        """中断/损坏后恢复。原则：不覆盖唯一的损坏原件（改名保留）；明确报告恢复来源、
        备份时间并警示"备份之后的写入不能视为已恢复"；只应由人工显式调用。"""
        with self._mutex:
            return self._recover_locked()

    def _recover_locked(self) -> dict:
        removed = []
        for tmp in self.pending_tmp_files():
            tmp.unlink(missing_ok=True)
            removed.append(tmp.name)
        repaired_from = None
        repaired_at = None
        corrupt_name = None
        try:
            self._load_locked()
        except ValidationError:
            backups = self._sorted_backups()
            if not backups:
                raise
            latest = backups[-1]
            try:
                restored = json.loads(latest.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValidationError("最新备份损坏，保留状态原件并停止恢复") from exc
            validate_state(restored)
            # 不覆盖唯一的损坏原件：改名保留，供人工核对备份之后的写入是否需要找回
            stamp = now_iso().replace(":", "").replace("-", "").replace("+", "p")
            corrupt_original = self.state_path.with_name(f"state.corrupt-{stamp}-{uuid.uuid4().hex}.json")
            shutil.move(str(self.state_path), corrupt_original)
            self._atomic_write(restored)
            self._load_locked()  # 还原后必须可解析
            repaired_from = latest.name
            repaired_at = datetime.fromtimestamp(latest.stat().st_mtime).isoformat(timespec="seconds")
            corrupt_name = corrupt_original.name
        return {
            "removed_tmp": removed,
            "repaired_from": repaired_from,
            "repaired_at": repaired_at,
            "corrupt_original": corrupt_name,
            "warning": (
                (
                    f"已从备份 {repaired_from}（备份时间 {repaired_at}）恢复；"
                    f"损坏原件保留为 {corrupt_name} 供人工核对。"
                    f"该备份之后、损坏之前的写入不在备份中——不能视为最新数据已全部恢复，"
                    f"请对照损坏原件确认是否需要手工找回。"
                )
                if repaired_from
                else None
            ),
            "state_ok": True,
        }

    def list_backups(self) -> list[str]:
        with self._mutex:
            return [p.name for p in self._sorted_backups()]

    def _sorted_backups(self) -> list[Path]:
        def key(path):
            match = re.match(r"state-v(\d+)(?:-|\.)", path.name)
            return (int(match.group(1)) if match else -1, path.name)
        return sorted(self.backup_dir.glob("state-v*.json"), key=key)

    def restore_latest_backup(self) -> str:
        with self._mutex:
            backups = self._sorted_backups()
            if not backups:
                raise ValidationError("没有可用备份")
            latest = backups[-1]
            try:
                restored = json.loads(latest.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValidationError("备份文件损坏，拒绝覆盖状态") from exc
            validate_state(restored)
            self._atomic_write(restored)
            return latest.name
