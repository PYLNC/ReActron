"""上传 GitHub 前的敏感信息扫描脚本。"""

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "data", "__pycache__", ".venv", "node_modules"}

PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "DeepSeek/OpenAI API Key"),
    (re.compile(r"-----BEGIN [A-Z0-9 ]+PRIVATE KEY-----"), "私钥"),
]

ENV_KEY_PATTERN = re.compile(r"DEEPSEEK_API_KEY\s*=\s*(.+)")
PLACEHOLDER_VALUES = {"", "sk-你的密钥", "your-api-key", "sk-xxx", "your_key_here"}


def scan():
    issues = []

    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(ROOT)

        if path.name == ".secret_key":
            issues.append((str(rel), "存在 .secret_key 文件，禁止提交"))
            continue
        if path.suffix in {".db", ".sqlite", ".sqlite3"} or path.name.startswith("countboter.db"):
            issues.append((str(rel), "存在 SQLite 数据库文件，禁止提交"))
            continue
        if path.suffix == ".log":
            issues.append((str(rel), "存在日志文件，禁止提交"))
            continue
        if path.suffix in {".pyc", ".pyo"}:
            issues.append((str(rel), "存在 Python 缓存文件，禁止提交"))
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for pattern, label in PATTERNS:
            if pattern.search(content):
                issues.append((str(rel), f"疑似包含 {label}"))

        for line in content.splitlines():
            match = ENV_KEY_PATTERN.match(line.strip())
            if match:
                value = match.group(1).strip().strip('"').strip("'")
                if value not in PLACEHOLDER_VALUES:
                    issues.append((str(rel), "DEEPSEEK_API_KEY 不是占位符"))

    return issues


def main():
    issues = scan()
    if not issues:
        print("[OK] 未发现敏感信息")
        return 0

    print("[WARN] 发现以下敏感信息，请处理后不要提交：")
    for path, reason in issues:
        print(f"  - {path}: {reason}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
