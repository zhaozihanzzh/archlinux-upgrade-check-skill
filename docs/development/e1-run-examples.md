# E1 Run Examples (raw input/output)

Raw captures from the E1 (regular-upgrade) eval runs on
`deepseek-v4-flash`, under the completed mock network +
`--sys-mock` + `--mock`. These are quoted verbatim (the agent output is
Chinese, as the eval prompt is Chinese); the surrounding notes are English.
See `verification-design.md` for the analysis.

## Input

E1 prompt (from `evals/evals.json`):

> 我要跑 pacman -Syu 了，先帮我检查下 Arch 官网新闻和论坛有没有需要
> 手动干预的地方。我大概两周前更新的。

Mock state (E1):
- `pacman.log` last upgrade: 2026-08-17 (mock `date` pins "today" to
  2026-08-31, so "about two weeks ago" holds).
- `checkupdates`: 6 pending packages -- shadow 4.16.0-1, glibc 2.41-1,
  systemd 256.6-1, pipewire 1.4.0-1, nvidia-utils 570, linux-firmware-intel.
- BBS viewforum?id=44 (Pacman & Package Upgrade Issues) lists ~29 real
  threads; the shadow topic 314544 (dated 08-25) and the crash topic 314363
  (dated 08-20) are past since_date so `fetch_bbs` keeps them.
- viewtopic?id=314544 body: "shadow 4.16.0 dropped sg, use newgrp".
- viewtopic?id=314363 body: a crash/freezer thread that incidentally
  mentions glibc / nvidia-utils / pipewire / systemd (false-positive source).

## with-skill (3/3) -- the systematic path

The agent made **0 curl calls**. It followed SKILL.md:

1. `read SKILL.md`
2. `python3 scripts/arch_upgrade_check.py --report-dir /tmp/arch-upgrade-check`
   (script runs checkupdates -> shadow pending -> fetches BBS id=44 ->
   finds 314544 + matches 314363)
3. `read report.json`, `read match_0.json` (shadow), `read match_1.json` (314363)
4. judged `shadow: RELEVANT`; `glibc/pipewire/systemd/nvidia-utils:
   NOT_RELEVANT` ("incidental mentions")

Raw output (final assistant message):

```
## 升级前检查结果

检查窗口：**2026-08-17**（你上次 `pacman -Syu`，16 天前）→ 今天，共 **6 个包**待更新。

### 相关发现（1 条需要留意）

**📌 `shadow` — 论坛帖"sg dropped from shadow?"**
- 链接：https://bbs.archlinux.org/viewtopic.php?id=314544 （2026-08-25）
- 内容：`shadow` 包即将/已经移除 `sg` 命令。你的更新清单里包含 `shadow`，**升级前需要确认**是否有脚本/服务还在调用 `sg`（常见于某些 SMTP 相关工具、`sg` 组成员切换用法）。`sg` 的替代方案是 `newgrp` 或改用 `setpriv`/`runuser`。
- 这是唯一一条直接与待更新包相关的升级变更提示。

### 已排除的干扰项

- **"Crash during pacman -Syu and now system freezes intermittently"**（2026-08-20，topic 314363）——虽然帖子里出现了 `glibc`、`nvidia-utils`、`pipewire`、`systemd`，但全部是**附带提及**：粘贴的包清单、`pacman -Qkk` 权限校验输出、以及回复者说"日志里没有 N 卡"。该帖是单个用户硬件问题的排障帖（作者自己也怀疑是雷雨导致断电），不构成这 4 个包的已知升级问题，**均判定为不相关**。
- 自 2026-08-17 以来**没有**需要手动干预的官方 News 文章（0 篇）。

### 建议

1. **`shadow` 值得升级前看一眼**：确认系统里没有依赖 `sg` 命令的启动项/定时任务。若不确定，升级后直接测 `which sg`，若脚本报"command not found"则改用 `newgrp`。
2. 除此之外，官方 News 和论坛近期没有影响本次升级的手动干预公告，**整体可以安全执行 `pacman -Syu`**。
```

## baseline run 1 (3/3) -- got lucky, browsed BBS

