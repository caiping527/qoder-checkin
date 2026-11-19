#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日积分播报：汇总各账号 result.json + 领取账本，生成榜单并推一条到企业微信。

⚠️ 到期口径（2026-11-19 修正）：
Qoder 有两条独立的到期线，绝不能合成一个日期——
  · 套餐额度（userQuota）：随本周期结束日 expiresAt 整批到期/刷新；
  · 签到资源包（addOnQuota 等）：每笔领取自领取之日起 30 天内有效、各自滚动到期
    （官方《每日领取 100 Credits》规则）。过去的写法把「套餐+资源包合计」
    统一挂在周期到期日上，会出现「每天签到却同一天过期」的错觉，已修正。

数据来源：
  · 各 checkin job 上传的 result.json（artifact）：当前积分 / 套餐剩余 /
    资源包剩余 / 本周期到期日；
  · 仓库 ledger/claims.json（merge_ledger.py 维护）：每笔领取的时刻、分数、
    30 天后的到期日 → 逐笔到期是精确的（记账启用后）。

用法：
  python daily_report.py _report --title Qoder
  python daily_report.py _report --file ledger/claims.json
开关：RECOMMEND_NOTIFY=0 / off / false / no / none / disabled 关闭推送
通道：WECOM_WEBHOOK（群机器人完整 webhook 或仅 key）、WECOM_APP（corpid,corpsecret,touser,agentid）
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

QYAPI = "https://qyapi.weixin.qq.com/cgi-bin"
BEIJING = timezone(timedelta(hours=8))
DEFAULT_LEDGER = os.path.join("ledger", "claims.json")
LOT_SIZE = 100  # 官方每天 100 Credits；估算历史笔数用


def truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def num(v):
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def load_ledger(path):
    """读领取账本；不存在或损坏都当空账本，播报不能因它失败。"""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return []
    claims = doc.get("claims") if isinstance(doc, dict) else None
    return claims if isinstance(claims, list) else []


def load_rows(root):
    """读取所有 result.json，一个账号一行。缺数据的账号也保留一行。"""
    rows = []
    for path in sorted(glob.glob(os.path.join(root, "**", "result.json"), recursive=True)):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        detail = data.get("detail")
        exp = {}
        if isinstance(detail, dict) and isinstance(detail.get("expiry"), dict):
            exp = detail["expiry"]
        elif isinstance(data.get("expiry"), dict):
            exp = data["expiry"]
        # 优先用脚本写好的账号标签；没有则退回 artifact 目录名，并去掉
        # 「wb-expiry- / qoder-expiry- / expiry-」前缀，避免显示成 wb-expiry-9653
        dir_name = os.path.basename(os.path.dirname(path))
        fallback = re.sub(r"^(?:wb|qoder|raccoon)?-?expiry-", "", dir_name)
        account = str(data.get("account") or fallback or dir_name or "未命名账号")
        status = str(data.get("status") or data.get("outcome") or "unknown")
        message = str(data.get("message") or data.get("msg") or "")
        err = str(exp.get("error") or "")
        credits = num(exp.get("credits")) if not err else 0.0
        # 新口径：套餐剩余与资源包剩余分开；老数据（无 addon_remaining）
        # 按旧口径回退：全部算套餐，资源包记 0，不会凭空出现错误拆分。
        if "addon_remaining" in exp and not err:
            addon = num(exp.get("addon_remaining"))
            plan = num(exp.get("earliest_remaining"))
        else:
            addon = 0.0
            plan = num(exp.get("earliest_remaining")) if not err else 0.0
        days = exp.get("days_left")
        rows.append({
            "account": account,
            "uid": str(data.get("uid") or ""),
            "status": status,
            "message": message,
            "has_data": bool(exp) and not err and credits > 0,
            "credits": credits,
            "plan_remaining": max(0.0, plan),
            "addon_remaining": max(0.0, addon),
            "expire_date": str(exp.get("earliest_expire_date") or ""),
            "days_left": days if isinstance(days, (int, float)) else None,
            "use_by": str(exp.get("use_by") or ""),
            "label": str(exp.get("label") or "开始过期"),
            "error": err,
        })
    # 与 AiRoute「积分先到期先用」一致：到期日近的在前；同日则套餐临期多的在前；
    # 再按总积分、账号名稳定排序。
    rows.sort(key=lambda r: (r["days_left"] if r["days_left"] is not None else 999,
                             -r["plan_remaining"], -r["credits"], r["account"]))
    return rows


