# Qoder 每日签到

账号由本机 `Qoder签到.pyw` 同步到 Secret，仓库内不含任何令牌。

- 签到：`.github/workflows/qoder-checkin.yml`，每天北京时间约 07:00
- 巡检：`.github/workflows/qoder-watchdog.yml`，只读检查 + 按需补跑
- 凭据：仓库 Secret / Environment `qoder-<编号>`