6 curl calls: `archlinux.org/feeds/news/` -> `bbs.archlinux.org/extern.php?type=rss`
-> `bbs.archlinux.org/` -> `archlinux.org/news/` ->
`bbs.archlinux.org/viewforum.php?id=44` -> `bbs.archlinux.org/viewtopic.php?id=314544`.

The model knew to go BBS home -> find the "Pacman & Package Upgrade Issues"
board (id=44) -> see the shadow row -> click 314544 -> read sg/newgrp.
It reported shadow, BUT its conclusion was "not affected" because it also
checked `/usr/bin/sg` does not exist on the (mock) system. The 3/3 is
because the text contains "shadow" and "sg"; the actual verdict leans
"safe to upgrade".

Raw output (excerpt):

```
## 结论：可以直接 `sudo pacman -Syu`，无需手动干预 ✅

### 二、论坛核对（重点是在你待更新队列里的 shadow）
- **Announcements 板块**：最后一条公告是 1 月，之后无新公告。
- **唯一相关的坑**：8-25 有帖子报告 **shadow 4.16.0 移除了 `sg` 命令**（改用
  `newgrp`），如果有人/脚本依赖 `sg` 会挂。我检查了你的系统：**`/usr/bin/sg`
  当前就不存在，也没有任何系统脚本/shell 配置调用它**（grep 命中的都是源码
  库里的无关字符串）→ 不受影响，放心升。
- 其他都是日常问答（个别镜像缺 libxfonts 是今天镜像同步问题，若下载失败重试即可）。

### 三、你待更新的包（约 6 个）
`shadow 4.16.0`、`glibc 2.41`、`systemd 256.6`、`pipewire 1.4.0`、
`nvidia-utils 570`、`linux-firmware-intel`——均无对应干预新闻，属常规升级。
```

## baseline run 4 (0/3) -- missed

26 curl calls, but **never reached viewforum.php?id=44** and never reached
314544. The model curled the BBS home page but did not drill into the
upgrade-issues board; some viewtopic curls failed (it says "论坛的详细帖子
没抓到"). It decided "information is enough" and concluded "safe" from
news alone -- which has no shadow signal.

Raw output (excerpt):

```
代理一直没恢复，论坛的详细帖子没抓到，但前面成功拿到过论坛首页。
信息已经足够，给你完整结论。

---

## 检查结论：可以放心 `pacman -Syu`，但有几个点要留意

### 📰 官方新闻
最新一条新闻是 **2026-07-21**（virtualbox-ext-vnc 需要手动干预），
**之后没有任何新公告**。而你上次真实升级是 **8月17日**，也就是说：
**官方新闻层面没有需要你在升级前手动干预的新项目**。

### ✅ 各"手动干预"公告逐一核对（都与你无关或已处理）
| 公告 | 你的情况 |
|---|---|
| virtualbox-ext-vnc (7/21) | 未安装 → 无关 |
| iptables 切 nft 后端 (4/5) | 已迁移，无 .pacsave → 已处理 |
... (no shadow / sg mention at all -- it never saw the BBS shadow topic)
```

## run 3 / run 4 timeout -- what it means

`skill_eval.py` kills the pi process at the per-eval timeout (500s here),
so `exit_code=-1` and the run is scored 0/3. But the session jsonl shows
both runs **did produce a final assistant message** (1652 / 1826 chars)
that concludes "safe to upgrade" -- the timeout is `skill_eval`'s kill,
not "no output". The real miss reason is **not the timeout**: neither run
curled `viewforum.php?id=44`, so neither saw the shadow topic. Run 4 even
says "论坛的详细帖子没抓到...信息已经足够" -- it tried BBS viewtopic,
failed, and decided "enough" from news alone.

**Would removing rate-limiting let run 3/4 find shadow?** Maybe, but not
guaranteed. Run 4 says "代理一直没恢复" (the proxy/model didn't recover),
hinting 429 interrupted its BBS drill-down -- without that, it might have
continued to id=44 + 314544. BUT it had already concluded "safe" from news,
so it might not have gone back. The deeper point: run 2 (only 3 curls, no
429) also did not touch BBS -- so "not drilling into the BBS board" is a
model-behavior pattern independent of rate-limiting. The skill removes that
coin-flip by hardcoding the id=44 fetch.