def ledger_for(row, claims, today):
    """该账号「还没过期」的领取笔：优先按 uid 匹配，回退按账号名（尾号）。"""
    keys = [k for k in (row.get("uid"), row.get("account")) if k]
    hits = []
    for c in claims:
        if not isinstance(c, dict):
            continue
        exp = str(c.get("expireAt") or "")
        if exp and exp < today:
            continue  # 已过期的不再提醒
        owner = str(c.get("uid") or "") or str(c.get("account") or "")
        if owner in keys or str(c.get("account") or "") == row.get("account"):
            hits.append(c)
    hits.sort(key=lambda c: (str(c.get("expireAt") or ""), str(c.get("claimedAt") or "")))
    return hits


def addon_estimate(addon_remaining, today):
    """记账启用前的资源包：按 100 分/笔、连续领取估算笔数与最早到期日。

    只是兜底展示（明确标注「估算」）；记账跑起来后以账本为准。
    today 允许传 date 或 "YYYY-MM-DD" 字符串（两种调用点都有）。
    """
    if addon_remaining <= 0:
        return None
    base = today if isinstance(today, date) else date.fromisoformat(str(today))
    lots = max(1, int(round(addon_remaining / float(LOT_SIZE))))
    earliest = base - timedelta(days=lots - 1) + timedelta(days=30)
    return {"lots": lots, "earliest": earliest.isoformat()}


def expiry_lines(r, claims, today):
    """一个账号的到期说明（纯文本多行，手机上看得清）。"""
    if not r["has_data"]:
        if r["error"]:
            return ["积分数据未取到（%s）" % r["error"][:50]]
        return ["积分数据未取到（本次签到可能失败，见下方告警）"]

    lines = []
    # ① 套餐额度：随本周期整批到期
    if r["expire_date"]:
        days = r["days_left"] if r["days_left"] is not None else "?"
        head = "套餐额度：%g 分于 %s 到期（剩 %s 天）" % (
            r["plan_remaining"], r["expire_date"], days)
        if isinstance(r["days_left"], (int, float)) and r["days_left"] <= 1:
            head += "；⚠️ 今明两天就到期，今天就用掉"
        elif r["use_by"] and r["plan_remaining"] > 0:
            head += "；建议 %s 前用掉，提前一天" % r["use_by"]
        elif r["plan_remaining"] <= 0:
            head += "；套餐已用完"
        lines.append(head)
    else:
        lines.append("套餐额度：未标注到期日")

    # ② 签到资源包：每笔 30 天各自滚动到期（账本精确，历史估算）
    addon = r["addon_remaining"]
    hits = ledger_for(r, claims, today)
    if addon <= 0 and not hits:
        lines.append("签到资源包：0 分")
        return lines
    if hits:
        booked = sum(num(c.get("credit")) for c in hits)
        first = hits[0]
        first_day = str(first.get("expireAt") or "")
        same_day = [c for c in hits if str(c.get("expireAt") or "") == first_day]
        same_amount = sum(num(c.get("credit")) for c in same_day)
        part = "签到资源包：%g 分，账本 %d 笔，最早 %s 到期 %g 分" % (
            addon, len(hits), first_day or "?", same_amount)
        if addon - booked > 0.01:
            est = addon_estimate(addon - booked, today)
            if est:
                part += "；另有 %g 分属记账前（约 %d 笔，最早约 %s）" % (
                    addon - booked, est["lots"], est["earliest"])
        lines.append(part)
    else:
        est = addon_estimate(addon, today)
        if est:
            lines.append("签到资源包：%g 分，约 %d 笔（记账未覆盖，估算），最早约 %s 到期" % (
                addon, est["lots"], est["earliest"]))
        else:
            lines.append("签到资源包：%g 分，到期日未记账" % addon)
    return lines


