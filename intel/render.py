"""渲染：同一份内容，四种粒度。

渠道能力差异决定内容结构，不是同一段文字换个包装：
  飞书卡片 —— 可放 6-8 条，纯阅读，不带任何按钮
  Telegram —— 一条事件一条消息，避免长文被 4096 字符截断
  Markdown —— 归档与 diff 用
  HTML     —— 当日完整报告，含淘汰原因

反馈只有控制台一个入口。「没用」要选原因（跑题/重复/太浅/标题党/不可信/太旧），
六个原因对应六档惩罚，手机上点两下按钮做不到这件事——所以卡片里不再放按钮，
只留一行地址。

语言层（i18n）只翻译「我们自己写的字」：标题、字段名、淘汰原因、兜底理由。
摘要正文由模型按 output.language 生成；规则模式下无法翻译原文，保留原语言，
不假装是译文。

所有用户可见文本都做 HTML 转义：标题来自第三方源，不能假设它干净。
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime
from typing import Any

from .i18n import change_label, gate_label, resolve_lang, t
from .models import Item

# 控制台反馈页的默认地址；端口变了改这里或传 console_url
CONSOLE_REVIEW_URL = "http://127.0.0.1:8765/#review"


def human_reason(reason: str, lang: str = "zh") -> str:
    """把内部判定码翻成人话。

    用户看到的应该是「新颖性 2.1 低于下限 3.0」，而不是 `gate:novelty=2.1<3.0`。
    但原始码仍保留在库里，排查时按码查更准。
    """
    if not reason:
        return "—"
    fixed = {
        "evergreen:no_news_signal": t("evergreen", lang),
        "no_gates": t("no_gates", lang),
        "coarse_rank": t("coarse_cut", lang),
        "folded_into_another_cluster": t("folded", lang),
    }
    if reason in fixed:
        return fixed[reason]
    if reason.startswith("gate:"):
        m = re.match(r"gate:(\w+)=([\d.]+)<([\d.]+)", reason)
        if m:
            return t("gate_low", lang, name=gate_label(m.group(1), lang),
                     value=m.group(2), floor=m.group(3))
    if reason.startswith("blocked_keyword:"):
        return t("blocked", lang, word=reason.split(":", 1)[1])
    if reason.startswith("source_quota"):
        return t("quota", lang)
    if reason.startswith("ai_triage:"):
        return t("ai_triage", lang, why=reason.split(":", 1)[1])
    return reason


def _bar(lang: str) -> str:
    return "｜" if lang != "en" else " · "


def _colon(lang: str) -> str:
    """中文用全角冒号，英文用半角加空格——混用会让英文版面看着像机器翻译。"""
    return "：" if lang != "en" else ": "


def _esc(text: str) -> str:
    return _html.escape(text or "", quote=True)


def _date_label() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _source_label(item: Item, lang: str = "zh") -> str:
    extra = len(item.also_reported)
    base = item.source or "unknown"
    if not extra:
        return base
    return f"{base} +{extra}" if lang == "en" else f"{base} 等 {extra + 1} 家"


def _change_chip(item: Item, lang: str) -> str:
    label = change_label(item.change_type, lang)
    return f"[{label}] " if label else ""


def _pack_md(pack: dict[str, Any] | None, lang: str = "zh") -> list[str]:
    """选题区块的 Markdown 版。pack 可能来自模型，也可能来自规则兜底。"""
    if not pack:
        return []
    lines = [f"## {t('pack_title', lang)}", ""]
    if pack.get("title_options"):
        lines.append(f"**{t('pack_titles', lang)}**{_colon(lang)}"
                     + (" / ".join(pack["title_options"])))
        lines.append("")
    if pack.get("angle"):
        lines.append(f"**{t('pack_angle', lang)}**{_colon(lang)}{pack['angle']}")
        lines.append("")
    if pack.get("basis"):
        lines.append(f"**{t('pack_basis', lang)}**{_colon(lang)}{pack['basis']}")
        lines.append("")
    for part in pack.get("outline") or []:
        lines.append(f"### {part.get('section', '')}")
        for point in part.get("points") or []:
            lines.append(f"- {point}")
        lines.append("")
    if pack.get("references"):
        lines.append(f"**{t('pack_refs', lang)}**{_colon(lang)}")
        for ref in pack["references"][:5]:
            lines.append(f"- [{ref.get('title', '')}]({ref.get('url', '')})")
        lines.append("")
    if pack.get("source") == "rule":
        lines.append(f"> {t('pack_rule', lang)}")
        lines.append("")
    return lines


def markdown(items: list[Item], title: str, lang: str = "zh",
             pack: dict[str, Any] | None = None) -> str:
    lines = [f"# {title}", "", f"> {_date_label()} · {t('count', lang, n=len(items))}", ""]
    for idx, item in enumerate(items, 1):
        lines.append(f"## {idx}. {item.shown_title}")
        lines.append("")
        meta = [f"{t('source', lang)}{_colon(lang)}{_source_label(item, lang)}{_bar(lang)}"
                f"{item.published_at[:16].replace('T', ' ')}"]
        chip = _change_chip(item, lang).strip("[] ")
        if chip:
            meta.append(chip)
        lines.append("- " + f"{_bar(lang)}".join(meta))
        lines.append("")
        lines.append(item.summary)
        lines.append("")
        for point in item.points[:3]:
            lines.append(f"- {point}")
        if item.points:
            lines.append("")
        if item.also_reported:
            others = "、".join(r["source"] for r in item.also_reported[:4])
            lines.append(f"{t('also_reported', lang, n=len(item.also_reported))}{_colon(lang)}{others}")
            lines.append("")
        lines.append(f"[{t('original', lang)}]({item.url})")
        lines.append("")
    lines.extend(_pack_md(pack, lang))
    return "\n".join(lines)


def feishu_card(items: list[Item], title: str, lang: str = "zh",
                pack: dict[str, Any] | None = None,
                tasks: list[dict[str, Any]] | None = None,
                console_url: str = "") -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for item in items[:10]:
        head = f"**[{_esc(item.shown_title)}]({item.url})**"
        stamp = (item.published_at or "")[:16].replace("T", " ")
        bits = [_source_label(item, lang), stamp, change_label(item.change_type, lang)]
        meta = (f"<font color='grey'>"
                f"{_esc(' · '.join(b for b in bits if b))}</font>")
        body = f"{head}\n{meta}\n{_esc(item.summary)}"
        for point in item.points[:3]:
            body += f"\n· {_esc(point)}"
        if item.also_reported:
            body += f"\n<font color='grey'>{t('also_reported', lang, n=len(item.also_reported))}</font>"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": body}})
        elements.append({"tag": "hr"})
    if elements:
        elements.pop()

    if pack:
        lines = [f"**{t('pack_title', lang)}**"]
        if pack.get("title_options"):
            lines.append(f"{t('pack_titles', lang)}{_colon(lang)}"
                         + " / ".join(pack["title_options"][:2]))
        if pack.get("angle"):
            lines.append(f"{t('pack_angle', lang)}{_colon(lang)}{_esc(pack['angle'])}")
        if pack.get("basis"):
            lines.append(f"<font color='grey'>{t('pack_basis', lang)}{_colon(lang)}"
                         f"{_esc(pack['basis'])}</font>")
        for part in (pack.get("outline") or [])[:4]:
            lines.append(f"\n**{_esc(part.get('section', ''))}**")
            for point in (part.get("points") or [])[:3]:
                lines.append(f"· {_esc(point)}")
        if pack.get("references"):
            ref = pack["references"][0]
            lines.append(f"\n[{t('pack_refs', lang)}]({ref.get('url', '')})")
        if pack.get("source") == "rule":
            lines.append(f"<font color='grey'>{t('pack_rule', lang)}</font>")
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})

    created = [r for r in (tasks or []) if r.get("ok") and r.get("count")]
    failed = [r for r in (tasks or []) if not r.get("ok")]
    if created or failed:
        bits = []
        if created:
            bits.append(f"{t('task_created', lang)} {len(created)}")
        if failed:
            bits.append(f"{t('task_failed', lang)} {len(failed)}")
        elements.append({"tag": "note", "elements": [
            {"tag": "plain_text", "content": " · ".join(bits)},
        ]})

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": t("feedback_console", lang,
                                   url=console_url or CONSOLE_REVIEW_URL)}],
    })
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": t("note_footer", lang, date=_date_label(), n=len(items))}],
    })
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
            "elements": elements,
        },
    }


def dingtalk_markdown(items: list[Item], title: str, lang: str = "zh",
                      pack: dict[str, Any] | None = None,
                      tasks: list[dict[str, Any]] | None = None,
                      limit: int = 3400,
                      console_url: str = "") -> list[dict[str, Any]]:
    """钉钉自定义机器人：一个 markdown 消息承载整份日报，超长才分段。

    钉钉的 markdown 是子集——`####` 标题、`**粗体**`、`>` 引用、`-` 列表、
    `[文字](url)` 都能用，表格和分割线不稳定（部分客户端会把 `---` 渲染成字面量），
    所以分节只靠标题层级 + 空行。单条上限约 20000 字节，中文按 3 字节算，
    这里用 3400 字符做保守阈值。
    """
    blocks: list[str] = []
    for idx, item in enumerate(items[:8], 1):
        head = f"#### {idx}. {item.shown_title}"
        stamp = (item.published_at or "")[:16].replace("T", " ")
        bits = [_source_label(item, lang), stamp, change_label(item.change_type, lang)]
        parts = [head, f"> {' · '.join(b for b in bits if b)}", item.summary]
        if item.points:
            parts.append("\n".join(f"- {p}" for p in item.points[:3]))
        if item.also_reported:
            parts.append(f"> {t('also_reported', lang, n=len(item.also_reported))}")
        parts.append(f"[{t('original', lang)}]({item.url})")
        blocks.append("\n\n".join(parts))

    if pack:
        lines = [f"**{t('pack_title', lang)}**"]
        if pack.get("title_options"):
            lines.append(f"{t('pack_titles', lang)}{_colon(lang)}"
                         + " / ".join(pack["title_options"][:2]))
        if pack.get("angle"):
            lines.append(f"{t('pack_angle', lang)}{_colon(lang)}{pack['angle']}")
        if pack.get("basis"):
            lines.append(f"> {t('pack_basis', lang)}{_colon(lang)}{pack['basis']}")
        for part in (pack.get("outline") or [])[:4]:
            lines.append(f"**{part.get('section', '')}**")
            lines.extend(f"- {point}" for point in (part.get("points") or [])[:3])
        if pack.get("references"):
            ref = pack["references"][0]
            lines.append(f"[{t('pack_refs', lang)}]({ref.get('url', '')})")
        if pack.get("source") == "rule":
            lines.append(f"> {t('pack_rule', lang)}")
        blocks.append("\n\n".join(lines))

    created = [r for r in (tasks or []) if r.get("ok") and r.get("count")]
    failed = [r for r in (tasks or []) if not r.get("ok")]
    if created or failed:
        bits = []
        if created:
            bits.append(f"{t('task_created', lang)} {len(created)}")
        if failed:
            bits.append(f"{t('task_failed', lang)} {len(failed)}")
        blocks.append("> " + " · ".join(bits))

    footer = f"> {t('note_footer', lang, date=_date_label(), n=len(items))}"
    blocks.append("> " + t("feedback_console", lang,
                           url=console_url or CONSOLE_REVIEW_URL))
    header = f"### {title}\n\n> {_date_label()} · {t('count', lang, n=len(items))}"

    messages: list[str] = []
    current = header
    for block in blocks:
        if len(current) + len(block) + 2 > limit and current != header:
            messages.append(current)
            current = f"{header}（{len(messages) + 1}）"
        current += "\n\n" + block
    current += "\n\n" + footer
    messages.append(current)
    return [{"title": title[:32], "text": body} for body in messages]


def telegram_chunks(items: list[Item], title: str, limit: int = 3800,
                    lang: str = "zh") -> list[str]:
    """一条事件一个消息段；按字符预算切块，不在事件中间截断。"""
    blocks: list[str] = []
    for item in items:
        parts = [
            f"<b>{_esc(item.shown_title)}</b>",
            _esc(item.summary),
        ]
        if item.points:
            parts.append("\n".join(f"· {_esc(p)}" for p in item.points[:3]))
        meta = f"{_esc(_source_label(item, lang))}"
        parts.append(f"<a href=\"{item.url}\">{t('original', lang)}</a>　<code>{meta}</code>")
        blocks.append("\n".join(parts))

    chunks: list[str] = []
    current = f"<b>{_esc(title)}</b>"
    for block in blocks:
        if len(current) + len(block) + 2 > limit:
            chunks.append(current)
            current = block
        else:
            current += "\n\n" + block
    if current:
        chunks.append(current)
    return chunks


def _pack_html(pack: dict[str, Any] | None, lang: str) -> str:
    if not pack:
        return ""
    parts = [f"<article class='card pack'><div class='row'><span class='src'>"
             f"{_esc(t('pack_title', lang))}</span>"
             f"<span class='conf'>{_esc(pack.get('source', ''))}</span></div>"]
    if pack.get("title_options"):
        parts.append("<h2>" + _esc(pack["title_options"][0]) + "</h2>")
    if pack.get("angle"):
        parts.append(f"<p class='sum'><strong>{_esc(t('pack_angle', lang))}{_colon(lang)}</strong>"
                     f"{_esc(pack['angle'])}</p>")
    if pack.get("basis"):
        parts.append(f"<p class='basis'>{_esc(t('pack_basis', lang))}{_colon(lang)}"
                     f"{_esc(pack['basis'])}</p>")
    for part in pack.get("outline") or []:
        points = "".join(f"<li>{_esc(p)}</li>" for p in part.get("points") or [])
        parts.append(f"<h4>{_esc(part.get('section', ''))}</h4><ul class='outline'>{points}</ul>")
    if pack.get("references"):
        refs = "".join(
            f"<li><a href='{_esc(r.get('url', ''))}'>{_esc(r.get('title', ''))}</a></li>"
            for r in pack["references"][:5]
        )
        parts.append(f"<h4>{_esc(t('pack_refs', lang))}</h4><ul class='outline'>{refs}</ul>")
    if pack.get("source") == "rule":
        parts.append(f"<p class='muted'>{_esc(t('pack_rule', lang))}</p>")
    parts.append("</article>")
    return "".join(parts)


def html_digest(items: list[Item], rejected: list[Item], title: str,
                lang: str = "zh", pack: dict[str, Any] | None = None) -> str:
    rows = []
    for item in items:
        also = ""
        if item.also_reported:
            names = "、".join(_esc(r["source"]) for r in item.also_reported[:4])
            also = (f"<div class='also'>{t('also_reported', lang, n=len(item.also_reported))}"
                    f"{_colon(lang)}{names}</div>")
        pts = "".join(f"<li>{_esc(p)}</li>" for p in item.points[:3])
        pts_html = f"<ul class='pts'>{pts}</ul>" if pts else ""
        stamp = (item.published_at or "")[:16].replace("T", " ")
        label = change_label(item.change_type, lang)
        chip = f"<span class='chg'>{_esc(label)}</span>" if label else ""
        rows.append(f"""
        <article class="card">
          <div class="row"><span class="src">{_esc(_source_label(item, lang))}</span>
            <span class="conf">{_esc(stamp)}</span></div>
          <h2><a href="{item.url}">{_esc(item.shown_title)}</a></h2>
          <p class="sum">{_esc(item.summary)}</p>
          {pts_html}
          <div class="foot">{chip}<a href="{item.url}">{t('original', lang)}</a></div>
          {also}
        </article>""")

    reject_rows = []
    for item in rejected[:40]:
        reject_rows.append(
            f"<tr><td>{_esc(item.title[:58] + ('…' if len(item.title) > 58 else ''))}</td>"
            f"<td>{_esc(human_reason(item.reject_reason, lang))}</td>"
            f"<td>{item.score:.1f}</td></tr>"
        )
    reject_html = (
        f"<table><thead><tr><th>{t('col_title', lang)}</th><th>{t('col_reason', lang)}</th>"
        f"<th>{t('col_score', lang)}</th></tr></thead><tbody>"
        + "".join(reject_rows) + "</tbody></table>"
        if reject_rows else f"<p class='muted'>{t('empty', lang)}</p>"
    )

    return f"""<!DOCTYPE html>
