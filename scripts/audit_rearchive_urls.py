"""Extract and validate source URLs before rebuilding a knowledge vault.

The generated reports can contain private browsing/knowledge URLs, so the
default output directory (``.migration/``) is intentionally git-ignored.
This script is read-only with respect to the source vault.

This is a legacy, user-only utility. It is never a default automation entry
point: callers must explicitly supply ``--vault`` after a user has authorized
reading that Vault. URL validation performs outbound network requests, so do
not run it against real data or networks without that same explicit user
authorization.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import frontmatter
import requests


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 PKV-Migration-Audit/1.0"
)
SOFT_FAILURE_MARKERS = (
    "404 not found",
    "page not found",
    "页面不存在",
    "内容不存在",
    "文章不存在",
    "该内容已被删除",
    "此内容已被删除",
    "内容已下架",
)
REVIEW_STATUS_CODES = {401, 403, 407, 408, 423, 425, 429}
INVALID_STATUS_CODES = {404, 410, 451}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "旧 Vault URL 审计（仅限获得明确用户授权后的手动运行）。"
            "此工具不属于默认自动化：读取 Vault 与验证 URL 都需要显式授权；"
            "URL 验证会访问外部网络。"
        ),
        epilog=(
            "必须显式提供 --vault。请勿在未获用户明确授权时读取真实 Vault "
            "或发起网络请求。"
        ),
    )
    parser.add_argument(
        "--vault",
        type=Path,
        required=True,
        metavar="VAULT",
        help="必填：经用户明确授权后才可读取的旧 Vault 目录。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".migration/url-audit"),
        help="报告输出目录（默认：.migration/url-audit）。",
    )
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args(argv)


def canonicalize_url(raw_url: str) -> tuple[str | None, str | None]:
    value = raw_url.strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        return None, f"url_parse_error:{exc}"

    if parsed.scheme.lower() not in {"http", "https"}:
        return None, "unsupported_scheme"
    if not parsed.hostname:
        return None, "missing_hostname"
    if parsed.username or parsed.password:
        return None, "userinfo_not_allowed"

    hostname = parsed.hostname.lower().rstrip(".")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None, "invalid_hostname"

    port = parsed.port
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, "")), None


def extract_urls(vault: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    records_by_url: dict[str, dict[str, Any]] = {}
    extraction_issues: list[dict[str, Any]] = []
    markdown_files = sorted(vault.rglob("*.md"))

    for path in markdown_files:
        relative_path = path.relative_to(vault).as_posix()
        try:
            post = frontmatter.load(path)
        except Exception as exc:  # report malformed legacy files, continue auditing
            extraction_issues.append(
                {
                    "source_file": relative_path,
                    "status": "review",
                    "reason": f"frontmatter_error:{type(exc).__name__}",
                }
            )
            continue

        raw_url = post.metadata.get("source_url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            extraction_issues.append(
                {
                    "source_file": relative_path,
                    "status": "review",
                    "reason": "missing_source_url",
                }
            )
            continue

        canonical_url, error = canonicalize_url(raw_url)
        if error or canonical_url is None:
            extraction_issues.append(
                {
                    "source_file": relative_path,
                    "url": raw_url.strip(),
                    "status": "invalid",
                    "reason": error,
                }
            )
            continue

        existing = records_by_url.get(canonical_url)
        if existing is None:
            records_by_url[canonical_url] = {
                "url": canonical_url,
                "source_files": [relative_path],
            }
        else:
            existing["source_files"].append(relative_path)

    return list(records_by_url.values()), extraction_issues, len(markdown_files)


def resolve_public_addresses(hostname: str, port: int) -> tuple[list[str], str | None]:
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return [], "dns_resolution_failed"

    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        return [], "dns_no_addresses"

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return addresses, "non_public_address"
    return addresses, None


def classify_response(response: requests.Response, sample: bytes) -> tuple[str, str]:
    status_code = response.status_code
    if status_code in INVALID_STATUS_CODES:
        return "invalid", f"http_{status_code}"
    if status_code in REVIEW_STATUS_CODES or status_code >= 500:
        return "review", f"http_{status_code}"
    if status_code >= 400:
        return "invalid", f"http_{status_code}"

    content_type = response.headers.get("content-type", "").lower()
    if content_type and not any(
        allowed in content_type
        for allowed in ("text/", "application/xhtml+xml", "application/json")
    ):
        return "review", f"unexpected_content_type:{content_type.split(';', 1)[0]}"

    encoding = response.encoding or "utf-8"
    text_sample = sample.decode(encoding, errors="replace").lower()
    if any(marker in text_sample for marker in SOFT_FAILURE_MARKERS):
        return "review", "soft_failure_marker"
    if len(text_sample.strip()) < 80:
        return "review", "content_too_short"
    return "valid", f"http_{status_code}"


def validate_record(
    record: dict[str, Any], timeout: float, retries: int
) -> dict[str, Any]:
    url = record["url"]
    parsed = urlsplit(url)
    addresses, dns_error = resolve_public_addresses(
        parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80)
    )
    result = dict(record)
    result["resolved_address_count"] = len(addresses)
    if dns_error:
        result.update(status="invalid", reason=dns_error)
        return result

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        "Range": "bytes=0-65535",
    }
    last_error: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            with requests.get(
                url,
                headers=headers,
                timeout=(min(timeout, 10.0), timeout),
                allow_redirects=True,
                stream=True,
            ) as response:
                sample = b""
                for chunk in response.iter_content(chunk_size=8192):
                    sample += chunk
                    if len(sample) >= 65536:
                        break
                status, reason = classify_response(response, sample)
                result.update(
                    status=status,
                    reason=reason,
                    http_status=response.status_code,
                    final_url=response.url,
                    redirect_count=len(response.history),
                    content_type=response.headers.get("content-type", ""),
                )
                return result
        except requests.RequestException as exc:
            last_error = exc
            if attempt < max(1, retries):
                time.sleep(0.5 * attempt)

    result.update(
        status="review",
        reason=f"request_error:{type(last_error).__name__}",
    )
    return result


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    vault = args.vault.resolve()
    output = args.output.resolve()
    if not vault.is_dir():
        raise SystemExit(f"vault 目录不存在: {vault}")
    if args.workers < 1:
        raise SystemExit("--workers 必须大于 0")

    output.mkdir(parents=True, exist_ok=True)
    url_records, extraction_issues, markdown_count = extract_urls(vault)

    validated: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(validate_record, record, args.timeout, args.retries): record
            for record in url_records
        }
        for future in as_completed(futures):
            validated.append(future.result())

    validated.sort(key=lambda item: item["url"])
    all_records = sorted(
        [*validated, *extraction_issues],
        key=lambda item: (item.get("url", ""), item.get("source_file", "")),
    )
    valid = [item for item in validated if item["status"] == "valid"]
    invalid = [
        item
        for item in all_records
        if item.get("status") == "invalid"
    ]
    review = [
        item
        for item in all_records
        if item.get("status") == "review"
    ]
    duplicate_sources = sum(
        max(0, len(record.get("source_files", [])) - 1) for record in url_records
    )
    summary = {
        "markdown_files": markdown_count,
        "unique_urls": len(url_records),
        "duplicate_sources": duplicate_sources,
        "valid": len(valid),
        "invalid": len(invalid),
        "review": len(review),
    }

    write_jsonl(output / "all.jsonl", all_records)
    write_jsonl(output / "invalid.jsonl", invalid)
    write_jsonl(output / "review.jsonl", review)
    with (output / "valid.txt").open("w", encoding="utf-8", newline="\n") as handle:
        for record in valid:
            handle.write(record["url"] + "\n")
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("URL_AUDIT_OK", json.dumps(summary, ensure_ascii=False, sort_keys=True))
    print(f"REPORT_DIR {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
