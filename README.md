# mihealth-api

**把小米运动健康（Mi Fitness）的云端健康数据接到你自己的项目里。**

> 逆向还原 App 内部 API 的认证与加密协议（`ssecurity` + `nonce` → SHA256 → RC4-drop1024），
> 用你自己的小米账号凭证调用 `hlth.io.mi.com`，拿到分钟级健康数据。
> ⚠️ **仅供访问你本人账号的数据**，不要拿去碰别人的账号或商用分发凭证。

[![license](https://img.shields.io/badge/license-MIT-blue)](#license)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](#)
[![platform](https://img.shields.io/badge/platform-Android%20%7C%20任何%20HTTP%20客户端-green)](#)

English · 中文

![dashboard](static/screenshot.png)

---

## ✨ 能力一览

| 类别 | 内容 |
|---|---|
| 分钟级数据 | 步数、卡路里、心率（~3400点/天）、血氧、压力、强度活动、站立、耳机噪声 |
| 周期数据 | 睡眠（含深睡/浅睡/REM 分期）、PAI、VO2 Max、体重、经期 |
| 医疗/项目 | ECG 等医疗记录、睡眠节律、睡眠日记 |
| 运动 | 记录列表（配速/心率/轨迹类型）、运动汇总、类别表 |
| 摘要 | 每日目标达成（步数/热量/活动/站立 达成率） |
| 统计/增量 | `daily_fitness` 统计、watermark 增量锚点 |
| 其它 | 亲友数据（已授权）、登录态刷新、第三方授权通道 |

- **17 种数据键**（CloudKey 全集）× **30+ 端点** × **全量历史翻页**
- 交付三件：Python 客户端、Flask HTTP 网关、Web 看板
- 凭证隔离：`config.json` 不进 git（已 `.gitignore`），仓库可安全公开

## 🚀 快速开始

```bash
pip install -r requirements.txt

# 1. 放入你的凭据（提取方法见 docs/API.md §1）
cp config.example.json config.json

# 2. 命令行试跑
python mihealth_client.py

# 3. 起 HTTP 网关 + 看板 → http://127.0.0.1:8567
python server.py

# 4.（可选）全量导出所有数据到 export/*.json
python tools/export_all.py
```

## 🔑 凭据提取（一次）

在**已登录小米账号**的 rooted 设备/模拟器上（我用的雷电 9）：

```bash
adb shell su -c "sqlite3 /data/data/com.mi.health/app_webview/Default/Cookies \
  'select host_key,name,value from cookies'"
```

| 字段 | 取哪一行 |
|---|---|
| `serviceToken` | `sts-hlth.io.mi.com` 行（长串，base64） |
| `cUserId` | 同一行 |
| `ssecurity` | `.wear.mi.com.internal.yrn.net` 行 |

⚠️ `.hlth.io.mi.com` 行的 `serviceToken` 值是 `miothealth`（sid 标记），**不是**要发的 token。

## 🌐 HTTP API（本地网关）

启动 `server.py` 后 `http://127.0.0.1:8567`：

| 路由 | 说明 |
|---|---|
| `GET /` | 数据看板（上图） |
| `GET /api/overview` | 今日摘要卡片 |
| `GET /api/series/<key>?hours=24` | 指定键时间序列 |
| `GET /api/fitness/<key>?start=&end=` | 分钟级数据，`start<=0` 拉全量历史 |
| `GET /api/daily_goals?days=14` | **每日目标达成**（健康摘要） |
| `GET /api/sport_records?days=` `/api/sport_summary` | 运动记录/汇总 |
| `GET /api/medical` `/api/project` `/api/stat/<key>` `/api/aggregated` | 医疗/项目/统计/聚合 |
| `GET /api/watermark/<key>` `/api/max_watermark` | 增量同步锚点 |
| `GET /api/latest?keys=` `/api/relatives/<sub>` `/api/raw/<path>` `/api/keys` | 最新/亲友/透传/键表 |

响应统一 `{ok, items, count, has_more}`。

## 📊 数据键（17 种，全集）

`steps` `calories` `sleep` `heart_rate` `stress` `spo2` `intensity`
`valid_stand` `energy` `goal` `pai` `blood_pressure` `blood_sugar`
`headset` `weight` `vo2_max` `menstruation`

## 🔐 协议还原（这是逆向的核心工作量）

```
nonce       = b64( random(8B) | int32(minutes_since_epoch) )
sessionKey  = b64( SHA256( b64dec(ssecurity) | b64dec(nonce) ) )   # RC4-drop1024
data        = b64( RC4(sessionKey, <JSON 参数体>) )
rc4_hash__  = b64( SHA1(METHOD&path&明文kv&sessionKey) ) → 再 RC4 加密
signature   = b64( SHA1(METHOD&path&加密kv&sessionKey) )
_nonce      = nonce
Cookie: cUserId=...; serviceToken=<长token>; locale=zh_cn
```

实测陷阱（逆向着作权）：

| 坑 | 真相 |
|---|---|
| `nextKey` 分页没用 | 服务端认 **`next_key`**（snake_case），`nextKey` 静默忽略导致永远第一页 |
| `startTime=0` + nextKey | 游标冻结重复同页——全量历史要用近期 startTime + next_key 向更老翻 |
| `.hlth` cookie 值 `miothealth` | 那是 sid 标记不是 token，认证用 `sts-hlth` 域长 token |
| 同一分钟多条记录 | 分页边界重叠 + 多 sid 源，要按 `(sid,time)` 去重取最大 |

详见 [API.md](API.md)。

## 🗂 目录

```
mihealth_client.py   # Python 客户端 + crypto
server.py            # Flask HTTP 网关（看板 + REST 代理）
static/              # index.html + Chart.js + screenshot.png
tools/               # export_all.py / dump_tokens.js(frida hook)
API.md               # 正式接口文档（本文档的精华）
RECON.md             # 逆向侦查记录
config.example.json  # 凭据模板
```

## ⚖️ 免责声明

逆向工程学习研究用途，仅供访问**你自己账号**的云端数据。本项目与小米公司无关，不附带任何担保。请遵守法律法规与平台条款，不得用于访问他人数据、批量抓取或商业转售。

## License

MIT — 保留出处即可自由使用。
