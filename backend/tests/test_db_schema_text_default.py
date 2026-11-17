"""MySQL 8 不接受 TEXT / BLOB / JSON 列的 DEFAULT —— 写错就是「面板起不来」。

真实的翻车记录：给 ``ai_sessions`` 加分段耗时列时写成了

    metrics TEXT NOT NULL DEFAULT ''

MariaDB 允许（本地和容器里一路绿灯），MySQL 8 直接报
``(1101, "BLOB, TEXT, GEOMETRY or JSON column 'metrics' can't have a default value")``。
而这条 ALTER 是在 ``lifespan`` 里跑的，于是升级后 systemd 反复重启、
页面完全打不开 —— 一个「顺手加个字段」的改动把整个面板挡在门外。

这里把规则钉成测试：**扫描 backend/app 下的 DDL 文本**，任何 TEXT/BLOB/JSON
列都不许带 DEFAULT。允许 NULL，需要默认值就由 SQL 里的显式取值或读取处的
``or ""`` 兜底（VARCHAR 不在此列，它本来就可以有默认值）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

APP_DIR = Path(__file__).resolve().parent.parent / "app"

#: 这几类列在 MySQL 8 上不能带 DEFAULT（MariaDB 可以，所以单测挡不住）
TEXT_TYPES = (
    "TINYTEXT",
    "MEDIUMTEXT",
    "LONGTEXT",
    "TEXT",
    "TINYBLOB",
    "MEDIUMBLOB",
    "LONGBLOB",
    "BLOB",
    "JSON",
    "GEOMETRY",
)

PATTERN = re.compile(
    r"\b(?:" + "|".join(TEXT_TYPES) + r")\b[^,;\n]*?\bDEFAULT\b",
    re.IGNORECASE,
)

#: 只在真正的 DDL 语句里找：Python 代码里 `text = ... or default` 这种赋值
#: 也含 TEXT / DEFAULT 两个词，扫全文会误报。
DDL_STATEMENT = re.compile(
    r"\b(?:CREATE\s+TABLE|ALTER\s+TABLE)\b[^;]*", re.IGNORECASE | re.DOTALL
)


def _strip_comments(text: str) -> str:
    """去掉注释再扫：注释里出现「TEXT 不能带 DEFAULT」不该算违规。"""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"(?m)^\s*#.*$", " ", text)
    return text


def test_no_text_column_has_a_default() -> None:
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        text = _strip_comments(path.read_text(encoding="utf-8"))
        for statement in DDL_STATEMENT.finditer(text):
            for match in PATTERN.finditer(statement.group(0)):
                line = text.count("\n", 0, statement.start() + match.start()) + 1
                column = " ".join(match.group(0).split())
                offenders.append(f"{path.relative_to(APP_DIR.parent)}:{line}: {column}")
    assert not offenders, (
        "MySQL 8 不允许 TEXT / BLOB / JSON 列带 DEFAULT，请改成允许 NULL：\n"
        + "\n".join(offenders)
    )


def test_the_scanner_actually_catches_the_regression() -> None:
    """自检：这条正是当初把面板挡在门外的写法，扫描器必须能抓到。"""
    bad = "ALTER TABLE ai_sessions ADD COLUMN metrics TEXT NOT NULL DEFAULT ''"
    assert PATTERN.search(_strip_comments(bad))

    # 允许的写法都不该被误报
    for good in (
        "metrics TEXT",
        "metrics MEDIUMTEXT NULL",
        "command VARCHAR(1024) NOT NULL DEFAULT ''",
        "ok TINYINT DEFAULT NULL",
    ):
        assert not PATTERN.search(good), good


def test_aiaudit_schema_is_mysql_safe() -> None:
    from app import aiaudit

    assert not PATTERN.search(_strip_comments(aiaudit.SCHEMA))
