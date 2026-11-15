#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qoder 签到状态巡检（GitHub Actions 云端，纯标准库）。

策略（用户定，避免与延迟落地的 7 点班撞车）：
  · 主班北京 07:00 触发，GitHub cron 常延迟 3~7.5 小时，落点常在 10:00 之后
  · 10:05 起每 15 分钟只读巡检一次，到 15:35
  · 每次只查「今天主 workflow 的 schedule run」，分三段：
      没有 run                  → 主班还在调度延迟里（run 根本没创建），静默等下一轮
      有 queued / in_progress   → 已落地但还没跑完，静默等下一轮
      全部 completed            → 才去看结果
  · 有「10:00 之后开跑且成功」的 schedule run → 判定已领到，静默收工
  · 否则 → 需要补跑；15:00 之后不再等排队，直接进入补跑判断
  · 补跑前查今天 10:00 后是否已有 dispatch run，有就不再重复触发
  · 补跑 = dispatch 主 workflow（幂等 + concurrency 串行），等结论后推手机：
      成功推 ✅ / 失败或超时推 ⚠️

⚠️ 本脚本绝不直接调 Qoder claim。所有领取都由主 workflow 执行，
   这样既不会和延迟落地的 7 点班同时发车，也不会绕过账号间隔 5 分钟。

环境变量：
  GITHUB_TOKEN              Actions 自动注入，需 actions:read + actions:write
  GITHUB_REPOSITORY         owner/repo
  WECOM_APP                 企业微信自建应用凭据（与签到/播报共用同一 Secret）
  QODER_WATCHDOG_DRY_RUN    1 = 只诊断不触发（演练用）

退出码：0 = 静默收工 / 补跑成功；1 = 补跑失败或异常（让 Actions 变红）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
API = 'https://api.github.com'
REPO = (os.environ.get('GITHUB_REPOSITORY') or 'caiping527/qoder-checkin').strip()
TOKEN = (os.environ.get('GITHUB_TOKEN') or '').strip()
MAIN_WF = 'qoder-checkin.yml'
DRY = os.environ.get('QODER_WATCHDOG_DRY_RUN', '') == '1'

# 巡检窗口（北京时间）。上限留到 15:40，容忍 GitHub cron 延迟到点后。
INSPECT_FROM = (10, 5)
FORCE_AT = (15, 0)
INSPECT_UNTIL = (15, 40)
WINDOW_OPEN = (10, 0)

ACTIVE = ('queued', 'in_progress', 'waiting', 'pending', 'requested')


def now():
    return datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S')


def log(msg):
    print('[%s] %s' % (now(), msg), flush=True)


def mins(t):
    return t.hour * 60 + t.minute


def at(pair):
    return pair[0] * 60 + pair[1]


