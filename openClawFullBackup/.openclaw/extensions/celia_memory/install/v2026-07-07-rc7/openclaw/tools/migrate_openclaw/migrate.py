"""OpenClaw → Celia 长期记忆迁移 CLI 入口。

子命令：

  export    读 MEMORY.md，逐段走 Celia memory_add 写入。
  verify    输出迁移报告 + failures.jsonl，方便人工审阅。
  run       先 export 再 verify；即使 export 出错也会跑 verify，
            因为 verify 产出的报告正是用来排查失败的。

所有子命令都依赖一个在运行的 Celia 服务，默认 URL 是
$CELIA_BASE_URL 或 http://localhost:3000。export/run 还需要
--workspace 指向具体 OpenClaw agent 的 workspace 目录。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from pathlib import Path

from . import export_longterm, verify
from .mcp_client import CeliaClient, McpError, RetryPolicy
from .openclaw_reader import (
    read_all_for_migration,
    read_long_term,
)


def _default_run_id() -> str:
    """缺省 run_id 用当前时间戳，便于多次重跑在 manifest 中区分。"""
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _make_client(args: argparse.Namespace) -> CeliaClient:
    """基于 argparse 参数组装一个 CeliaClient 实例。"""
    base_url = args.celia_base or os.environ.get(
        "CELIA_BASE_URL", "http://localhost:3000"
    )
    session_id = args.session_id or (
        f"migrate-{args.agent_id}-{args.run_id}"
    )
    policy = RetryPolicy(
        max_attempts=args.max_attempts,
        base_backoff_seconds=args.backoff_base,
        timeout_seconds=args.timeout,
    )
    return CeliaClient(base_url=base_url, session_id=session_id,
                      policy=policy)


def _connect(client: CeliaClient, user_id: str) -> None:
    """健康检查 → initialize 握手 → open session。"""
    health = client.health()
    print(f"  health: {health}")
    info = client.initialize().get("serverInfo", {})
    print(f"  server: {info.get('name')} v{info.get('version')}")
    client.open_session(user_id=user_id)


def _resolve_paths(args: argparse.Namespace) -> tuple[Path | None, Path, Path]:
    """解析 workspace / out_dir / manifest_path 这三个常用路径。"""
    workspace = Path(args.workspace).expanduser() if args.workspace else None
    out_dir = Path(args.out_dir).expanduser()
    manifest_path = out_dir / f"manifest-{args.agent_id}.jsonl"
    return workspace, out_dir, manifest_path


def _resolve_user_id(args: argparse.Namespace) -> str:
    """确定最终写入 Celia 时使用的 userId。

    默认按 agent-id 派生：`openclaw-<agent-id>`。显式 `--user-id`
    会覆盖默认值（比如多 agent 合并到同一 user 时用）。
    """
    return args.user_id or f"openclaw-{args.agent_id}"


def _add_common_args(p: argparse.ArgumentParser,
                     need_workspace: bool = True) -> None:
    """往一个子解析器上挂通用参数。"""
    if need_workspace:
        p.add_argument(
            "--workspace", required=True,
            help="OpenClaw agent workspace 路径，例如 ~/.openclaw/workspace",
        )
    else:
        # verify 子命令不读 workspace，但保留位点以兼容脚本组合
        p.add_argument("--workspace", default=None,
                       help=argparse.SUPPRESS)
    p.add_argument("--agent-id", default=os.environ.get(
        "OPENCLAW_AGENT_ID", "main"
    ))
    p.add_argument("--user-id", default=None,
                   help="目标 Celia userId（默认 'openclaw-<agent-id>'）")
    p.add_argument("--celia-base", default=None,
                   help="Celia 根 URL（env: CELIA_BASE_URL）")
    p.add_argument("--session-id", default=None)
    p.add_argument("--run-id", default=os.environ.get(
        "MIGRATE_RUN_ID", _default_run_id()
    ))
    p.add_argument("--out-dir", default="out")
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--backoff-base", type=float, default=1.0)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--ingest-mode",
        choices=["immediate", "deferred", "deferred-urgent"],
        default="deferred",
        help=(
            "memory_add 的 ingestMode。"
            "deferred（默认）走对话窗口管线提取多条原子 L2 事实；"
            "immediate 是服务端 legacy 默认，对 markdown 文档效果差。"
        ),
    )


def _cmd_export(args: argparse.Namespace) -> int:
    """执行 export 子命令；返回退出码（0 表示无失败条目）。

    默认 reader 为 `read_all_for_migration`（先 USER.md 再 Memory.md）；
    `--long-term-only` 退化为仅读 Memory.md（老行为，测试用）。
    """
    workspace, _, manifest_path = _resolve_paths(args)
    user_id = _resolve_user_id(args)
    client = _make_client(args)
    _connect(client, user_id)
    reader = (
        read_long_term if getattr(args, "long_term_only", False)
        else read_all_for_migration
    )
    stats = export_longterm.run(
        workspace=workspace, user_id=user_id, client=client,
        manifest_path=manifest_path, run_id=args.run_id,
        dry_run=args.dry_run,
        reader=reader,
        ingest_mode=args.ingest_mode,
    )
    return 0 if stats.failed == 0 else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    """执行 verify 子命令。退出码：0 全 ok，2 有非 ok 条目。"""
    workspace, out_dir, manifest_path = _resolve_paths(args)
    user_id = _resolve_user_id(args)
    client = _make_client(args)
    _connect(client, user_id)

    # 读召回探针文件（可选），# 开头的行当注释跳过
    probes: list[str] = []
    if args.recall_probes_file:
        probes = [
            line.strip() for line in
            Path(args.recall_probes_file).expanduser()
                .read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]

    # 如果能访问 workspace，预读一份 items 供 verify 做 content 兜底匹配
    workspace_items: list[dict] = []
    if workspace and workspace.exists():
        workspace_items = [
            {"hash": it.hash(), "text": it.text}
            for it in read_long_term(workspace, user_id)
        ]

    report = verify.run(
        user_id=user_id, client=client,
        manifest_path=manifest_path, out_dir=out_dir,
        recall_probes=probes,
        workspace_items=workspace_items,
    )
    non_ok = sum(v for k, v in report.counts.items() if k != "ok")
    return 0 if non_ok == 0 else 2


def _cmd_run(args: argparse.Namespace) -> int:
    """export + verify 一体化。

    设计取舍：export 失败时**默认仍然**继续跑 verify——报告本身就是
    给人排查失败用的，没报告反而更糟。`--strict` 恢复旧行为（失败
    立刻返回，不再跑 verify）。
    """
    export_code = _cmd_export(args)
    if export_code and args.strict:
        return export_code
    verify_code = _cmd_verify(args)
    # 组合退出码：export 失败 → 1；仅 verify 有 non-ok → 2；都成功 → 0
    return export_code or verify_code


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器，返回给 main 用。"""
    p = argparse.ArgumentParser(
        prog="migrate-openclaw",
        description="把 OpenClaw 的长期记忆（MEMORY.md）通过 HTTP MCP "
                    "迁移到运行中的 Celia 服务。",
    )
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("export", help="导出 USER.md + Memory.md 到 Celia")
    _add_common_args(e, need_workspace=True)
    e.add_argument(
        "--long-term-only", action="store_true",
        help="仅导 Memory.md，跳过 USER.md（老行为，一般只测试时用）",
    )
    e.set_defaults(func=_cmd_export)

    v = sub.add_parser("verify", help="跑校验并生成报告")
    _add_common_args(v, need_workspace=False)
    v.add_argument("--recall-probes-file", default=None,
                   help="召回探针文件，每行一个查询")
    v.set_defaults(func=_cmd_verify)

    r = sub.add_parser("run", help="export 然后 verify")
    _add_common_args(r, need_workspace=True)
    r.add_argument("--recall-probes-file", default=None)
    r.add_argument("--strict", action="store_true",
                   help="export 出任何失败时立即返回，不再跑 verify")
    r.add_argument("--long-term-only", action="store_true",
                   help="仅导 Memory.md，跳过 USER.md")
    r.set_defaults(func=_cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    """命令行入口。McpError 统一转成退出码 3。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except McpError as e:
        print(f"[fatal] MCP error: [{e.kind}] {e.message}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
