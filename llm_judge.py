#!/usr/bin/env python3
"""Batch-label QC Markdown records with a cloud-configured Knot agent."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

RECORD_RE = re.compile(r"(?m)^##\s+(?P<number>\d+)\.\s+(?P<instance_id>\S+)\s*$")
ANALYSIS_RE = re.compile(r"<analysis_process>.*?</analysis_process>", re.DOTALL)
RESULT_RE = re.compile(r"<quality_result>.*?</quality_result>", re.DOTALL)
VALID_RESULTS = {"PASS", "FAIL"}

def load_dotenv(path: Path, override: bool = False) -> dict[str, str]:
    """Load KEY=VALUE pairs from .env without requiring python-dotenv."""
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid .env line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env key on line {line_number}: {key!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'\"', "'"}:
            value = value[1:-1]
        loaded[key] = value
        if override or not os.environ.get(key, "").strip():
            os.environ[key] = value
    return loaded



@dataclass(frozen=True)
class Record:
    number: int
    instance_id: str
    markdown: str


def split_records(markdown: str) -> list[Record]:
    """Split an extract/OCR Markdown document into independent QC records."""
    matches = list(RECORD_RE.finditer(markdown))
    records: list[Record] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        block = markdown[match.start():end].strip()
        records.append(Record(int(match.group("number")), match.group("instance_id"), block))
    return records



def text_of(node: ET.Element | None) -> str:
    return "" if node is None or node.text is None else node.text.strip()


def parse_bool(value: str, field: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"{field} must be true or false, got {value!r}")
    return normalized == "true"


def extract_and_validate_xml(output: str) -> dict[str, Any]:
    """Extract the two required XML blocks and validate key label fields."""
    analysis_matches = ANALYSIS_RE.findall(output)
    result_matches = RESULT_RE.findall(output)
    if len(analysis_matches) != 1 or len(result_matches) != 1:
        raise ValueError(
            f"expected exactly one analysis_process and one quality_result; "
            f"got {len(analysis_matches)} and {len(result_matches)}"
        )

    outside = ANALYSIS_RE.sub("", output)
    outside = RESULT_RE.sub("", outside).strip()
    if outside:
        raise ValueError(f"unexpected text outside XML blocks: {outside[:160]!r}")

    analysis_xml = analysis_matches[0]
    result_xml = result_matches[0]
    analysis_root = ET.fromstring(analysis_xml)
    result_root = ET.fromstring(result_xml)

    is_qualified = parse_bool(text_of(result_root.find("is_qualified")), "is_qualified")
    answer_qualified = parse_bool(text_of(result_root.find("answer_qualified")), "answer_qualified")
    evaluation_qualified = parse_bool(
        text_of(result_root.find("evaluation_qualified")), "evaluation_qualified"
    )
    result = text_of(result_root.find("result")).upper()
    if result not in VALID_RESULTS:
        raise ValueError(f"result must be PASS or FAIL, got {result!r}")
    if is_qualified != (result == "PASS"):
        raise ValueError("is_qualified conflicts with result")
    if is_qualified != (answer_qualified and evaluation_qualified):
        raise ValueError("overall label conflicts with answer/evaluation labels")

    corrected_node = result_root.find("corrected_evaluation")
    corrected_required = False
    corrected_text = ""
    if corrected_node is not None:
        corrected_required = parse_bool(
            corrected_node.attrib.get("required", ""), "corrected_evaluation.required"
        )
        corrected_text = text_of(corrected_node)
    if corrected_required:
        if not corrected_text or corrected_text == "null":
            raise ValueError("corrected_evaluation is required but empty")
        try:
            json.loads(corrected_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrected_evaluation is not valid JSON: {exc}") from exc
    elif corrected_text not in {"", "null"}:
        raise ValueError("corrected_evaluation must be null when required=false")

    for xpath in (
        "./answer_review/format_and_completeness",
        "./answer_review/source_quality",
        "./answer_review/claim_coverage",
        "./evaluation_review/json_structure",
        "./evaluation_review/required",
        "./evaluation_review/unique_columns",
        "./evaluation_review/eval_pipeline",
    ):
        node = analysis_root.find(xpath)
        if node is None or node.attrib.get("status") not in {"pass", "fail"}:
            raise ValueError(f"{xpath} must have status=pass or status=fail")

    issues = []
    for issue in analysis_root.findall("./issues/issue"):
        severity = issue.attrib.get("severity", "")
        area = issue.attrib.get("area", "")
        issue_text = text_of(issue)
        if severity not in {"critical", "major", "minor"}:
            raise ValueError(f"invalid issue severity: {severity!r}")
        if area not in {"answer", "sources", "evaluation"}:
            raise ValueError(f"invalid issue area: {area!r}")
        if not issue_text or issue_text.lower() == "none":
            raise ValueError("issue text must describe a concrete problem")
        issues.append({"severity": severity, "area": area, "text": issue_text})

    return {
        "analysis_xml": analysis_xml,
        "quality_result_xml": result_xml,
        "is_qualified": is_qualified,
        "answer_qualified": answer_qualified,
        "evaluation_qualified": evaluation_qualified,
        "result": result,
        "summary": text_of(result_root.find("summary")),
        "corrected_evaluation_required": corrected_required,
        "corrected_evaluation": corrected_text if corrected_required else None,
        "issues": issues,
    }



def _decode_json_string(value: str) -> str:
    """Decode JSON quoting plus still-escaped newlines and Unicode sequences."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
            if isinstance(decoded, str):
                value = decoded
        except json.JSONDecodeError:
            pass

    # The Knot response may expose escapes as literal characters after the
    # outer JSON envelope was decoded. JSON Unicode escapes are exactly four
    # hex digits; surrogate pairs are combined by the final UTF-16 round trip.
    def replace_unicode(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    value = re.sub(r"\\u([0-9a-fA-F]{4})", replace_unicode, value)
    value = value.replace("\\r\\n", "\n").replace("\\n", "\n")
    value = value.replace("\\r", "\n").replace("\\t", "\t")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        value = value.encode("utf-16", "surrogatepass").decode("utf-16")
    return value


def _collect_response_text(value: Any, candidates: list[str], deltas: list[str]) -> None:
    """Collect text from common JSON/AG-UI response shapes."""
    if isinstance(value, str):
        decoded = _decode_json_string(value)
        candidates.append(decoded)
        if decoded != value and decoded[:1] in "[{":
            try:
                _collect_response_text(json.loads(decoded), candidates, deltas)
            except json.JSONDecodeError:
                pass
        return
    if isinstance(value, list):
        for item in value:
            _collect_response_text(item, candidates, deltas)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key.lower() in {"delta", "text_delta", "content_delta"} and isinstance(item, str):
            deltas.append(_decode_json_string(item))
            continue
        _collect_response_text(item, candidates, deltas)


def extract_agent_output(response: requests.Response) -> str:
    """Extract the agent's XML from JSON, NDJSON/SSE, or plain text responses."""
    raw = response.text.strip()
    candidates: list[str] = []
    deltas: list[str] = []

    try:
        _collect_response_text(response.json(), candidates, deltas)
    except (requests.exceptions.JSONDecodeError, json.JSONDecodeError, ValueError):
        pass

    # Some AG-UI gateways return one JSON event per line (optionally prefixed by data:).
    # Do not reparse an ordinary single JSON response, which would duplicate deltas.
    content_type = response.headers.get("content-type", "").lower()
    is_event_stream = "text/event-stream" in content_type or any(
        line.lstrip().startswith("data:") for line in raw.splitlines()
    )
    if is_event_stream:
        for line in raw.splitlines():
            payload = line.strip()
            if payload.startswith("data:"):
                payload = payload[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                _collect_response_text(json.loads(payload), candidates, deltas)
            except json.JSONDecodeError:
                continue

    if deltas:
        candidates.append("".join(deltas))
    candidates.append(_decode_json_string(raw))
    fallback = ""
    for candidate in candidates:
        candidate = candidate.strip()
        if "<analysis_process>" not in candidate or "<quality_result>" not in candidate:
            continue
        if not fallback or len(candidate) < len(fallback):
            fallback = candidate
        outside = ANALYSIS_RE.sub("", candidate)
        outside = RESULT_RE.sub("", outside).strip()
        if not outside:
            return candidate
    if fallback:
        return fallback
    raise ValueError(f"agent response does not contain required XML blocks: {raw[:300]!r}")


def call_model(
    record: Record,
    api_token: str,
    api_url: str,
    api_user: str,
    agent_client_uuid: str,
    timeout: float,
    enable_web_search: bool,
) -> tuple[str, dict[str, Any]]:
    # The agent's prompt is configured in Knot. Send only the record itself.
    chat_body = {
        "input": {
            "message": record.markdown,
            "stream": False,
            "enable_web_search": enable_web_search,
            "chat_extra": {"agent_client_uuid": agent_client_uuid},
        }
    }
    headers = {
        "x-knot-api-token": api_token,
        "x-knot-api-user": api_user,
    }
    response = requests.post(api_url, json=chat_body, headers=headers, timeout=timeout)
    response.raise_for_status()
    content = extract_agent_output(response)
    parsed = extract_and_validate_xml(content)
    return content, {"parsed": parsed, "usage": None}


def judge_one(
    record: Record,
    api_token: str,
    api_url: str,
    api_user: str,
    agent_client_uuid: str,
    timeout: float,
    enable_web_search: bool,
    retries: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    last_error = ""
    for attempt in range(1, retries + 2):
        try:
            raw_output, data = call_model(
                record, api_token, api_url, api_user, agent_client_uuid, timeout,
                enable_web_search
            )
            parsed = data["parsed"]
            return {
                "instance_id": record.instance_id,
                "record_number": record.number,
                "success": True,
                "attempts": attempt,
                "provider": "knot_agent",
                "api_url": api_url,
                "result": parsed["result"],
                "is_qualified": parsed["is_qualified"],
                "answer_qualified": parsed["answer_qualified"],
                "evaluation_qualified": parsed["evaluation_qualified"],
                "summary": parsed["summary"],
                "issues": parsed["issues"],
                "corrected_evaluation_required": parsed["corrected_evaluation_required"],
                "corrected_evaluation": parsed["corrected_evaluation"],
                "analysis_xml": parsed["analysis_xml"],
                "quality_result_xml": parsed["quality_result_xml"],
                "raw_output": raw_output,
                "usage": data["usage"],
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "labeled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "error": "",
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt <= retries:
                time.sleep(min(8.0, 1.5 * (2 ** (attempt - 1))))
    return {
        "instance_id": record.instance_id,
        "record_number": record.number,
        "success": False,
        "attempts": retries + 1,
        "provider": "knot_agent",
        "api_url": api_url,
        "result": "ERROR",
        "is_qualified": None,
        "answer_qualified": None,
        "evaluation_qualified": None,
        "summary": "",
        "issues": [],
        "corrected_evaluation_required": False,
        "corrected_evaluation": None,
        "analysis_xml": "",
        "quality_result_xml": "",
        "raw_output": "",
        "usage": None,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "labeled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "error": last_error,
    }


def load_completed(path: Path) -> dict[str, dict[str, Any]]:
    completed: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return completed
    with path.open("r", encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            instance_id = item.get("instance_id")
            if instance_id and item.get("success"):
                completed[str(instance_id)] = item
    return completed


def append_jsonl(path: Path, item: dict[str, Any], lock: threading.Lock) -> None:
    line = json.dumps(item, ensure_ascii=False) + "\n"
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(line)
            stream.flush()


def load_latest_results(path: Path) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            instance_id = str(item.get("instance_id") or "")
            if instance_id:
                latest[instance_id] = item
    return sorted(latest.values(), key=lambda item: int(item.get("record_number") or 0))


def escape_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", "<br>")


def write_summary(path: Path, results: list[dict[str, Any]], source: Path) -> None:
    success = [item for item in results if item.get("success")]
    passed = [item for item in success if item.get("result") == "PASS"]
    failed = [item for item in success if item.get("result") == "FAIL"]
    errors = [item for item in results if not item.get("success")]
    answer_fail = [item for item in success if item.get("answer_qualified") is False]
    eval_fail = [item for item in success if item.get("evaluation_qualified") is False]

    lines = [
        "# LLM 自动质检打标汇总", "",
        f"- 输入文件：`{source.name}`",
        f"- 总记录数：{len(results)}",
        f"- PASS：{len(passed)}",
        f"- FAIL：{len(failed)}",
        f"- API/格式错误：{len(errors)}",
        f"- answer 不合格：{len(answer_fail)}",
        f"- evaluation 不合格：{len(eval_fail)}", "",
        "| 序号 | instance_id | 总体 | answer | evaluation | 摘要/错误 |",
        "|---:|---|---|---|---|---|",
    ]
    for item in results:
        answer = "PASS" if item.get("answer_qualified") is True else "FAIL" if item.get("answer_qualified") is False else "-"
        evaluation = "PASS" if item.get("evaluation_qualified") is True else "FAIL" if item.get("evaluation_qualified") is False else "-"
        note = item.get("summary") if item.get("success") else item.get("error")
        lines.append(
            f"| {item.get('record_number', '')} | `{escape_cell(item.get('instance_id'))}` | "
            f"{escape_cell(item.get('result'))} | {answer} | {evaluation} | {escape_cell(note)} |"
        )

    lines.extend(["", "## 详细结果", ""])
    for item in results:
        lines.extend([
            f"### {item.get('record_number', '')}. {item.get('instance_id', '')}", "",
            f"- 状态：**{item.get('result', 'ERROR')}**",
            f"- answer：{item.get('answer_qualified')}",
            f"- evaluation：{item.get('evaluation_qualified')}",
            f"- 摘要：{item.get('summary') or item.get('error') or '（空）'}", "",
        ])
        if item.get("success"):
            lines.extend([item.get("analysis_xml", ""), "", item.get("quality_result_xml", ""), ""])
        else:
            lines.extend(["```text", item.get("error", "unknown error"), "```", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


CSV_FIELDS = [
    "record_number", "instance_id", "result", "is_qualified",
    "answer_qualified", "evaluation_qualified", "summary", "issues",
    "corrected_evaluation_required", "corrected_evaluation",
    "analysis_xml", "quality_result_xml", "provider", "api_url",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "elapsed_seconds", "attempts", "labeled_at", "success", "error",
]


def write_csv(path: Path, results: list[dict[str, Any]]) -> None:
    """Write one latest label per instance_id as an Excel-friendly UTF-8 CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for item in results:
            usage = item.get("usage") or {}
            row = dict(item)
            row["issues"] = json.dumps(item.get("issues") or [], ensure_ascii=False)
            corrected = item.get("corrected_evaluation")
            if isinstance(corrected, (dict, list)):
                corrected = json.dumps(corrected, ensure_ascii=False)
            row["corrected_evaluation"] = corrected or ""
            row["prompt_tokens"] = usage.get("prompt_tokens")
            row["completion_tokens"] = usage.get("completion_tokens")
            row["total_tokens"] = usage.get("total_tokens")
            writer.writerow(row)


def select_records(records: list[Record], instance_ids: list[str], limit: int) -> list[Record]:
    selected = records
    if instance_ids:
        wanted = set(instance_ids)
        selected = [record for record in selected if record.instance_id in wanted]
        missing = wanted - {record.instance_id for record in selected}
        if missing:
            raise ValueError(f"instance_id not found: {', '.join(sorted(missing))}")
    if limit > 0:
        selected = selected[:limit]
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().with_name(".env"), help=".env file loaded before reading API/model environment variables")
    parser.add_argument("input", type=Path, help="QC/OCR Markdown containing ## N. instance_id records")
    parser.add_argument("-o", "--output", type=Path, help="Incremental JSONL output")
    parser.add_argument("--summary", type=Path, help="Human-readable Markdown summary")
    parser.add_argument("--csv", type=Path, help="Final CSV containing the latest result per instance_id")
    parser.add_argument("--api-url", help="Knot agent endpoint; defaults to KNOT_API_URL")
    parser.add_argument("--api-token-env", default="KNOT_API_TOKEN", help="Environment variable containing the Knot API token")
    parser.add_argument("--api-user", help="Knot API user; defaults to KNOT_API_USER")
    parser.add_argument("--agent-client-uuid", help="Agent client UUID; defaults to KNOT_AGENT_CLIENT_UUID")
    parser.add_argument("--enable-web-search", action="store_true", help="Enable web search for the cloud agent")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent API requests")
    parser.add_argument("--limit", type=int, default=0, help="Process first N selected records; 0 means all")
    parser.add_argument("--instance-id", action="append", default=[], help="Only process this instance_id; repeatable")
    parser.add_argument("--timeout", type=float, default=180, help="Per-request timeout in seconds")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--no-resume", action="store_true", help="Do not skip successful existing labels")
    parser.add_argument("--retry-errors", action="store_true", help="With resume, retry prior failed records")
    parser.add_argument("--dry-run", action="store_true", help="Parse records and print planned work without API calls")
    args = parser.parse_args()
    try:
        load_dotenv(args.env_file.resolve())
    except (OSError, ValueError) as exc:
        parser.error(f"cannot load .env: {exc}")

    args.api_url = args.api_url or os.getenv("KNOT_API_URL", "").strip()
    args.api_user = args.api_user or os.getenv("KNOT_API_USER", "").strip()
    args.agent_client_uuid = args.agent_client_uuid or os.getenv("KNOT_AGENT_CLIENT_UUID", "").strip()
    if not args.enable_web_search:
        args.enable_web_search = os.getenv("KNOT_ENABLE_WEB_SEARCH", "false").strip().lower() in {"1", "true", "yes", "on"}

    input_path = args.input.resolve()
    output_path = (args.output or input_path.with_name(f"{input_path.stem}_judge.jsonl")).resolve()
    summary_path = (args.summary or input_path.with_name(f"{input_path.stem}_judge_summary.md")).resolve()
    csv_path = args.csv.resolve() if args.csv else None

    try:
        markdown = input_path.read_text(encoding="utf-8")
        records = select_records(split_records(markdown), args.instance_id, args.limit)
    except (OSError, ValueError) as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 1
    if not records:
        print("No QC records found.", file=sys.stderr)
        return 1

    completed = {} if args.no_resume else load_completed(output_path)
    pending = [record for record in records if record.instance_id not in completed]
    if args.retry_errors:
        # Failed entries are never included in completed, so this flag documents intent.
        pending = [record for record in records if record.instance_id not in completed]

    print(f"Input records: {len(records)}; completed: {len(completed)}; pending: {len(pending)}")
    print(f"Knot agent: {args.api_url}; workers: {max(1, args.workers)}; web_search: {args.enable_web_search}")
    print(f"JSONL: {output_path}")
    print(f"Summary: {summary_path}")
    if csv_path:
        print(f"CSV: {csv_path}")
    if args.dry_run:
        for record in pending:
            print(f"- {record.number}. {record.instance_id}: {len(record.markdown)} chars")
        return 0

    api_token = os.getenv(args.api_token_env, "").strip()
    missing = []
    if not api_token:
        missing.append(args.api_token_env)
    if not args.api_url:
        missing.append("KNOT_API_URL/--api-url")
    if not args.api_user:
        missing.append("KNOT_API_USER/--api-user")
    if not args.agent_client_uuid:
        missing.append("KNOT_AGENT_CLIENT_UUID/--agent-client-uuid")
    if missing:
        print(f"Missing configuration: {', '.join(missing)}", file=sys.stderr)
        return 2

    write_lock = threading.Lock()
    workers = min(max(1, args.workers), max(1, len(pending)))
    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(
                    judge_one,
                    record,
                    api_token,
                    args.api_url,
                    args.api_user,
                    args.agent_client_uuid,
                    max(1.0, args.timeout),
                    args.enable_web_search,
                    max(0, args.retries),
                ): record
                for record in pending
            }
            finished = 0
            for future in concurrent.futures.as_completed(future_map):
                record = future_map[future]
                try:
                    item = future.result()
                except Exception as exc:  # Defensive: judge_one normally captures errors.
                    item = {
                        "instance_id": record.instance_id,
                        "record_number": record.number,
                        "success": False,
                        "result": "ERROR",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                append_jsonl(output_path, item, write_lock)
                finished += 1
                print(
                    f"[{finished}/{len(pending)}] {record.instance_id}: "
                    f"{item.get('result')} ({item.get('elapsed_seconds', 0)}s)"
                )

    results = load_latest_results(output_path)
    write_summary(summary_path, results, input_path)
    if csv_path:
        write_csv(csv_path, results)
    errors = sum(not item.get("success") for item in results)
    print(f"Finished: {len(results)} labels; errors: {errors}")
    return 0 if errors == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())