<html lang="{'en' if lang == 'en' else 'zh-CN'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>
  :root {{ --bg:#0f1319; --card:#171d27; --line:#252d3b; --fg:#dfe5ef; --dim:#8b95a7; --accent:#5b9bd5; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.65 -apple-system,"Segoe UI","Noto Sans SC",sans-serif; }}
  .wrap {{ max-width:780px; margin:0 auto; padding:40px 20px 64px; }}
  header h1 {{ font-size:22px; margin:0 0 6px; font-weight:600; }}
  header p {{ margin:0 0 28px; color:var(--dim); font-size:13px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:18px 20px; margin-bottom:14px; }}
  .row {{ display:flex; justify-content:space-between; font-size:12px; color:var(--dim); }}
  .conf {{ font-variant-numeric:tabular-nums; }}
  .card h2 {{ font-size:16px; margin:8px 0 10px; line-height:1.45; font-weight:600; }}
  .card h2 a {{ color:var(--fg); text-decoration:none; }}
  .card h2 a:hover {{ color:var(--accent); }}
  .sum {{ margin:0 0 10px; color:#c3cbd9; }}
  .pts {{ margin:0 0 12px; padding-left:18px; color:#c3cbd9; font-size:14px; }}
  .pts li {{ margin:3px 0; }}
  .foot {{ display:flex; gap:12px; align-items:center; font-size:12px; color:var(--dim); }}
  .foot a {{ color:var(--accent); text-decoration:none; }}
  .foot a:hover {{ text-decoration:underline; }}
  .chg {{ font-size:11px; padding:1px 6px; border:1px solid var(--line);
          border-radius:4px; color:var(--dim); }}
  .basis {{ margin:0 0 12px; color:#b9c3d4; font-size:13px; }}
  .gates {{ display:flex; gap:8px; flex-wrap:wrap; }}
  .g {{ font:11px/1 ui-monospace,monospace; color:var(--dim);
        border:1px solid var(--line); border-radius:4px; padding:4px 6px; }}
  .also {{ margin-top:10px; font-size:12px; color:var(--dim); }}
  .pack {{ border-color:#3a4a63; }}
  .pack h4 {{ font-size:13px; margin:14px 0 6px; color:var(--accent); font-weight:600; }}
  .outline {{ margin:0; padding-left:18px; color:#c3cbd9; font-size:14px; }}
  .outline li {{ margin:3px 0; }}
  .brief {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:24px; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:12px 16px; min-width:104px; }}
  .stat b {{ display:block; font-size:20px; font-variant-numeric:tabular-nums; }}
  .stat span {{ font-size:11px; color:var(--dim); letter-spacing:.06em; }}
  .topic {{ margin-bottom:22px; }}
  .topic h3 {{ margin:0 0 8px; font-size:13px; color:var(--accent); text-transform:none;
               letter-spacing:0; }}
  .topic .item {{ padding:8px 0; border-bottom:1px solid #1d2430; }}
  .topic .item a {{ color:var(--fg); text-decoration:none; font-size:14px; }}
  .topic .item .line {{ margin:5px 0 0; font-size:12px; color:#a8b1c0; }}
  .topic .item .stamp {{ margin:3px 0 0; font-size:11px; color:var(--dim);
                         font-variant-numeric:tabular-nums; }}
  h3 {{ font-size:14px; color:var(--dim); margin:36px 0 12px; font-weight:500;
        text-transform:uppercase; letter-spacing:.08em; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; color:var(--dim); font-weight:500; padding:6px 8px;
        border-bottom:1px solid var(--line); }}
  td {{ padding:6px 8px; border-bottom:1px solid #1d2430; color:#a8b1c0; }}
  .muted {{ color:var(--dim); font-size:13px; }}
</style></head>
<body><div class="wrap">
  <header>
    <h1>{_esc(title)}</h1>
    <p>{t('head_meta', lang, date=_date_label(), kept=len(items), rejected=len(rejected))}</p>
  </header>
  {''.join(rows) if rows else f'<p class="muted">{t("empty", lang)}</p>'}
  {_pack_html(pack, lang)}
  <h3>{t('rejected_title', lang)}</h3>
  {reject_html}
</div></body></html>"""


def heartbeat_text(scanned: int, cfg: dict[str, Any], reason: str = "",
                   lang: str | None = None) -> str:
    lang = lang or resolve_lang(cfg)
    floor = float(cfg["filter"]["gates"]["relevance"]["floor"])
    tail = f"（{reason}）" if reason and lang == "zh" else (f" ({reason})" if reason else "")
    return t("heartbeat", lang, scanned=scanned, tail=tail, floor=floor)


def digest_title(lang: str, moment: datetime | None = None) -> str:
    moment = moment or datetime.now()
    label = t("daily", lang)
    return f"{t('digest_title', lang)} · {moment.strftime('%m-%d %H:%M')}" if lang == "zh" \
        else f"{t('digest_title', lang)} · {moment.strftime('%b %d, %H:%M')}"


# ---------------------------------------------------------------- 晚间简报
def briefing_html(summary: dict[str, Any], lang: str = "zh") -> str:
    """晚间简报：一轮不看会漏，一天不看会慌——这份是「今天到底发生了什么」的兜底。"""
    stats = "".join(
        f"<div class='stat'><b>{_esc(str(value))}</b><span>{_esc(label)}</span></div>"
        for label, value in (
            ("轮次" if lang == "zh" else "runs", summary.get("runs", 0)),
            ("扫描" if lang == "zh" else "scanned", summary.get("fetched", 0)),
            ("入选" if lang == "zh" else "kept", summary.get("kept", 0)),
            ("推送" if lang == "zh" else "pushed", summary.get("pushed", 0)),
            ("待办" if lang == "zh" else "tasks", len(summary.get("tasks") or [])),
        )
    )

    blocks: list[str] = []
    for topic in summary.get("topics") or []:
        rows = []
        for it in (topic.get("items") or [])[:6]:
            digest = _esc((it.get("summary") or "").strip())
            if len(digest) > 90:
                digest = digest[:90] + "…"
            stamp = _esc((it.get("first_seen") or "")[:16].replace("T", " "))
            rows.append(
                f"<div class='item'><a href='{_esc(it.get('url', ''))}'>"
                f"{_esc(it.get('title', ''))}</a>"
                + (f"<p class='line'>{digest}</p>" if digest else "")
                + f"<p class='stamp'>{stamp}</p></div>"
            )
        if rows:
            blocks.append(
                f"<section class='topic'><h3>{_esc(topic.get('name', ''))}"
                f" · {len(topic.get('items') or [])}</h3>{''.join(rows)}</section>"
            )
        else:
            blocks.append(
                f"<section class='topic'><h3>{_esc(topic.get('name', ''))} · 0</h3>"
                f"<p class='muted'>{t('empty', lang)}</p></section>"
            )
    if not blocks:
        blocks.append(f"<p class='muted'>{t('brief_empty', lang)}</p>")

    task_rows = "".join(
        f"<tr><td>{_esc(task.get('summary', ''))}</td>"
        f"<td>{(task.get('created_at') or '')[:16].replace('T', ' ')}</td></tr>"
        for task in (summary.get("tasks") or [])
    )
    task_html = (f"<h3>{t('brief_tasks', lang, n=len(summary.get('tasks') or []))}</h3>"
                 f"<table><tbody>{task_rows}</tbody></table>"
                 f"<p class='muted'>{t('brief_tasks_hint', lang)}</p>") if task_rows else ""

    quiet = summary.get("silenced") or 0
    quiet_html = f"<p class='muted'>{t('brief_quiet', lang, n=quiet)}</p>" if quiet else ""

    return f"""<!DOCTYPE html>
<html lang="{'en' if lang == 'en' else 'zh-CN'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(t('briefing_title', lang))} · {_esc(str(summary.get('date', '')))}</title>
<style>
  :root {{ --bg:#0f1319; --card:#171d27; --line:#252d3b; --fg:#dfe5ef; --dim:#8b95a7; --accent:#5b9bd5; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.65 -apple-system,"Segoe UI","Noto Sans SC",sans-serif; }}
  .wrap {{ max-width:780px; margin:0 auto; padding:40px 20px 64px; }}
  header h1 {{ font-size:22px; margin:0 0 6px; font-weight:600; }}
  header p {{ margin:0 0 24px; color:var(--dim); font-size:13px; }}
  .brief {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:24px; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:12px 16px; min-width:104px; }}
  .stat b {{ display:block; font-size:20px; font-variant-numeric:tabular-nums; }}
  .stat span {{ font-size:11px; color:var(--dim); letter-spacing:.06em; }}
  .topic {{ margin-bottom:22px; }}
  .topic h3 {{ margin:0 0 8px; font-size:13px; color:var(--accent); }}
  .topic .item {{ padding:8px 0; border-bottom:1px solid #1d2430; }}
  .topic .item a {{ color:var(--fg); text-decoration:none; font-size:14px; }}
  .topic .item .line {{ margin:5px 0 0; font-size:12px; color:#a8b1c0; }}
  .topic .item .stamp {{ margin:3px 0 0; font-size:11px; color:var(--dim);
                         font-variant-numeric:tabular-nums; }}
  h3 {{ font-size:13px; color:var(--dim); margin:28px 0 10px; font-weight:500;
        text-transform:uppercase; letter-spacing:.08em; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  td {{ padding:6px 8px; border-bottom:1px solid #1d2430; color:#a8b1c0; }}
  .muted {{ color:var(--dim); font-size:13px; }}
</style></head>
<body><div class="wrap">
  <header>
    <h1>{t('briefing_title', lang)} · {_esc(str(summary.get('date', '')))}</h1>
    <p>{t('brief_meta', lang, date=_esc(str(summary.get('date', ''))),
         runs=summary.get('runs', 0), fetched=summary.get('fetched', 0),
         kept=summary.get('kept', 0), pushed=summary.get('pushed', 0))}</p>
  </header>
  <div class="brief">{stats}</div>
  {quiet_html}
  <h3>{t('brief_topics', lang)}</h3>
  {''.join(blocks)}
  {task_html}
</div></body></html>"""


def briefing_card(summary: dict[str, Any], lang: str = "zh") -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": t(
            "brief_meta", lang, date=str(summary.get("date", "")),
            runs=summary.get("runs", 0), fetched=summary.get("fetched", 0),
            kept=summary.get("kept", 0), pushed=summary.get("pushed", 0))}},
    ]
    quiet = summary.get("silenced") or 0
    if quiet:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"<font color='grey'>{t('brief_quiet', lang, n=quiet)}</font>"}})

    topics = summary.get("topics") or []
    if not any(tp.get("items") for tp in topics):
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            t("brief_empty", lang)}})
    for topic in topics:
        items = (topic.get("items") or [])[:3]
        if not items:
            continue
        lines = [f"**{_esc(topic.get('name', ''))}**"]
        for it in items:
            lines.append(f"· [{_esc(it.get('title', ''))}]({it.get('url', '')})")
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})

    tasks = summary.get("tasks") or []
    if tasks:
        names = "、".join(_esc(tk.get("summary", ""))[:18] for tk in tasks[:3])
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"{t('brief_tasks', lang, n=len(tasks))}{_colon(lang)}{names}"}})
    if summary.get("next"):
        elements.append({"tag": "note", "elements": [
            {"tag": "plain_text", "content": t("brief_next", lang, topics=summary["next"])},
        ]})
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "turquoise",
                       "title": {"tag": "plain_text",
                                 "content": f"{t('briefing_title', lang)} · {summary.get('date', '')}"}},
            "elements": elements,
        },
    }
