"""共享测试夹具：在临时目录构造测试知识库。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))

FORMAL_NOTE = """---
id: note-20260926-001
status: accepted
title: "Unit 1：为什么需要 Harness"
source_type: "pdf"
source_uri: "01-AI技术领域/资料/Harness_Unit1_Tutorial.pdf"
domain: "AI技术领域"
created_at: "2026-09-26"
---

# Unit 1：为什么需要 Harness

（复制自真实知识库的测试副本，正文略。）
"""

CANDIDATE_NOTE = """---
id: note-20260924-001
status: candidate
title: "课程知识地图"
domain: "AI技术领域"
created_at: "2026-09-24"
evidence_ids:
  - "Harness_Unit0_Tutorial.pdf#page=1"
related_notes:
  - "从提问到可靠交付的五层演进"
---

# 课程知识地图

（复制自真实知识库的测试副本，正文略。）
"""

NO_ID_NOTE = """---
title: "提示词工程的基本手法（测试用无 ID 笔记）"
domain: "AI技术领域"
---

# 提示词工程的基本手法

测试数据：模拟没有稳定 ID 的知识笔记。
"""

DUP_A = """---
id: note-20260101-999
status: candidate
title: "重复 ID 测试甲：上下文窗口管理"
domain: "AI技术领域"
---

甲：测试数据，与乙共用 ID。
"""

DUP_B = """---
id: note-20260101-999
status: candidate
title: "重复 ID 测试乙：上下文窗口的截断策略"
domain: "AI技术领域"
---

乙：测试数据，与甲共用 ID。
"""


def make_test_vault(root: Path) -> Path:
    """构造与 test_vault/ 同构的临时测试库。"""
    root = Path(root)
    (root / "正式笔记").mkdir(parents=True, exist_ok=True)
    (root / "候选笔记").mkdir(parents=True, exist_ok=True)
    (root / "个人学习状态").mkdir(parents=True, exist_ok=True)
    (root / "正式笔记" / "04-Unit1.md").write_text(FORMAL_NOTE, encoding="utf-8")
    (root / "候选笔记" / "00-知识地图.md").write_text(CANDIDATE_NOTE, encoding="utf-8")
    (root / "候选笔记" / "无ID笔记.md").write_text(NO_ID_NOTE, encoding="utf-8")
    (root / "候选笔记" / "重复ID甲.md").write_text(DUP_A, encoding="utf-8")
    (root / "候选笔记" / "重复ID乙.md").write_text(DUP_B, encoding="utf-8")
    return root


def copy_repo_test_vault(root: Path) -> Path:
    """把仓库内 test_vault/ 复制到临时目录（用于端到端/演示一致性测试）。"""
    shutil.copytree(WORKSPACE / "test_vault", root, dirs_exist_ok=True)
    return root