def to_bj(ts):
    """GitHub 的 ISO UTC 时间戳 → 北京时间 datetime。"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace('Z', '+00:00')).astimezone(BJ)
    except (TypeError, ValueError):
        return None


def api_get(path):
    req = urllib.request.Request(API + path, headers={
        'Authorization': 'Bearer ' + TOKEN,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'qoder-watchdog',
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def api_post(path, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(API + path, data=data, method='POST', headers={
        'Authorization': 'Bearer ' + TOKEN,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
        'User-Agent': 'qoder-watchdog',
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def runs_today(event):
    """今天（北京时间）主 workflow 的指定事件 run 列表。

    ⚠️ 不能用 API 的 created=YYYY-MM-DD 过滤：GitHub 按 UTC 日期算，
    而主班 cron 是 UTC 23:00（= 北京次日 07:00），UTC 日期比北京早一天，
    会被漏掉 → 巡检会误判「主班没落地」并天天走 15:00 兜底。
    所以拉最近 100 条，在客户端按北京时间日期过滤。
    用 workflow 文件名查询，避免把 watchdog 自己的 schedule run 算进来。
    """
    q = urllib.parse.urlencode({'event': event, 'per_page': '100'})
    d = api_get('/repos/%s/actions/workflows/%s/runs?%s' % (REPO, MAIN_WF, q))
    today = datetime.now(BJ).date()
    out = []
    for r in d.get('workflow_runs') or []:
        c = to_bj(r.get('created_at'))
        if c and c.date() == today:
            out.append(r)
    return out


# ── 企业微信推送（自包含，和 daily_report / checkin 同一套凭据口径） ──
def push_wecom(title, content):
    raw = (os.environ.get('WECOM_APP') or '').strip()
    if not raw:
        return 'skipped(未配置 WECOM_APP)'
    parts = [x.strip() for x in raw.split(',')]
    if len(parts) < 4 or not parts[0] or not parts[1] or not parts[3]:
        return 'skipped(凭据格式错误)'
    try:
        def post(url, payload):
            req = urllib.request.Request(
                url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                method='POST', headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode('utf-8', 'replace'))

        t = post('https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s'
                 % (urllib.parse.quote(parts[0]), urllib.parse.quote(parts[1])), {})
        token = t.get('access_token') or ''
        if not token:
            return 'gettoken失败(%s)' % t.get('errmsg', '')
        r = post('https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=%s' % token,
                 {'touser': parts[2] or '@all', 'msgtype': 'text', 'agentid': parts[3],
                  'text': {'content': (title + '\n\n' + content).strip()}})
        return 'ok' if r.get('errcode') == 0 else 'send失败(errcode=%s)' % r.get('errcode')
    except Exception as e:
        return '异常(%s)' % type(e).__name__


def notify(title, content):
    r = push_wecom(title, content)
    log('企业微信推送：%s' % r)
    return r


def do_repair(reason):
    """dispatch 主 workflow → 找到新 run → 等结论 → 成功/失败都推手机。"""
    t0 = datetime.now(timezone.utc) - timedelta(seconds=5)
    try:
        st = api_post('/repos/%s/actions/workflows/%s/dispatches' % (REPO, MAIN_WF),
                      {'ref': 'main', 'inputs': {'reason': 'watchdog'}})
        log('已 dispatch 主 workflow（HTTP %s）' % st)
    except Exception as e:
        notify('⚠️ Qoder 签到补跑未触发',
               '巡检判定需要补跑，但 dispatch 失败：%s\n原因：%s\n请在 Actions 手动 Run workflow。'
               % (type(e).__name__, reason))
        return 1

    rid, url = None, ''
    for _ in range(20):                       # 最多等 100 秒找到新 run
        time.sleep(5)
        try:
            for r in runs_today('workflow_dispatch'):
                c = to_bj(r.get('created_at'))
                if c and c.astimezone(timezone.utc) >= t0 and r.get('id'):
                    if rid is None or r['id'] > rid:
                        rid, url = r['id'], r.get('html_url') or ''
        except Exception as e:
            log('查找新 run 时出错（继续重试）：%s' % type(e).__name__)
        if rid:
            break
    if not rid:
        notify('⚠️ Qoder 签到补跑未触发',
               '已发起 dispatch，但没查到新 run。\n原因：%s\n请去 Actions 手动 Run workflow。' % reason)
        return 1
    log('补跑 run id=%s' % rid)

    conclusion = None
    for _ in range(40):                      # 40 × 20s ≈ 13 分钟，覆盖 5 分钟账号间隔
        time.sleep(20)
        try:
            d = api_get('/repos/%s/actions/runs/%s' % (REPO, rid))
            url = d.get('html_url') or url
            if d.get('status') == 'completed':
                conclusion = d.get('conclusion')
                break
        except Exception as e:
            log('查询补跑状态时出错（继续重试）：%s' % type(e).__name__)
    if conclusion is None:
        notify('⚠️ Qoder 签到补跑超时',
               '补跑 run 超过 13 分钟仍未结束，请自行确认结果。\n原因：%s\n运行详情：%s'
               % (reason, url))
        return 1

    log('补跑结论 = %s' % conclusion)
    if conclusion == 'success':
        notify('✅ Qoder 签到补跑成功',
               '巡检发现今天主班未在窗口内成功领取，已自动补跑完成。\n'
               '原因：%s\n运行详情：%s\n（主 workflow 的积分播报也会照常推一条）' % (reason, url))
        return 0
    notify('⚠️ Qoder 签到补跑失败',
           '巡检已确认过了 10 点、没有排队，补跑仍然失败，属于真异常，请查看日志。\n'
           '原因：%s\n结论：%s\n运行详情：%s' % (reason, conclusion, url))
    return 1


def main():
    n = datetime.now(BJ)
    cur = mins(n)
    log('开始巡检（北京时间 %s，DRY=%s）' % (n.strftime('%H:%M:%S'), DRY))

    if cur < at(INSPECT_FROM) or cur > at(INSPECT_UNTIL):
        log('不在巡检窗口 10:05~15:40，静默退出（cron 延迟到窗口外属正常）')
        return 0
    if not TOKEN:
        log('缺少 GITHUB_TOKEN，无法巡检')
        return 1

    try:
        sched = runs_today('schedule')
        disp = runs_today('workflow_dispatch')
    except Exception as e:
        log('查询 run 失败：%s' % type(e).__name__)
        return 1
    log('今天主 workflow：schedule runs = %d，dispatch runs = %d' % (len(sched), len(disp)))
    for r in (sched + disp)[:6]:
        st = to_bj(r.get('run_started_at') or r.get('created_at'))
        log('  id=%s status=%s conclusion=%s 开始=%s' % (
            r.get('id'), r.get('status'), r.get('conclusion') or '-',
            st.strftime('%H:%M') if st else '-'))

    force = cur >= at(FORCE_AT)
    active = [r for r in sched if r.get('status') in ACTIVE]

    # 15:00 之前：只要主班还没落地、或落地了还没跑完，就静默等下一轮。
    # 关键：GitHub 的调度延迟阶段 run 根本不存在，所以「没有 run」也必须等，
    # 绝不能据此判定「没排队」而立刻补跑 —— 那正是会撞风控的情况。
    if not force:
        if active:
            log('→ 主班已落地但还在跑（%d 个活跃 run），静默等下一轮' % len(active))
            return 0
        if not sched:
            log('→ 今天还没有 schedule run，主班仍在调度延迟里，静默等下一轮')
            return 0
    else:
        log('→ 已过 15:00，不再等排队，进入补跑判断')

    # 「有没有领到」看今天全部主 workflow run：主班(schedule) 或补跑/手动(dispatch)。
    # 只要有一个在 10:00 之后开跑且成功，就算已领到 —— 补跑常常是 dispatch 事件，
    # 只看 schedule 会把它漏掉、误判成「还需要补跑」。
    for r in sched + disp:
        st = to_bj(r.get('run_started_at') or r.get('created_at'))
        if (r.get('status') == 'completed' and r.get('conclusion') == 'success'
                and st and mins(st) >= at(WINDOW_OPEN)):
            log('→ 今天已有窗口内成功的 run，判定已领到，静默收工')
            return 0

    # 走到这里 = 过了 10 点、没有排队、确实没领到 → 真异常，需要补跑
    if force and not sched:
        reason = '已过 15:00，今天主班 schedule run 始终没有落地'
    elif force:
        reason = '已过 15:00，今天主班未在 10:00 之后成功领取'
    else:
        reason = '主班已跑完，但落在 10:00 窗口前或结论非成功'
    log('→ 判定需要补跑：%s' % reason)

    # 去重：今天 10:00 后已有任意手动/自动 dispatch run，就不再重复补跑
    already = []
    for r in disp:
        c = to_bj(r.get('created_at'))
        if c and mins(c) >= at(WINDOW_OPEN):
            already.append(r)
    if already:
        log('→ 今天 10:00 后已有 %d 次 dispatch run，避免重复补跑，静默退出' % len(already))
        return 0

    if DRY:
        log('DRY_RUN=1：只诊断，不 dispatch（本次本应补跑：%s）' % reason)
        return 0
    return do_repair(reason)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        log('[x] 未预期错误：%s' % e)
        sys.exit(1)