def build_text(rows, title, claims, run_url=None):
    """企微纯文本，不用 Markdown 符号，手机上看得清。"""
    now = datetime.now(BEIJING)
    today = now.date().isoformat()
    lines = ["【%s · 每日积分播报】%s" % (title, now.strftime("%Y-%m-%d")),
             "每个号的当前积分，以及积分什么时候到期、该提前哪天用掉：", ""]
    if not rows:
        lines.append("本次没有收到任何账号的积分数据，请查看运行详情。")
        if run_url:
            lines.append("运行详情：%s" % run_url)
        return "\n".join(lines)
    # 缺口可见：少一个号时，数量本身就能提醒（小浣熊播报同一处理）
    bad = [r for r in rows if not r["has_data"]]
    lines.append("本次收到 %d 个账号的积分数据%s" % (
        len(rows), ("，其中 %d 个未取到（见上方）" % len(bad)) if bad else ""))
    lines.append("")
    for i, r in enumerate(rows, 1):
        lines.append("%d. %s" % (i, r["account"]))
        if r["has_data"]:
            if r["addon_remaining"] > 0:
                lines.append("   当前积分：%g 分（套餐 %g + 资源包 %g）" % (
                    r["credits"], r["plan_remaining"], r["addon_remaining"]))
            else:
                lines.append("   当前积分：%g 分" % r["credits"])
        else:
            lines.append("   当前积分：未取到")
        for part in expiry_lines(r, claims, today):
            lines.append("   " + part)
        if r["status"] in ("error", "failed") and r["message"]:
            lines.append("   ⚠️ 签到状态：%s" % r["message"][:60])
        lines.append("")

    # ③ 未来 7 天到期合计：套餐 + 资源包（账本精确项 + 估算项单列）
    plan_soon = [r for r in rows if r["has_data"] and r["days_left"] is not None
                 and r["days_left"] <= 7 and r["plan_remaining"] > 0]
    plan_sum = sum(r["plan_remaining"] for r in plan_soon)
    limit = (now.date() + timedelta(days=7)).isoformat()
    ledger_soon = [c for c in claims if isinstance(c, dict)
                   and str(c.get("expireAt") or "") and today <= str(c.get("expireAt") or "") <= limit]
    ledger_sum = sum(num(c.get("credit")) for c in ledger_soon)
    total = plan_sum + ledger_sum
    lines.append("未来 7 天到期合计：%g 分" % total)
    if plan_soon:
        lines.append("   · 套餐：%s" % "、".join(
            "%s %g 分" % (r["expire_date"], r["plan_remaining"]) for r in plan_soon))
    if ledger_soon:
        lines.append("   · 资源包：%s" % "、".join(
            "%s %g 分" % (c.get("expireAt"), num(c.get("credit"))) for c in ledger_soon))
    if not plan_soon and not ledger_soon:
        book = [c for c in claims if isinstance(c, dict) and str(c.get("expireAt") or "") >= today]
        if book:
            lines.append("   （资源包最早 %s 到期，不在 7 天内）" % book[0].get("expireAt"))
    lines.append("注：套餐额度随本周期整批到期；签到资源包每笔领取后 30 天各自到期"
                 "（官方规则）；数据来自云端只读余额查询与领取账本。")
    if run_url:
        lines.append("运行详情：%s" % run_url)
    return "\n".join(lines)


