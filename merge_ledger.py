#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把各账号 result.json 里的领取记录合并成持久账本（仓库 ledger/claims.json）。

为什么需要它
------------
Qoder 官方接口只给「总积分 + 本周期到期日」，**没有逐笔明细**。
但签到这件事我们自己是知道的：每笔 100 分、领取之日起 30 天有效（官方规则）。
把「哪一天、哪个号、领了多少」记下来，就能精确算出——
  · 每一笔什么时候过期；
  · 某一天（比如明天）到底有多少分要过期。
过去没记的补不了；从启用本账本那天起，逐笔到期都是精确的。

数据流
------
checkin job 上传 result.json（artifact）→ 本脚本在 report job 里合并：
  1. 读旧账本（首次不存在 = 空）；
  2. 逐个 result.json 取 claim 字段（只有 outcome=claimed 才会有）；
  3. 按「uid + 领取日期（北京时间）」去重，补跑 / 重跑都不会重复记账；
  4. 按领取时间升序写回 ledger/claims.json。
workflow 随后 git commit 回仓库，本地 AiRoute 与手机播报都读这一份。

用法
----
  python merge_ledger.py _report                       # 默认写 ledger/claims.json
  python merge_ledger.py _report --file some/other.json
退出码恒为 0：账本失败绝不能影响签到与播报。
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
DEFAULT_FILE = os.path.join('ledger', 'claims.json')


def num(v):
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def day_of(claimed_at, fallback=''):
    """领取时刻 → 北京时间日期字符串（YYYY-MM-DD）。解析失败用 fallback。"""
    text = str(claimed_at or '').strip()
    if len(text) >= 10 and text[4] == '-' and text[7] == '-':
        return text[:10]
    return fallback


def normalize(claim, data):
    """result.json 的 claim 字段 → 账本条目；不合法返回 None。"""
    if not isinstance(claim, dict):
        return None
    credit = num(claim.get('credit'))
    if credit <= 0:
        return None
    uid = str(claim.get('uid') or data.get('uid') or '').strip()
    account = str(claim.get('account') or data.get('account') or '').strip()
    if not uid and not account:
        return None
    claimed_at = str(claim.get('claimedAt') or '').strip()
    valid_days = int(num(claim.get('validDays') or 30)) or 30
    expire_at = str(claim.get('expireAt') or '').strip()
    if not expire_at and claimed_at:
        try:
            expire_at = (datetime.fromisoformat(claimed_at) + timedelta(days=valid_days)).date().isoformat()
        except ValueError:
            expire_at = ''
    today = datetime.now(BEIJING).date().isoformat()
    return {
        'uid': uid,
        'account': account,
        'credit': credit,
        'claimedAt': claimed_at,
        'validDays': valid_days,
        'expireAt': expire_at or day_of(claimed_at, today),
    }


def load_ledger(path):
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
    except Exception:
        doc = {}
    claims = doc.get('claims') if isinstance(doc, dict) else None
    return claims if isinstance(claims, list) else []


def key_of(item):
    owner = item.get('uid') or item.get('account') or ''
    return '%s|%s' % (owner, day_of(item.get('claimedAt')))


def merge(old, incoming):
    """按 uid+日期 去重合并。同键保留最早领取时刻，金额取较大值。"""
    table = {}
    for item in old:
        if isinstance(item, dict):
            table.setdefault(key_of(item), item)
    before = len(table)
    for item in incoming:
        k = key_of(item)
        prev = table.get(k)
        if prev is None:
            table[k] = item
            continue
        # 同一天同一号重复上报：金额以防更大（不会重复给分，但取全更稳）
        if num(item.get('credit')) > num(prev.get('credit')):
            prev['credit'] = item['credit']
        # 到期日与账号名以更新的记录为准
        for field in ('expireAt', 'account', 'uid'):
            if item.get(field):
                prev[field] = item[field]
    merged = list(table.values())
    merged.sort(key=lambda x: (str(x.get('claimedAt') or ''), str(x.get('uid') or x.get('account') or '')))
    return merged, len(merged) - before


def save_ledger(path, claims):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    doc = {
        'version': 1,
        'updatedAt': datetime.now(BEIJING).isoformat(timespec='seconds'),
        'rule': '每笔领取自领取之日起 30 天内有效，各自滚动到期（Qoder 官方活动规则）',
        'claims': claims,
    }
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def main():
    args = sys.argv[1:]
    root = args[0] if args and not args[0].startswith('--') else '_report'
    path = DEFAULT_FILE
    if '--file' in args:
        i = args.index('--file')
        if i + 1 < len(args):
            path = args[i + 1]

    old = load_ledger(path)
    incoming = []
    for result_path in sorted(glob.glob(os.path.join(root, '**', 'result.json'), recursive=True)):
        try:
            with open(result_path, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        item = normalize(data.get('claim'), data)
        if item:
            incoming.append(item)

    merged, added = merge(old, incoming)
    if added > 0 or not os.path.exists(path):
        save_ledger(path, merged)
    print('账本：旧 %d 条，本次上报 %d 条，新增 %d 条，合计 %d 条 → %s'
          % (len(old), len(incoming), added, len(merged), path))
    # workflow 用这一行判断「要不要 commit」
    print('LEDGER_ADDED=%d' % added)
    return 0


if __name__ == '__main__':
    sys.exit(main())
