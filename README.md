# Qoder 签到

> 每天自动领取 Qoder CN「每天领 100 Credits」，GitHub Actions 云端运行，不需要开电脑。

## 这个仓库是什么

Qoder 的每日签到，靠 GitHub 的定时任务跑，本机不用常开。

- 私有仓库：不出现在公开搜索里，降低被扫号/风控的概率
- 纯标准库，零第三方依赖
- 凭据存在 Secrets，不进代码、不进日志
- 失败自动推企业微信（只推失败，成功不打扰）

## 领取窗口

Qoder 的活动窗口是 **今天 10:00 → 明天 10:00**（UTC+8）。

| 北京时刻 | 作用 |
|---|---|
| 07:00 | 主班。GitHub cron 常延迟 3~7.5 小时，落点大概率在 10:00 之后，届时直接领取 |
| 10:05~15:35 | 状态巡检，每 15 分钟一次（只读，不直接领取） |

两个账号之间固定间隔 5 分钟（主账号先领，第二个号等 300 秒）。

## 状态巡检与自动补跑

不挂固定补签班 —— 固定班可能和延迟落地的 7 点主班撞车，触发上游风控。
改为 `watchdog.py` 只读巡检，按「当前状态」决定要不要补跑：

1. 查今天主 workflow 的 schedule run：
   - 没有 run → 主班还在 GitHub 调度延迟里（run 尚未创建），静默等下一轮
   - 有 `queued` / `in_progress` → 已落地还没跑完，静默等下一轮
   - 全部 `completed` → 才看结果
2. 有「10:00 之后开跑且成功」的 run → 判定已领到，静默收工
3. 否则 → 需要补跑；**15:00 之后不再等排队**，直接进入补跑判断
   （实测最长延迟 7.5 小时，15:00 主班怎么都该跑完了）
4. 补跑 = dispatch 主 workflow（幂等 + `concurrency` 串行 + 5 分钟账号间隔），
   **巡检绝不自己发 claim**；补跑前查今天 10:00 后是否已有 dispatch run，避免重复触发

推送语义：排队中 / 已领到 / 窗口未开 → 静默不推；补跑成功推 ✅、失败或超时推 ⚠️。
窗口未开时连积分播报也整条跳过，避免早上收到一条「积分没变」的无效消息。
「有可领活动却一个都没领到」现在改判为失败，会推 ⚠️ 并让 Actions 变红。

演练：手动 Run `qoder-watchdog.yml`，勾选 `dry_run`，只诊断不触发补跑。

## 怎么部署 / 怎么维护

本机工具在 `D:\GITHUB与Token路由\QODER签到更新\`，只有一个入口：

1. 在 AiRoute 里登录 Qoder 账号
2. 双击 `一键更新.bat`
   - 读 AiRoute 里的全部 Qoder 账号，依次上传凭据
   - 第 1 个 → 仓库级 Secret；其余 → 各建 Environment `qoder-<编号>`
   - 代码与 workflow 逐字比对，一致就跳过，有改动才上传
   - 触发一次云端自检

首次部署和日常维护都是它 —— 缺什么补什么，幂等。

## 目录

```
checkin.py                       签到逻辑（含积分/额度到期只读查询）
summarize.py                     运行摘要
daily_report.py                  每日积分播报（汇总各账号，推企业微信）
watchdog.py                      状态巡检与条件补跑（只读 + dispatch）
.github/workflows/qoder-checkin.yml    主班
.github/workflows/qoder-watchdog.yml   巡检
```

## 每日积分播报

每天签到完成后，`daily_report` job 汇总全部账号，推一条消息到手机，逐个列出
「当前积分」「还有多久到期、该提前哪天用掉」，按最早到期日排序。与小浣熊同形态。

- 数据来源：云端只读调 `/sash/api/v2/me/usage`，与 AiRoute 本地余额口径一致；
  查不到只标注「未取到」，不影响签到结果与心跳。
- 推送凭据：`WECOM_APP`，由「一键更新」从 工具箱\配置.json 的 `wecom` 自动写入 Secret。
- 关闭：仓库 Variable `RECOMMEND_NOTIFY=0`。
- Qoder 的 `expiresAt` 是**本周期额度到期/刷新日**，播报按此措辞如实标注，
  不冒充小浣熊那种逐批积分作废。

## 环境变量（Actions 自动注入 / Secrets）

| 名称 | 说明 |
|---|---|
| `QODER_ACCESS_TOKEN` | `dt-` 访问令牌 |
| `QODER_REFRESH_TOKEN` | `drt-` 续期令牌，可自动续期 |
| `QODER_UID` | 账号 uid |
| `QODER_MACHINE_ID` | 设备标识（续期用） |
| `WECOM_APP` | 企业微信应用凭据 `corpid,corpsecret,touser,agentid` |
| `GITHUB_TOKEN` | Actions 自动注入；巡检用它查 run 状态、dispatch 主 workflow |

## 多账号

仓库变量 `QODER_ACCOUNTS` 列出账号编号（逗号分隔），每个编号对应一个
Environment `qoder-<编号>`，环境级 Secret 覆盖仓库级同名值，互不干扰。

⚠️ 官方限制：Qoder 按「人」去重，同一台机器/同一个人名下多个账号，
当天只有第一个能领到，其余返回 `SAME_PERSON_ALREADY_CLAIMED`。

两个账号之间固定间隔 5 分钟：主账号先领，动态账号靠 workflow 里的
`needs: checkin_main` 排在主账号之后、再等 `sleep 300` 才发 claim。

## 排错

| 日志 | 含义 |
|---|---|
| `当前北京时间 XX:XX，今天 10:00 的新一轮还没开放` | 7 点主班没延迟、窗口未开；不算失败也不推手机，巡检 10:05 起接手 |
| `没有领到（可领活动均未成功）` | 真失败：Actions 变红、推 ⚠️，巡检会补跑 |
| `今天已领过` | 已领到，正常 |
| `鉴权失败（HTTP 401）` | 令牌过期，回 AiRoute 重登后跑「一键更新」 |
| `同一人今天已领过` | 官方去重，不是故障 |

## License

MIT
