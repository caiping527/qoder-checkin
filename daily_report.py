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
import math
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
        # 今天这次实际领到多少分：和 outcome 一起判断「今天有没有新加一笔」
        today_claim_credit = num(data.get("credit"))
        err = str(exp.get("error") or "")
        credits = num(exp.get("credits")) if not err else 0.0
        # 新口径：套餐剩余与资源包剩余分开；老数据（无 addon_remaining）
        # 按旧口径回退：全部算套餐，资源包记 0，不会凭空出现错误拆分。
        if "addon_remaining" in exp and not err:
            addon = num(exp.get("addon_remaining"))
            plan = num(exp.get("earliest_remaining"))
            addon_total = num(exp.get("addon_total"))
            addon_used = num(exp.get("addon_used"))
        else:
            addon = 0.0
            plan = num(exp.get("earliest_remaining")) if not err else 0.0
            addon_total = 0.0
            addon_used = 0.0
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
            "addon_total": max(0.0, addon_total),
            "addon_used": max(0.0, addon_used),
            "today_claim_credit": max(0.0, today_claim_credit),
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


def claimed_today(row):
    """今天到底有没有领到一笔——决定「最后一笔」的基准日，不能默认成功。

    outcome 四态：
      claimed  = 今天真领到（credit>0 才算真加了一笔）
      done     = 今天已领过（今天确实有这一笔）
      ineligible / failed / error = 今天没领到 → 最后一笔只能在前几天
    返回值只影响推算，不影响签到主流程。
    """
    status = str(row.get("status") or "")
    if status == "done":
        return True
    if status == "claimed" and num(row.get("today_claim_credit")) > 0:
        return True
    return False


def addon_expiry(row, claims, today):
    """资源包「最早到期日」——随消耗自动往后推，不用人工记账。

    依据（官方规则）：每天领 100 分、每笔领取后 30 天有效、先到期先用。
    上游 me/usage 直接给资源包的 total / used / remaining，所以消耗量不用猜：
      · 最后一笔的到期日：优先用账本里最新一笔的 expireAt（精确）；
        没账本时按今天签到结果推——今天领到了 = 今天+30；
        今天没领到（failed/ineligible/error）= 今天+29（只可能更早，往早算更保守）；
      · 剩余笔数 lots = ceil(remaining / 100)；
      · 最早到期日 = 最后一笔到期日 - (lots - 1) 天；
      · 当天到期量 = remaining - (lots - 1) * 100（那一天可能不满 100 分）。
    例：700 分（7 笔）今天领到 → 最早 12-13；用了 350 分剩 350（4 笔）→ 最早 12-16，
        12-16 当天到期 50 分。
    账本断档或今天漏签只影响「最后一笔是哪天」，误差按天计，文案统一用「约」。
    """
    remaining = row.get("addon_remaining") or 0
    if remaining <= 0:
        return None
    hits = ledger_for(row, claims, today)
    last = str(hits[-1].get("expireAt") or "") if hits else ""
    if not last:
        base = today if isinstance(today, date) else date.fromisoformat(str(today))
        # 今天没签到成功 → 最后一笔不可能在今天，往早推一天，避免把到期日算晚
        offset = 0 if claimed_today(row) else 1
        last = (base + timedelta(days=30 - offset)).isoformat()
    lots = max(1, int(math.ceil(remaining / float(LOT_SIZE))))
    earliest = date.fromisoformat(last) - timedelta(days=lots - 1)
    day_amount = round(remaining - (lots - 1) * LOT_SIZE, 2)
    return {"earliest": earliest.isoformat(), "lots": lots, "day_amount": day_amount}


def expiry_lines(r, claims, today):
    """一个账号的到期说明：套餐一行 + 资源包一行，不展开内部细节。"""
    if not r["has_data"]:
        if r["error"]:
            return ["积分未取到（%s）" % r["error"][:40]]
        return ["积分未取到"]

    lines = []
    # ① 套餐额度：随本周期整批到期（用完了就不再提）
    plan = r["plan_remaining"]
    if plan > 0 and r["expire_date"]:
        days = r["days_left"] if r["days_left"] is not None else "?"
        line = "%g 分于 %s 到期（剩 %s 天）" % (plan, r["expire_date"], days)
        if isinstance(r["days_left"], (int, float)) and r["days_left"] <= 1:
            line += "；今天就用掉"
        elif r["use_by"]:
            line += "；建议 %s 前用掉，提前一天" % r["use_by"]
        lines.append(line)

    # ② 签到资源包：按剩余量算「最早哪天开始到期」，消耗后自动往后推
    est = addon_expiry(r, claims, today)
    if est:
        if est["day_amount"] < LOT_SIZE:
            lines.append("%g 分约 %s 起陆续到期" % (r["addon_remaining"], est["earliest"]))
        else:
            lines.append("%g 分约 %s 到期" % (r["addon_remaining"], est["earliest"]))
    return lines


def build_text(rows, title, claims, run_url=None):
    """企微纯文本，不用 Markdown 符号，手机上看得清。"""
    now = datetime.now(BEIJING)
    today = now.date().isoformat()
    lines = ["【%s · 每日积分播报】%s" % (title, now.strftime("%Y-%m-%d")), ""]
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
            lines.append("   当前积分：%g 分" % r["credits"])
        else:
            lines.append("   当前积分：未取到")
        for part in expiry_lines(r, claims, today):
            lines.append("   " + part)
        if r["status"] in ("error", "failed", "ineligible") and r["message"]:
            lines.append("   ⚠️ 今天没领到：%s" % r["message"][:60])
        lines.append("")

    lines.append("注：套餐按周期整批到期；签到积分每笔 30 天，滚动到期。")
    if run_url:
        lines.append("运行详情：%s" % run_url)
    return "\n".join(lines)


def build_report(rows, title, claims, today):
    """Markdown 榜单，写进 Actions Summary（与手机版同口径，只是排版好看点）。"""
    lines = ["#### %s · 每日积分播报" % title, "",
             "| # | 账号 | 当前积分 | 套餐到期 | 积分约到期 | 签到状态 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for i, r in enumerate(rows, 1):
        if not r["has_data"]:
            lines.append("| %d | %s | 未取到 | — | — | %s |" % (
                i, r["account"], "失败" if r["status"] in ("error", "failed") else "成功"))
            continue
        plan = "—"
        if r["plan_remaining"] > 0 and r["expire_date"]:
            plan = "%g 分 / %s（剩 %s 天）" % (
                r["plan_remaining"], r["expire_date"],
                r["days_left"] if r["days_left"] is not None else "?")
        est = addon_expiry(r, claims, today)
        addon = "—"
        if est:
            addon = "%g 分 / 约 %s" % (r["addon_remaining"], est["earliest"])
        lines.append("| %d | %s | **%g** | %s | %s | %s |" % (
            i, r["account"], r["credits"], plan, addon,
            "失败" if r["status"] in ("error", "failed") else "成功"))
    lines.append("")
    lines.append("> 套餐按周期整批到期；签到积分每笔领取后 30 天滚动到期，"
                 "剩余量减少时「约到期日」自动往后推。")
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
