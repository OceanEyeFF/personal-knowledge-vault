"""
CLI commands for Personal Knowledge Vault (PKV).

实现核心命令：archive / search / show / list / config / stats
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.utils.config import Config
from src.workflow.engine import WorkflowEngine
from src.retrieval.query_router import QueryRouter
from src.retrieval.bm25_retriever import BM25Retriever
from src.retrieval.vector_retriever import VectorRetriever
from src.retrieval.hybrid_retriever import HybridRetriever
from src.storage.sqlite_store import SQLiteStore
from src.utils.text_utils import TextProcessor
from src.ai.embedder import Embedder


console = Console()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_config() -> Config:
    config = Config()
    config.ensure_dirs()
    return config


def _parse_tags(tags: Optional[str]) -> List[str]:
    if not tags:
        return []
    parts = [part.strip() for part in tags.split(",")]
    return [part for part in parts if part]


def _format_bytes(size: int) -> str:
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def _get_entry_by_id(store: SQLiteStore, knowledge_id: int) -> Optional[Dict[str, Any]]:
    if hasattr(store, "get_entry_by_id"):
        return store.get_entry_by_id(knowledge_id)
    if hasattr(store, "query_by_id"):
        return store.query_by_id(knowledge_id)
    with store.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM knowledge_items WHERE knowledge_id = ?",
            (knowledge_id,),
        ).fetchone()
        return dict(row) if row else None


def _get_entry_by_url(store: SQLiteStore, url: str) -> Optional[Dict[str, Any]]:
    if hasattr(store, "get_entry_by_url"):
        return store.get_entry_by_url(url)
    with store.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM knowledge_items WHERE source_url = ?",
            (url,),
        ).fetchone()
        return dict(row) if row else None


def _query_entries(
    store: SQLiteStore,
    tag: Optional[str],
    order_by: str,
    desc: bool,
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    if hasattr(store, "query_entries"):
        filters = {"tag": tag} if tag else {}
        return store.query_entries(filters=filters, order_by=order_by, desc=desc, limit=limit)

    sql = "SELECT knowledge_id, title, source_type, source_url, tags, archived_at FROM knowledge_items"
    params: List[Any] = []

    if tag:
        tag = tag.strip()
        if store.table_exists("tags") and store.table_exists("knowledge_tags"):
            sql = (
                "SELECT ki.knowledge_id, ki.title, ki.source_type, ki.source_url, ki.tags, ki.archived_at "
                "FROM knowledge_items ki "
                "JOIN knowledge_tags kt ON ki.knowledge_id = kt.knowledge_id "
                "JOIN tags t ON kt.tag_id = t.tag_id "
                "WHERE t.name = ?"
            )
            params.append(tag)
        else:
            sql += " WHERE tags LIKE ?"
            params.append(f"%{tag}%")

    sql += f" ORDER BY {order_by} {'DESC' if desc else 'ASC'}"
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    rows: List[Dict[str, Any]] = []
    with store.get_connection() as conn:
        cursor = conn.execute(sql, tuple(params))
        for row in cursor.fetchall():
            rows.append(dict(row))
    return rows


def _count_entries(store: SQLiteStore) -> int:
    if hasattr(store, "count_entries"):
        return store.count_entries()
    with store.get_connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0])


def _count_entries_by_source_type(store: SQLiteStore) -> List[Tuple[str, int]]:
    if hasattr(store, "count_entries_by_source_type"):
        return store.count_entries_by_source_type()
    with store.get_connection() as conn:
        rows = conn.execute(
            "SELECT source_type, COUNT(*) FROM knowledge_items GROUP BY source_type"
        ).fetchall()
        return [(row[0], int(row[1])) for row in rows]


def _get_top_tags(store: SQLiteStore, limit: int = 10) -> List[Tuple[str, int]]:
    if hasattr(store, "get_top_tags"):
        return store.get_top_tags(limit=limit)
    with store.get_connection() as conn:
        if store.table_exists("tags"):
            rows = conn.execute(
                "SELECT name, count FROM tags ORDER BY count DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [(row[0], int(row[1])) for row in rows]

        tag_counter: Dict[str, int] = {}
        rows = conn.execute(
            "SELECT tags FROM knowledge_items WHERE tags IS NOT NULL AND tags != ''"
        ).fetchall()
        for row in rows:
            tags = row[0]
            if not tags:
                continue
            for tag in str(tags).split(","):
                tag = tag.strip()
                if tag:
                    tag_counter[tag] = tag_counter.get(tag, 0) + 1
        return sorted(tag_counter.items(), key=lambda x: x[1], reverse=True)[:limit]


def _extract_result_id(result: Any) -> Optional[int]:
    if result is None:
        return None
    if hasattr(result, "entry_id"):
        return getattr(result, "entry_id")
    if hasattr(result, "knowledge_id"):
        return getattr(result, "knowledge_id")
    if isinstance(result, dict):
        return result.get("entry_id") or result.get("knowledge_id")
    return None


def _extract_result_snippet(result: Any) -> str:
    if result is None:
        return ""
    for key in ("snippet", "highlight"):
        if hasattr(result, key):
            return getattr(result, key) or ""
        if isinstance(result, dict) and key in result:
            return result.get(key) or ""
    metadata = None
    if hasattr(result, "metadata"):
        metadata = getattr(result, "metadata")
    elif isinstance(result, dict):
        metadata = result.get("metadata")
    if isinstance(metadata, dict):
        return (
            metadata.get("summary_one_sentence")
            or metadata.get("summary_100_words")
            or ""
        )
    return ""


def _extract_result_metadata(result: Any) -> Dict[str, Any]:
    if result is None:
        return {}
    if hasattr(result, "metadata"):
        return getattr(result, "metadata") or {}
    if isinstance(result, dict):
        return result.get("metadata") or {}
    return {}


def _result_to_dict(result: Any) -> Dict[str, Any]:
    return {
        "entry_id": _extract_result_id(result),
        "title": getattr(result, "title", None)
        if not isinstance(result, dict)
        else result.get("title"),
        "snippet": _extract_result_snippet(result),
        "score": getattr(result, "score", None)
        if not isinstance(result, dict)
        else result.get("score"),
        "metadata": _extract_result_metadata(result),
    }


def _render_search_table(results: List[Any], title: str) -> Table:
    table = Table(title=title)
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("标题", style="bold")
    table.add_column("得分", style="green", justify="right")
    table.add_column("片段", style="")

    for result in results:
        rid = _extract_result_id(result)
        title_text = getattr(result, "title", None) if not isinstance(result, dict) else result.get("title")
        score = getattr(result, "score", None) if not isinstance(result, dict) else result.get("score")
        snippet = _extract_result_snippet(result)

        table.add_row(
            str(rid) if rid is not None else "-",
            title_text or "(无标题)",
            f"{score:.3f}" if isinstance(score, (float, int)) else "-",
            snippet[:80] + ("..." if snippet and len(snippet) > 80 else ""),
        )

    return table


def _render_list_table(rows: List[Dict[str, Any]], title: str) -> Table:
    table = Table(title=title)
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("标题", style="bold")
    table.add_column("类型", style="magenta")
    table.add_column("时间", style="green")
    table.add_column("标签")

    for row in rows:
        tags = row.get("tags") or ""
        table.add_row(
            str(row.get("knowledge_id", "-")),
            row.get("title") or "(无标题)",
            row.get("source_type") or "-",
            row.get("archived_at") or "-",
            tags,
        )

    return table


def _render_entry_panel(entry: Dict[str, Any], raw: bool = False) -> Panel:
    if raw:
        content = entry.get("content") or ""
        return Panel(content, title=f"条目 #{entry.get('knowledge_id', '-')}")

    tags = entry.get("tags") or ""
    keywords = entry.get("keywords") or ""
    summary = entry.get("summary_100_words") or entry.get("summary_one_sentence") or ""

    text = (
        f"[bold]标题[/bold]: {entry.get('title') or ''}\n"
        f"[bold]来源[/bold]: {entry.get('source_url') or ''}\n"
        f"[bold]类型[/bold]: {entry.get('source_type') or ''}\n"
        f"[bold]时间[/bold]: {entry.get('archived_at') or ''}\n"
        f"[bold]标签[/bold]: {tags}\n"
        f"[bold]关键词[/bold]: {keywords}\n\n"
        f"[bold]摘要[/bold]:\n{summary}\n\n"
        f"[bold]文件[/bold]: {entry.get('file_path') or ''}"
    )
    return Panel(text, title=f"知识条目 #{entry.get('knowledge_id', '-')}")


def _normalize_env_value(value: str) -> str:
    if value is None:
        return ""
    trimmed = value.strip()
    if not trimmed:
        return ""
    if any(ch.isspace() for ch in trimmed) or "#" in trimmed:
        if not (trimmed.startswith("\"") and trimmed.endswith("\"")):
            return f"\"{trimmed}\""
    return trimmed


def _set_env_value(env_path: Path, key: str, value: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    replaced = False
    normalized_value = _normalize_env_value(value)

    new_lines: List[str] = []
    for line in lines:
        if key_pattern.match(line):
            new_lines.append(f"{key}={normalized_value}")
            replaced = True
        else:
            new_lines.append(line)

    if not replaced:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append(f"{key}={normalized_value}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _friendly_hint(message: str) -> None:
    msg = (message or "").lower()
    if "processor" in msg or "抓取" in msg or "url" in msg:
        console.print("[yellow]提示: 请检查 URL 是否正确，或稍后重试[/yellow]")
    if "openai" in msg or "embedding" in msg or "openai_api_key" in msg:
        console.print("[yellow]提示: 请检查 OPENAI_API_KEY 配置[/yellow]")
    if "deepseek" in msg or "deepseek_api_key" in msg:
        console.print("[yellow]提示: 请检查 DEEPSEEK_API_KEY 配置[/yellow]")


@click.group()
def cli() -> None:
    """个人知识库 CLI 工具。"""


@cli.command("archive")
@click.argument("url_or_path")
@click.option("--skip-sharpen", is_flag=True, help="跳过 idea Sharpen 交互")
@click.option("--tags", help="手动指定标签（逗号分隔）")
@click.option("--quiet", is_flag=True, help="静默模式，跳过交互并减少输出")
@click.option(
    "--type",
    "content_type",
    type=click.Choice(["auto", "webpage", "chat", "news"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="内容类型",
)
def archive(url_or_path: str, skip_sharpen: bool, tags: Optional[str], quiet: bool, content_type: str) -> None:
    """归档内容到知识库。"""
    try:
        _ = _load_config()
        engine = WorkflowEngine()

        input_data: Dict[str, Any] = {
            "url": url_or_path,
            "skip_sharpen": bool(skip_sharpen or quiet),
        }

        manual_tags = _parse_tags(tags)
        if manual_tags:
            input_data["manual_tags"] = manual_tags
        if content_type and content_type != "auto":
            input_data["content_type"] = content_type

        if not quiet:
            console.print(f"正在归档: [cyan]{url_or_path}[/cyan]")

        with console.status("[cyan]归档中...[/cyan]"):
            result = asyncio.run(engine.execute_async("archive-url", input_data))

        if not result.success:
            console.print("[red]错误: 归档失败[/red]")
            errors = []
            if hasattr(result, "errors"):
                errors = result.errors or []
            elif hasattr(result, "error") and result.error:
                errors = [result.error]
            if errors:
                for err in errors:
                    console.print(f"[red]- {err}[/red]")
                    _friendly_hint(err)
            else:
                console.print("[red]未返回详细错误信息[/red]")
            sys.exit(1)

        data = result.data or {}
        entry = data.get("entry")
        knowledge_id = data.get("knowledge_id") or data.get("entry_id")
        file_path = data.get("file_path")

        if quiet:
            if knowledge_id is not None:
                console.print(str(knowledge_id))
            else:
                console.print("ok")
            return

        console.print("[green]成功: 归档完成![/green]")

        title = getattr(entry, "title", "") if entry else ""
        source_url = getattr(entry, "source_url", "") if entry else url_or_path
        tags_list = getattr(entry, "tags", []) if entry else []
        tags_text = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)

        summary = getattr(entry, "summary_100_words", "") if entry else ""
        if summary:
            summary = summary.strip()

        detail_lines = [
            f"[bold]标题[/bold]: {title}",
            f"[bold]来源[/bold]: {source_url}",
            f"[bold]标签[/bold]: {tags_text}",
            f"[bold]文件[/bold]: {file_path or ''}",
            f"[bold]ID[/bold]: {knowledge_id if knowledge_id is not None else ''}",
        ]
        if summary:
            detail_lines.insert(3, f"[bold]摘要[/bold]: {summary[:160]}" + ("..." if len(summary) > 160 else ""))

        console.print(Panel("\n".join(detail_lines), title="归档结果"))

    except Exception as exc:
        console.print(f"[red]错误: 归档异常: {exc}[/red]")
        _friendly_hint(str(exc))
        console.print("[yellow]提示: 使用 --debug 查看详细日志[/yellow]")
        sys.exit(1)


@cli.command("search")
@click.argument("query")
@click.option(
    "--strategy",
    type=click.Choice(["auto", "bm25", "vector", "hybrid"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="检索策略",
)
@click.option("--limit", type=int, default=10, show_default=True, help="返回结果数量")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json", "markdown"], case_sensitive=False),
    default="table",
    show_default=True,
    help="输出格式",
)
def search(query: str, strategy: str, limit: int, output_format: str) -> None:
    """搜索知识库。"""
    try:
        config = _load_config()
        embedder: Optional[Embedder] = None
        results: List[Any] = []
        strategy_used = strategy.lower()

        if strategy_used == "auto":
            embedder = Embedder()
            router = QueryRouter(
                db_path=config.db_path,
                vector_index_dir=config.vector_index_dir,
                embedder=embedder,
                token_threshold=10,
            )
            # QueryRouter 默认长查询使用 HybridRetriever，这里替换为 VectorRetriever 以符合规范
            router.hybrid_retriever = VectorRetriever(
                config.db_path,
                config.vector_index_dir,
                embedder,
            )

            text_processor = TextProcessor()
            token_count = len(text_processor.tokenize_chinese(query).split())
            strategy_used = "bm25" if token_count < 10 else "vector"
            results = router.search(query, limit)
        elif strategy_used == "bm25":
            retriever = BM25Retriever(config.db_path)
            results = retriever.search(query, limit)
        elif strategy_used == "vector":
            embedder = Embedder()
            retriever = VectorRetriever(config.db_path, config.vector_index_dir, embedder)
            results = retriever.search(query, limit)
        elif strategy_used == "hybrid":
            embedder = Embedder()
            retriever = HybridRetriever(config.db_path, config.vector_index_dir, embedder)
            results = retriever.search(query, limit)

        if output_format == "json":
            payload = {
                "query": query,
                "strategy": strategy_used,
                "total": len(results),
                "results": [_result_to_dict(r) for r in results],
            }
            console.print(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        if output_format == "markdown":
            lines = [
                f"# 搜索结果\n",
                f"- 查询: {query}",
                f"- 策略: {strategy_used}",
                f"- 结果数: {len(results)}\n",
            ]
            for item in results:
                rid = _extract_result_id(item)
                title = getattr(item, "title", None) if not isinstance(item, dict) else item.get("title")
                score = getattr(item, "score", None) if not isinstance(item, dict) else item.get("score")
                snippet = _extract_result_snippet(item)
                lines.append(f"## {title or '(无标题)'}")
                lines.append(f"- ID: {rid if rid is not None else '-'}")
                lines.append(f"- 得分: {score:.3f}" if isinstance(score, (float, int)) else "- 得分: -")
                if snippet:
                    lines.append(f"- 片段: {snippet}")
                lines.append("")
            console.print("\n".join(lines), markup=False)
            return

        console.print(f"搜索: [cyan]{query}[/cyan]")
        console.print(f"找到 {len(results)} 条结果 ({strategy_used} 策略)\n")
        table = _render_search_table(results, title="搜索结果")
        console.print(table)
        if results:
            console.print("提示: 使用 'pkv show <id>' 查看详情")

    except Exception as exc:
        console.print(f"[red]错误: 搜索失败: {exc}[/red]")
        _friendly_hint(str(exc))
        sys.exit(1)


@cli.command("show")
@click.argument("id_or_url", required=False)
@click.option("--url", "source_url", help="按 URL 查询")
@click.option("--raw", is_flag=True, help="输出原始 Markdown")
def show(id_or_url: Optional[str], source_url: Optional[str], raw: bool) -> None:
    """显示条目详情。"""
    try:
        config = _load_config()
        store = SQLiteStore(config.db_path)

        if not source_url and not id_or_url:
            console.print("[red]错误: 请提供 knowledge_id 或 --url[/red]")
            sys.exit(1)

        entry: Optional[Dict[str, Any]] = None

        if source_url:
            entry = _get_entry_by_url(store, source_url)
        else:
            if id_or_url and id_or_url.isdigit():
                knowledge_id = int(id_or_url)
                entry = _get_entry_by_id(store, knowledge_id)
            else:
                entry = _get_entry_by_url(store, id_or_url or "")

        if not entry:
            console.print("[yellow]警告: 未找到对应条目[/yellow]")
            sys.exit(1)

        if raw:
            file_path = entry.get("file_path")
            if not file_path:
                console.print("[red]错误: 条目缺少 file_path，无法读取原始 Markdown[/red]")
                sys.exit(1)
            path = Path(file_path)
            if not path.exists():
                console.print(f"[red]错误: Markdown 文件不存在: {file_path}[/red]")
                sys.exit(1)
            content = path.read_text(encoding="utf-8")
            console.print(content, markup=False)
            return

        panel = _render_entry_panel(entry)
        console.print(panel)

    except Exception as exc:
        console.print(f"[red]错误: 查询失败: {exc}[/red]")
        _friendly_hint(str(exc))
        sys.exit(1)


@cli.command("list")
@click.option("--tag", help="按标签过滤")
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(["time", "title", "id"], case_sensitive=False),
    default="time",
    show_default=True,
    help="排序字段",
)
@click.option("--desc", is_flag=True, help="降序排列")
@click.option("--limit", type=int, default=20, show_default=True, help="返回数量")
def list_entries(tag: Optional[str], sort_by: str, desc: bool, limit: int) -> None:
    """列出知识条目。"""
    try:
        config = _load_config()
        store = SQLiteStore(config.db_path)

        sort_map = {
            "time": "archived_at",
            "title": "title",
            "id": "knowledge_id",
        }
        order_by = sort_map.get(sort_by.lower(), "archived_at")

        rows = _query_entries(store, tag, order_by, desc, limit)

        if not rows:
            console.print("[yellow]警告: 未找到条目[/yellow]")
            return

        title = "知识条目列表"
        if tag:
            title += f" (标签: {tag})"
        console.print(_render_list_table(rows, title=title))

    except Exception as exc:
        console.print(f"[red]错误: 列表查询失败: {exc}[/red]")
        _friendly_hint(str(exc))
        sys.exit(1)


@cli.group("config")
def config_cmd() -> None:
    """配置管理。"""


@config_cmd.command("show")
def config_show() -> None:
    """显示主要配置。"""
    try:
        config = _load_config()

        table = Table(title="当前配置")
        table.add_column("键")
        table.add_column("值")

        rows = [
            ("storage.vault_dir", str(config.vault_dir)),
            ("storage.db_path", str(config.db_path)),
            ("storage.vector_index_dir", str(config.vector_index_dir)),
            ("storage.log_dir", str(config.log_dir)),
            ("storage.tmp_dir", str(config.tmp_dir)),
            ("ai.deepseek.model", config.get("ai.deepseek.model")),
            ("ai.openai.embedding_model", config.get("ai.openai.embedding_model")),
            ("logging.level", config.log_level),
            ("DEEPSEEK_API_KEY", "已设置" if config.deepseek_api_key else "未设置"),
            ("OPENAI_API_KEY", "已设置" if config.openai_api_key else "未设置"),
        ]

        for key, value in rows:
            table.add_row(key, str(value) if value is not None else "-")

        console.print(table)

    except Exception as exc:
        console.print(f"[red]错误: 配置读取失败: {exc}[/red]")
        sys.exit(1)


@config_cmd.command("get")
@click.argument("key")
def config_get(key: str) -> None:
    """查询单个配置。"""
    try:
        config = _load_config()

        value = None
        if "." in key:
            value = config.get(key)
        else:
            value = config.get_env(key)
            if value is None:
                value = config.get(key)

        if value is None:
            console.print(f"[yellow]警告: 未找到配置: {key}[/yellow]")
            sys.exit(1)

        console.print(value)

    except Exception as exc:
        console.print(f"[red]错误: 配置查询失败: {exc}[/red]")
        sys.exit(1)


@config_cmd.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """修改 .env 配置。"""
    try:
        env_path = _project_root() / ".env"
        _set_env_value(env_path, key, value)
        console.print(f"[green]成功: 已更新 {key} 到 .env[/green]")

    except Exception as exc:
        console.print(f"[red]错误: 配置更新失败: {exc}[/red]")
        sys.exit(1)


@cli.command("stats")
def stats() -> None:
    """显示统计信息。"""
    try:
        config = _load_config()
        store = SQLiteStore(config.db_path)

        if not config.db_path.exists():
            console.print("[yellow]警告: 数据库不存在，请先归档内容[/yellow]")
            return

        if not store.table_exists("knowledge_items"):
            console.print("[yellow]警告: 数据库未初始化[/yellow]")
            return

        total = _count_entries(store)
        source_rows = _count_entries_by_source_type(store)
        top_tags = _get_top_tags(store, limit=10)

        vault_size = _dir_size(config.vault_dir)
        db_size = _dir_size(config.db_path)
        vector_size = _dir_size(config.vector_index_dir)

        lines = [
            "[bold]知识库统计[/bold]",
            "",
            f"总条目数: {total}",
        ]
        for source, count in source_rows:
            lines.append(f"  - {source or 'unknown'}: {count}")

        lines.append("")
        lines.append("存储大小:")
        lines.append(f"  - Markdown: {_format_bytes(vault_size)}")
        lines.append(f"  - SQLite: {_format_bytes(db_size)}")
        lines.append(f"  - 向量索引: {_format_bytes(vector_size)}")

        lines.append("")
        lines.append("标签统计 (Top 10):")
        if top_tags:
            for idx, (name, count) in enumerate(top_tags, start=1):
                lines.append(f"  {idx}. {name} ({count})")
        else:
            lines.append("  - 暂无标签")

        console.print(Panel("\n".join(lines)))

    except Exception as exc:
        console.print(f"[red]错误: 统计失败: {exc}[/red]")
        _friendly_hint(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    cli()
