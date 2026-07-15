#!/usr/bin/env python3
"""把 integration/openclaw/config/AGENTS.md 烘成 agentsTemplate.generated.ts。

source of truth：integration/openclaw/config/AGENTS.md
output：integration/openclaw/memory-plugin/src/memory/agentsTemplate.generated.ts

CI 看护：generated 文件必须与最新 source 一致；不一致则 fail，提示运行
``python3 _codegen_agents_template.py`` 重新生成。

设计取舍：用 JSON.stringify 处理所有转义（含反引号、反斜杠、控制字符），
无需手动维护 escape 表。
"""

import json
import os
import sys

SOURCE_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "..", "..", "config", "AGENTS.md",
    )
)
OUTPUT_PATH = os.path.join(
    os.path.dirname(__file__),
    "agentsTemplate.generated.ts",
)


def buildOutput(content: str) -> str:
    """把 markdown 模板内容烘成 TS 模块字符串（含 generated 头注释）。"""
    encoded = json.dumps(content, ensure_ascii=False)
    lines = [
        "/* GENERATED FILE — DO NOT EDIT BY HAND",
        " * Source : integration/openclaw/config/AGENTS.md",
        " * Tool   : src/memory/_codegen_agents_template.py",
        " *",
        " * 重新生成：",
        " *   python3 src/memory/_codegen_agents_template.py",
        " */",
        "",
        "/** openclaw 测试用 AGENTS.md 完整模板（无受管 wrapper）。 */",
        f"export const OPENCLAW_AGENTS_TEMPLATE: string = {encoded};",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    """CLI 入口：默认重写 generated 文件；--check 只比对不写盘。"""
    with open(SOURCE_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    output = buildOutput(content)

    if "--check" in sys.argv:
        existing = ""
        if os.path.exists(OUTPUT_PATH):
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                existing = f.read()
        if existing != output:
            print(
                "ERROR: agentsTemplate.generated.ts is out of sync.\n"
                "  Run: python3 "
                "integration/openclaw/memory-plugin/src/memory/"
                "_codegen_agents_template.py",
                file=sys.stderr,
            )
            return 1
        print("OK: agentsTemplate.generated.ts is in sync")
        return 0

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
