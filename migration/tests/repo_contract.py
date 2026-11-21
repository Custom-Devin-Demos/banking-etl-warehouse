"""Helpers for the repo-level offline contract checks (PLAN.md §3/§4). No Snowflake access."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "migration"
PLAN = MIGRATION / "PLAN.md"
README = MIGRATION / "README.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "snowflake-migration.yml"
SNOWFLAKE_SQL_ROOT = MIGRATION / "snowflake"

BINARY_SUFFIXES = {".xlsx", ".xls", ".bak", ".zip", ".pdf", ".pyc", ".png", ".jpg", ".gif", ".parquet"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}


def plan_section(number: int) -> str:
    """Return the body of `## <number>. ...` in PLAN.md."""
    text = PLAN.read_text(encoding="utf-8")
    match = re.search(rf"^## {number}\. .*?(?=^## \d+\. |\Z)", text, flags=re.M | re.S)
    if not match:
        raise AssertionError(f"PLAN.md has no section {number}")
    return match.group(0)


def plan_layout() -> list[str]:
    """Repo-relative paths from the PLAN.md §4 layout tree; directories end with '/'."""
    block = re.search(r"```\n(.*?)```", plan_section(4), flags=re.S)
    if not block:
        raise AssertionError("PLAN.md §4 has no layout code block")
    paths: list[str] = []
    stack: list[tuple[int, str]] = []
    for raw in block.group(1).splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else ""
        for name in (n.strip() for n in line.split(",")):
            paths.append(parent + name)
        if line.strip().endswith("/") and "," not in line:
            stack.append((indent, parent + line.strip()))
    return paths


def plan_env_vars() -> list[str]:
    return sorted(set(re.findall(r"\bSNOWFLAKE_[A-Z_]+\b", plan_section(3))))


def plan_sqlfluff_pin() -> str:
    match = re.search(r"sqlfluff==([\d.]+)", plan_section(3))
    if not match:
        raise AssertionError("PLAN.md §3 does not pin sqlfluff")
    return match.group(1)


def sqlfluff_ignore_patterns() -> list[str]:
    ignore = REPO_ROOT / ".sqlfluffignore"
    lines = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def is_sqlfluff_ignored(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    for pattern in sqlfluff_ignore_patterns():
        pat = pattern.lstrip("/")
        if pat.endswith("/") and (rel + "/").startswith(pat):
            return True
        if fnmatch.fnmatch(rel, pat) or rel.startswith(pat.rstrip("/") + "/"):
            return True
    return False


def _walk(root: Path):
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not (SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts)):
            yield path


def migration_sql_files() -> list[Path]:
    """All committed Snowflake SQL under migration/ that sqlfluff lints (legacy baseline T-SQL excluded)."""
    return [p for p in _walk(MIGRATION) if p.suffix.lower() == ".sql" and not is_sqlfluff_ignored(p)]


def snowflake_sql_files() -> list[Path]:
    return [p for p in migration_sql_files() if SNOWFLAKE_SQL_ROOT in p.parents]


def scanned_text_files() -> list[Path]:
    """Text files scanned for credentials: everything under migration/ and .github/."""
    files = list(_walk(MIGRATION)) + list(_walk(REPO_ROOT / ".github"))
    return [p for p in files if p.suffix.lower() not in BINARY_SUFFIXES]


_NON_SQL_LANG = re.compile(r"\bLANGUAGE\s+(PYTHON|JAVASCRIPT|JAVA|SCALA)\b", re.I)


def strip_sql(sql: str) -> str:
    """Return SQL code with comments and string literals blanked (newlines kept).

    `$$ ... $$` bodies of Snowflake Scripting procedures are scanned as SQL; bodies of
    Python/JavaScript/Java/Scala procedures are blanked because they are not SQL.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    statement_start = 0

    def blank(segment: str) -> str:
        return re.sub(r"[^\n]", " ", segment)

    while i < n:
        ch, nxt = sql[i], sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-" or ch == "/" and nxt == "/":
            end = sql.find("\n", i)
            end = n if end == -1 else end
            out.append(blank(sql[i:end]))
            i = end
        elif ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(blank(sql[i:end]))
            i = end
        elif ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            out.append("'" + blank(sql[i + 1 : end - 1]) + "'" if end - i >= 2 else blank(sql[i:end]))
            i = end
        elif ch == "$" and nxt == "$":
            end = sql.find("$$", i + 2)
            end = n if end == -1 else end
            body = sql[i + 2 : end]
            if _NON_SQL_LANG.search(sql[statement_start:i]):
                out.append("$$" + blank(body))
            else:
                out.append("$$" + strip_sql(body))
            if end < n:
                out.append("$$")
            i = end + 2
        else:
            if ch == ";":
                statement_start = i + 1
            out.append(ch)
            i += 1
    return "".join(out)


def first_code_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def github_slug(heading: str) -> str:
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\- ]", "", slug)
    return slug.replace(" ", "-")


def markdown_links(text: str) -> list[str]:
    """Link targets of inline Markdown links, ignoring fenced code blocks."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", text)