def build_report(rows, title, claims, today):
    """Markdown 榜单，写进 Actions Summary。"""
    lines = ["#### %s · 每日积分播报（按套餐到期日排序）" % title, "",
             "| # | 账号 | 当前积分 | 套餐剩余 | 资源包 | 套餐到期日 | 剩几天 | 资源包最早到期 | 签到状态 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for i, r in enumerate(rows, 1):
        hits = ledger_for(r, claims, today) if r["has_data"] else []
        addon_exp = hits[0].get("expireAt") if hits else "—"
        lines.append("| %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            i, r["account"],
            "**%g**" % r["credits"] if r["has_data"] else "未取到",
            "%g 分" % r["plan_remaining"] if r["has_data"] else "—",
            "%g 分" % r["addon_remaining"] if r["has_data"] else "—",
            r["expire_date"] or "—",
            r["days_left"] if r["days_left"] is not None else "—",
            "%s（%d 笔）" % (addon_exp, len(hits)) if hits else "—",
            "失败" if r["status"] in ("error", "failed") else "成功"))
    lines.append("")
    lines.append("> 套餐剩余取云端只读余额接口（随本周期到期日整批到期）；"
                 "签到资源包按笔记录，每笔领取后 30 天各自到期（账本 ledger/claims.json）。")
    if not rows:
        lines.append("")
        lines.append("- 本次没有收到任何账号的积分数据。")
    return "\n".join(lines) + "\n"


def post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def push_webhook(value, text):
    url = value if value.startswith("http") else (QYAPI + "/webhook/send?key=" + value)
    r = post_json(url, {"msgtype": "text", "text": {"content": text}})
    return "webhook:%s" % (r.get("errmsg") or r.get("errcode"))


def push_app(value, text):
    parts = [p.strip() for p in value.split(",")]
    if len(parts) < 4:
        return "app:配置格式错误（应为 corpid,corpsecret,touser,agentid）"
    corpid, secret, touser, agentid = parts[0], parts[1], parts[2] or "@all", parts[3]
    token_url = "%s/gettoken?corpid=%s&corpsecret=%s" % (
        QYAPI, urllib.parse.quote(corpid, safe=""), urllib.parse.quote(secret, safe=""))
    with urllib.request.urlopen(token_url, timeout=15) as resp:
        t = json.loads(resp.read().decode("utf-8", "replace"))
    if t.get("errcode") != 0:
        return "app:gettoken失败 %s" % t.get("errmsg")
    r = post_json("%s/message/send?access_token=%s" % (QYAPI, t["access_token"]), {
        "touser": touser, "msgtype": "text", "agentid": agentid,
        "text": {"content": text}, "safe": 0,
    })
    bits = ["errcode=%s" % r.get("errcode"), "errmsg=%s" % (r.get("errmsg") or "-")]
    if r.get("invaliduser"):
        bits.append("invaliduser=%s" % r["invaliduser"])
    if r.get("msgid"):
        bits.append("msgid=%s" % r["msgid"])
    return "app:" + " ".join(bits)


def push_all(text):
    channels = []
    webhook = (os.environ.get("WECOM_WEBHOOK") or "").strip()
    app = (os.environ.get("WECOM_APP") or "").strip()
    if webhook:
        try:
            channels.append(push_webhook(webhook, text))
        except Exception as e:
            channels.append("webhook异常:%s" % type(e).__name__)
    if app:
        try:
            channels.append(push_app(app, text))
        except Exception as e:
            channels.append("app异常:%s" % type(e).__name__)
    if not channels:
        channels.append("未配置通知通道，仅打印：\n" + text)
    return channels


def main():
    args = sys.argv[1:]
    root = args[0] if args and not args[0].startswith("--") else "_report"
    title = "签到"
    if "--title" in args:
        i = args.index("--title")
        if i + 1 < len(args):
            title = args[i + 1]
    ledger_path = DEFAULT_LEDGER
    if "--file" in args:
        i = args.index("--file")
        if i + 1 < len(args):
            ledger_path = args[i + 1]
    claims = load_ledger(ledger_path)
    today = datetime.now(BEIJING).date().isoformat()
    rows = load_rows(root)
    print(build_report(rows, title, claims, today))
    policy = str(os.environ.get("RECOMMEND_NOTIFY") or "daily").strip().lower()
    if policy in ("0", "off", "false", "no", "none", "disabled"):
        print("每日积分播报：已关闭（RECOMMEND_NOTIFY=%s）" % policy)
        return 0
    text = build_text(rows, title, claims, os.environ.get("REPORT_RUN_URL"))
    try:
        print("每日积分播报：%s" % " | ".join(push_all(text)))
    except Exception as e:
        # 播报失败绝不影响签到结果与心跳
        print("每日积分播报推送异常（已忽略）：%s" % type(e).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
