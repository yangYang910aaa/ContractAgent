"""政策库运维助手（轻档：确定性起稿，不调模型）。

把一份新政策文档变成三样待审产物：
- 规范化草稿：解析标题/编号/版本/生效日期/归口/适用范围与条文，按现有体例重排；
- 重叠报告：逐条用向量检索现有政策（余弦分，可读），标出与既有条文高度相近的；
- 冲突提示：编号撞号、元信息缺失、同主题阈值数字不一致这类**可核对**的矛盾单列。
另附一份配套改动清单骨架（范围卡体例：风险类型候选/规则待定项/样本/金标/验收）。

"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from backend.app.parser import extract_text
from backend.app.policy_corpus import corpus_fingerprint
from backend.app.policy_rag import POLICY_DIR, PolicyHit, get_store, load_policies

DRAFTS_DIR = POLICY_DIR / "_drafts"

# 元信息行：「文件编号：P-15　　版本：V1.0　　生效日期：2026年9月14日」等，一行可有多项
_META_RE = re.compile(r"(文件编号|版本|生效日期|归口部门|适用范围)\s*[：:]\s*([^\s　]+)")
_META_KEYS = {
    "文件编号": "ref",
    "版本": "version",
    "生效日期": "effective_date",
    "归口部门": "owner",
    "适用范围": "scope",
}
# 条文头：行首「## 第X条 …」（与入库分条同一形态）
_ARTICLE_RE = re.compile(r"^#{1,4}\s*(第[一二三四五六七八九十百\d]+条.*)$", re.M)
# 阈值数字：百分比与月数（冲突检测只认这两类可核对的量）
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_MONTH_RE = re.compile(r"(\d+)\s*个月")

OVERLAP_HIGH = 0.80  # 余弦相似度 ≥ 此值 → 高度重叠（疑似重复或替代关系）
OVERLAP_MEDIUM = 0.70  # ≥ 此值 → 中等重叠（值得并读确认）；以下只记录不提示

# 复用同一个检索库：逐条新建 store 会重复打印"已有 N 条，跳过导入"并多花连接开销
_STORE_CACHE: dict[str, object] = {}


def _default_retriever():
    """默认检索器：进程内建一次向量库，返回 (query, k) -> list[PolicyHit]。"""
    def retrieve(query: str, k: int) -> list[PolicyHit]:
        if "store" not in _STORE_CACHE:
            store = get_store()
            store.insert(load_policies())
            _STORE_CACHE["store"] = store
        return _STORE_CACHE["store"].similarity_search(query, k=k)  # type: ignore[attr-defined]

    return retrieve


def parse_policy(text: str, source: str = "") -> dict:
    """解析政策文本：标题/元信息/条文列表；缺项留空，不猜内容。"""
    body = text or ""
    lines = [line.strip() for line in body.splitlines()]
    title = next((line.lstrip("# ").strip() for line in lines if line), "")
    # 适用范围可能折行，单独按段落抓（元信息行只抓「键：值」的单行部分）
    scope_block = re.search(r"适用范围[：:]\s*([\s\S]+?)(?:\n\s*\n|\n#)", body)
    meta = {name: value for name, value in _META_RE.findall(body[:800])}
    parsed = {
        "title": title,
        "source": source,
        "ref": meta.get("文件编号", "") or _ref_from_name(source),
        "version": meta.get("版本", ""),
        "effective_date": meta.get("生效日期", ""),
        "owner": meta.get("归口部门", ""),
        "scope": re.sub(r"\s+", " ", scope_block.group(1)).strip() if scope_block else "",
        "articles": _split_articles(body),
    }
    parsed["missing"] = [
        key for key in ("ref", "version", "effective_date", "owner", "scope") if not parsed[key]
    ]
    return parsed


def render_draft(parsed: dict) -> str:
    """按现有政策体例重排成草稿文本（缺的元信息写明「待填」，不替作者编）。"""
    def value(key: str) -> str:
        return parsed[key] if parsed[key] else "（待填）"

    lines = [
        f"# {parsed['title'] or '（待填标题）'}",
        "",
        f"文件编号：{value('ref')}　　版本：{value('version')}　　生效日期：{value('effective_date')}",
        f"归口部门：{value('owner')}",
        f"适用范围：{value('scope')}",
        "",
    ]
    for index, article in enumerate(parsed["articles"], start=1):
        # 分支：原文已有序号标题 → 沿用；没有就按顺序补「第X条」
        heading = article["heading"] or f"第{index}条"
        lines.extend([f"## {heading}", "", article["body"].strip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def find_overlaps(
    parsed: dict, retriever=None, top_k: int = 3
) -> list[dict]:
    """逐条检索现有政策，标出重叠程度（用向量余弦分，分越高越像）。

    retriever 可注入（(query, k) -> list[PolicyHit]），离线测试不必连向量库。
    """
    retriever = retriever or _default_retriever()
    overlaps: list[dict] = []
    for article in parsed["articles"]:
        query = f"{article['heading']} {article['body']}".strip()
        if not query:
            continue
        hits = _safe_retrieve(retriever, query, top_k)
        mapped = [
            {
                "policy_ref": hit.policy_ref,
                "source": hit.source,
                "score": round(float(hit.score), 3),
                "text_head": re.sub(r"\s+", "", hit.text)[:60],
                # 数字随命中一起带上：冲突检测要比的是条文里的阈值，不是标题
                "numbers": _numbers(hit.text),
            }
            for hit in hits
        ]
        best = max((item["score"] for item in mapped), default=0.0)
        # 分支：最高分不到提示线 → 只留一行记录，不占篇幅
        level = (
            "high" if best >= OVERLAP_HIGH
            else "medium" if best >= OVERLAP_MEDIUM
            else "low"
        )
        overlaps.append(
            {
                "article": article["heading"] or "(无标题)",
                "level": level,
                "numbers": _numbers(f"{article['heading']} {article['body']}"),
                "hits": mapped,
            }
        )
    return overlaps


def detect_conflicts(parsed: dict, overlaps: list[dict], policy_dir: Path | None = None) -> list[dict]:
    """列出**可核对**的矛盾：编号撞号、元信息缺失、同主题阈值数字不一致。"""
    conflicts: list[dict] = []
    existing = _existing_refs(policy_dir)
    # 分支：编号与现有政策撞号 → 新政策必须换号（直接冲突）
    if parsed["ref"] and parsed["ref"] in existing:
        conflicts.append({
            "kind": "编号重复",
            "detail": f"{parsed['ref']} 已被现有政策占用，新政策需要另编号",
        })
    # 分支：元信息缺失 → 入库前必须补齐（体例要求）
    if parsed["missing"]:
        conflicts.append({
            "kind": "元信息缺失",
            "detail": "缺：" + "、".join(parsed["missing"]),
        })
    conflicts.extend(_threshold_conflicts(parsed, overlaps))
    return conflicts


def build_checklist(parsed: dict, overlaps: list[dict], conflicts: list[dict]) -> str:
    """生成配套改动清单骨架（范围卡体例），供人补全口径与样本计划。"""
    high = [item for item in overlaps if item["level"] == "high"]
    medium = [item for item in overlaps if item["level"] == "medium"]
    lines = [
        f"# 新政策配套清单（草稿）：{parsed['title'] or parsed['source']}",
        "",
        f"- 政策库当前版本：{corpus_fingerprint()['version']}",
        f"- 条文数：{len(parsed['articles'])}",
        "",
        "## 一、重叠与冲突（机器初筛，需人工确认）",
        "",
    ]
    for item in high:
        lines.append(f"- 【高度重叠】{item['article']} → " + _hit_brief(item))
    for item in medium:
        lines.append(f"- 【中等重叠】{item['article']} → " + _hit_brief(item))
    for item in conflicts:
        lines.append(f"- 【{item['kind']}】{item['detail']}")
    if not high and not medium and not conflicts:
        lines.append("- 未发现可核对的重叠或冲突（仍需人工读一遍口径）")
    lines.extend([
        "",
        "## 二、待定项（人工填写）",
        "",
        "- 风险类型候选（risk_type）：",
        "- 判定口径与阈值（只认确定性判据，写清误报护栏）：",
        "- 与既有规则的去重/分工：",
        "",
        "## 三、样本与评测",
        "",
        "- 缺陷样本（正例）/ 正常对照样本（反例）：",
        "- 检索金标要补的查询：",
        "- 零新增命中回归范围：",
        "",
        "## 四、验收",
        "",
        "- 离线：现有语料零新增命中 / 检索金标对照 / 单测",
        "- 入库：`policy_admin --check` 核对后 `--sync --yes` 按单元增量写入",
    ])
    return "\n".join(lines) + "\n"


def run_assist(path: str | Path, retriever=None, out_dir: Path | None = None) -> dict:
    """跑一遍起稿流程：解析 → 重叠 → 冲突 → 落三份草稿，返回产物路径与摘要。"""
    source = Path(path)
    parsed = parse_policy(extract_text(source), source=source.name)
    overlaps = find_overlaps(parsed, retriever=retriever)
    conflicts = detect_conflicts(parsed, overlaps)
    target = out_dir or DRAFTS_DIR / f"{source.stem}_{datetime.now().strftime('%H%M%S')}"
    target.mkdir(parents=True, exist_ok=True)
    draft = target / f"{parsed['ref'] or source.stem}_draft.md"
    draft.write_text(render_draft(parsed), encoding="utf-8")
    (target / "overlaps.json").write_text(
        json.dumps(overlaps, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (target / "checklist.md").write_text(
        build_checklist(parsed, overlaps, conflicts), encoding="utf-8"
    )
    return {
        "parsed": parsed,
        "overlaps": overlaps,
        "conflicts": conflicts,
        "out_dir": target,
        "draft": draft,
    }


def _split_articles(text: str) -> list[dict]:
    """按「## 第X条」切条文；没有条文结构时整文当一条（与入库分条口径一致）。"""
    matches = list(_ARTICLE_RE.finditer(text or ""))
    # 分支：无条文结构 → 整文一条，避免把正文丢掉
    if not matches:
        return [{"heading": "", "body": (text or "").strip()}]
    return [
        {
            "heading": match.group(1).strip(),
            "body": text[match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip(),
        }
        for index, match in enumerate(matches)
    ]


def _ref_from_name(name: str) -> str:
    """文件名里的编号（P-XX）兜底，取不到返回空串。"""
    matched = re.match(r"(P-\d+)", name or "")
    return matched.group(1) if matched else ""


def _numbers(text: str) -> dict:
    """抽出可比较的阈值数字：百分比与月数（冲突检测只认这两类能直接对上的量）。"""
    return {
        "percent": sorted(set(_PERCENT_RE.findall(text or ""))),
        "months": sorted(set(_MONTH_RE.findall(text or ""))),
    }


def _existing_refs(policy_dir: Path | None = None) -> set[str]:
    """现有政策编号集合（按目录里的文件名取，与入库口径一致）。"""
    directory = policy_dir or POLICY_DIR
    return {
        ref
        for path in directory.glob("*.md")
        if (ref := _ref_from_name(path.name))
    }


def _threshold_conflicts(parsed: dict, overlaps: list[dict]) -> list[dict]:
    """同一主题上数字对不上：新条文写了 40%，重叠的既有条文写着 30% → 提示人工确认口径。"""
    conflicts: list[dict] = []
    for item in overlaps:
        # 分支：没有重叠命中 → 无从比较
        if not item["hits"]:
            continue
        new_percent = set(item.get("numbers", {}).get("percent", []))
        new_months = set(item.get("numbers", {}).get("months", []))
        for hit in item["hits"]:
            old_percent = set(hit.get("numbers", {}).get("percent", []))
            old_months = set(hit.get("numbers", {}).get("months", []))
            # 分支：两边百分比都存在且完全不同 → 口径可能不一致
            if new_percent and old_percent and not (new_percent & old_percent):
                conflicts.append({
                    "kind": "阈值不一致",
                    "detail": f"{item['article']} 写 {sorted(new_percent)}%，{hit['policy_ref']} 相近条文写 "
                              f"{sorted(old_percent)}%，需人工确认是补充还是覆盖",
                })
            # 分支：月数同理
            if new_months and old_months and not (new_months & old_months):
                conflicts.append({
                    "kind": "阈值不一致",
                    "detail": f"{item['article']} 写 {sorted(new_months)} 个月，{hit['policy_ref']} 相近条文写 "
                              f"{sorted(old_months)} 个月，需人工确认",
                })
    return conflicts


def _hit_brief(item: dict) -> str:
    """把一条重叠记录压成一行（取最高分的两个命中）。"""
    parts = [f"{hit['policy_ref']}({hit['score']})" for hit in item["hits"][:2]]
    return "、".join(parts) or "无命中"


def _safe_retrieve(retriever, query: str, k: int) -> list[PolicyHit]:
    """检索失败不阻断起稿：拿不到结果就按"无命中"处理。"""
    try:
        return list(retriever(query, k)) or []
    except Exception:  # noqa: BLE001
        return []


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：对一份或多份政策文件起稿，打印摘要与产物路径。"""
    parser = argparse.ArgumentParser(description="政策库运维助手（起稿，不改库）")
    parser.add_argument("paths", nargs="+", help="政策文件（md/txt/pdf/docx）")
    parser.add_argument("--out", type=Path, default=None, help="草稿输出目录（默认 data/policies/_drafts/）")
    parser.add_argument("--json", action="store_true", help="以 JSON 打印结果")
    args = parser.parse_args(argv)

    results = []
    for path in args.paths:
        result = run_assist(path, out_dir=(args.out / Path(path).stem) if args.out else None)
        results.append({
            "source": path,
            "ref": result["parsed"]["ref"],
            "articles": len(result["parsed"]["articles"]),
            "missing": result["parsed"]["missing"],
            "conflicts": result["conflicts"],
            "overlap_high": [item["article"] for item in result["overlaps"] if item["level"] == "high"],
            "out_dir": str(result["out_dir"]),
        })
        # 分支：非 JSON 模式给人读摘要
        if not args.json:
            print(f"\n== {path} ==")
            print(f"编号 {result['parsed']['ref'] or '（无）'} · 条文 {len(result['parsed']['articles'])} 条"
                  f" · 元信息缺失 {result['parsed']['missing'] or '无'}")
            for item in result["conflicts"]:
                print(f"  [{item['kind']}] {item['detail']}")
            for item in result["overlaps"]:
                # 分支：只打印中等以上重叠，低重叠不占篇幅
                if item["level"] != "low":
                    print(f"  [{item['level']}] {item['article']} → {_hit_brief(item)}")
            print(f"  草稿目录：{result['out_dir']}")
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
