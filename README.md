# mihealth-api

小米运动健康（`com.mi.health`，Mi Fitness）云端健康数据 API — 逆向实现。

通过还原 App 内部接口的认证与加密协议，调用其云端服务获取个人健康数据。

[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](#)

## 用途

为需要访问**本人**小米运动健康云端数据的场景提供可编程通道：个人数据备份与导出、接入自建项目/仪表板、与第三方工具（Home Assistant、Obsidian、Grafana 等）做数据集成、接口逆向研究与学习。

- 仅供访问**使用者自己账号**名下的数据；需先在登录态设备上提取凭据（见下文）
- 与小米公司无任何关联，不是官方开放 API（官方通道为 `pv.hlthopen.io.mi.com`，需注册 OAuth client_id）
- 请遵守适用法律与平台条款使用

![dashboard](static/screenshot.png)

---

## 能力

| 类别 | 内容 |
|---|---|
| 分钟级数据 | 步数、卡路里、心率、血氧、压力、强度活动、站立、耳机噪声 |
| 周期数据 | 睡眠（深睡/浅睡/REM 分期）、PAI、VO2 Max、体重、经期 |
| 医疗/项目 | ECG 等医疗记录、睡眠节律、睡眠日记 |
| 运动 | 记录（9 种类型）× 79 项跑步指标（跑姿/成绩预测/心率区间/训练负荷）、汇总、类别表 |
| 饮食/体重管理 | 饮食记录、食物库、饮食建议、减重计划（接口已通，本账号无数据） |
| 摘要 | 每日目标达成（步数/热量/活动/站立 达成率） |
| 多维查询 | 按数据键 / 时间窗 / 数据源 / 运动类型 / 去重开关，服务端聚合与 CSV 导出 |
| 统计/增量 | `daily_fitness` 统计、watermark 增量锚点与通用水位流 |
| 其它 | 亲友数据、登录态刷新、第三方授权通道 |

- 17 种数据键（CloudKey 全集）× 30+ 端点 × 全量历史翻页
- 交付三件：Python 客户端、Flask HTTP 网关、Web 看板
- 凭证隔离：`config.json` 已 `.gitignore`，不进版本库

## 快速开始

```bash
pip install -r requirements.txt          # pycryptodome 可选：RC4 提速 ~40x

# 放入凭据（提取方法见 API.md §1；有 root 设备也可自动抓取）
cp config.example.json config.json
python tools/refresh_credentials.py       # 自动从设备读取并写回 config.json

# 1) 首次全量回填 → SQLite（17 个数据键 + 运动记录 + 每日摘要）
python sync.py backfill

# 2) 之后定时增量（水位流 + 时间窗补拉，通常只需个位数请求）
python sync.py incremental
python sync.py status                     # 游标与库内行数

# 3) HTTP 网关 + 看板 → http://127.0.0.1:8567（默认读本地库，无库时走实时接口）
python server.py

# 其它
python mihealth_client.py                 # 客户端冒烟
python sync.py export --family fitness --key steps --csv steps.csv
python tools/export_all.py                # 全量导出 JSON（不落库）
```

### 同步与存储

| 能力 | 说明 |
|---|---|
| 全量回填 | 按时间窗 + `next_key` 游标翻页拉全历史，4 线程并发（`--workers`） |
| 增量同步 | 走**水位变更流**（fitness / sport 各一条，族内向前推进），游标持久化，逐页落库 |
| 时间窗补拉 | 增量时同时补拉最近 `--hours`（默认 72h），修复断档 |
| 去重 | 分页重叠与多数据源（手表/手机）在**查询视图** `v_dedup` 收敛为每分钟一条 |
| 限流 | `--min-interval` 控制请求间隔（默认 0.4s），避免触发风控 |
| 断点续传 | 游标每页落库，中断不丢进度；`INSERT OR REPLACE` 保证重跑幂等 |
| 401 自愈 | 检测到鉴权失败自动调用 `tools/refresh_credentials.py` 重取凭据并**重试当次请求** |

数据库表：`records`（原始行，主键 `family+key+sid+ts`）、`v_dedup`（去重视图）、`sync_state`（游标）。

## 登录 / 会话（三种方式）

网页看板右上角 **登录** 按钮提供三条路径，都不需要每次都碰模拟器：

| 方式 | 说明 | 何时用 |
|---|---|---|
| **账号密码登录** | 用小米账号（手机号/邮箱/ID）+ 密码走 passport 登录，成功后保存 `passToken`。密码只用于当次请求、**不落盘**；可能弹验证码（面板会显示图片） | 首次接入 |
| **免密续期** | 用已保存的 `passToken` 换新 `serviceToken`，不需要密码/设备。passToken 有效期约 1~2 个月，可反复续期 | 日常（token 过期时自动触发） |
| **从设备导入** | 从已登录 App 的 rooted 设备/模拟器读取会话（含 `passToken`），一条命令完成 | 手上已有登录态 |

登完一次之后，`server.py` / `sync.py` 在收到 401 时会**自动免密续期并重试**，
实测链路：坏 token → `401 -> refreshed via passToken` → 写回新会话 → 数据正常。

命令行等价：

```bash
python login.py --user <小米账号> --password <密码>   # 首次（密码不留存）
python login.py --refresh                            # 之后免密续期
python tools/refresh_credentials.py                  # 从设备导入（含 passToken）
```

> 落盘内容只有 `ssecurity / service_token / cuser_id / user_id / pass_token / session_at`（`config.json`，权限 600）。
> 注意 `ssecurity` **每次会话都会变**，必须与同次会话的 token 配套使用。

### 备用：手动提取

在已登录小米账号的 rooted 设备/模拟器上：

```bash
adb shell su -c "sqlite3 /data/data/com.mi.health/app_webview/Default/Cookies \
  'select host_key,name,value from cookies'"
```

| 字段 | 位置 |
|---|---|
| `serviceToken` | `sts-hlth.io.mi.com` 行（长串） |
| `cUserId` | 同一行 |
| `ssecurity` | `.wear.mi.com.internal.yrn.net` 行 |

`.hlth.io.mi.com` 行的 `serviceToken` 值为 `miothealth`，是 sid 标记，不参与认证。

## HTTP API（本地网关）

`server.py` 启动后 `http://127.0.0.1:8567`：

| 路由 | 说明 |
|---|---|
| `GET /` | 数据看板（含"同步"按钮） |
| `GET /api/health` | 认证状态 + 存储模式 + 行数 |
| `POST /api/sync` · `GET /api/sync/status` | 触发增量同步 / 查询进度 |
| `GET /api/overview` | 今日摘要 |
| `GET /api/series/<key>?hours=24` | 指定键时间序列 |
| `GET /api/fitness/<key>?start=&end=` | 分钟级数据，`start<=0` 拉全量 |
| `GET /api/daily_goals?days=14` | 每日目标达成 |
| `GET /api/sport_records?days=` `/api/sport_summary` | 运动记录/汇总 |
| `GET /api/medical` `/api/project` `/api/stat/<key>` `/api/aggregated` | 医疗/项目/统计/聚合 |
| `GET /api/watermark/<key>` `/api/max_watermark` | 增量锚点 |
| `GET /api/watermark_feed/<family>?wm=` | 通用水位流（fitness/sport/medical/project） |
| `GET /api/latest?keys=` `/api/relatives/<sub>` `/api/raw/<path>` `/api/keys` | 最新/亲友/透传/键表 |
| `GET /api/families` | 数据清单：family/key 行数、时间范围、数据源 |
| `GET /api/db/<family>` | **多维查询**：key × 时间窗 × 数据源 × 去重开关 × 排序 × 条数 |
| `GET /api/agg/<family>/<key>` | **服务端聚合**：内层字段 × sum/max/min/avg × 任意桶宽 |
| `GET /api/export.csv` | 任意族/键导出 CSV |
| `GET /api/sport_types` · `/api/sport_records?type=` | 运动类型清单 / 按类型过滤 |
| `GET /api/sport_detail` · `/api/routes` | 单条运动扩展数据（轨迹引用）/ GPS 轨迹库 |
| `GET /api/diet?days=` | 饮食记录 |

响应统一 `{ok, items, count, has_more}`。

## 数据键（17 种）

`steps` `calories` `sleep` `heart_rate` `stress` `spo2` `intensity`
`valid_stand` `energy` `goal` `pai` `blood_pressure` `blood_sugar`
`headset` `weight` `vo2_max` `menstruation`

## 协议还原

```
nonce       = b64( random(8B) | int32(minutes_since_epoch) )
sessionKey  = b64( SHA256( b64dec(ssecurity) | b64dec(nonce) ) )   # RC4-drop1024
data        = b64( RC4(sessionKey, <JSON 参数体>) )
rc4_hash__  = b64( SHA1(METHOD&path&明文kv&sessionKey) ) → 再 RC4 加密
signature   = b64( SHA1(METHOD&path&加密kv&sessionKey) )
_nonce      = nonce
Cookie: cUserId=...; serviceToken=<长token>; locale=zh_cn
```

实测要点：

| 现象 | 结论 |
|---|---|
| `nextKey` 分页无效 | 服务端认 `next_key`（snake_case）；`nextKey` 静默忽略导致同页重复 |
| `startTime=0` + nextKey | 游标冻结重复同页；全量需用近期 startTime + next_key 回溯 |
| `.hlth` cookie `serviceToken=miothealth` | sid 标记；真实 token 在 `sts-hlth` 域 |
| 同分钟多条 | 分页重叠 + 多 sid 源 → 按 `(sid,time)` 去重取最大 |

详见 [API.md](API.md)。

## 目录

```
mihealth_client.py   # Python 客户端 + crypto（重试/限流/去重/水位/区域）
store.py             # SQLite 存储层（幂等 upsert + v_dedup 视图 + 游标）
sync.py              # 同步编排器：backfill / incremental / status / export
server.py            # Flask HTTP 网关（看板 + REST 代理，库优先·实时回退）
static/              # index.html + Chart.js + screenshot.png
tools/
  refresh_credentials.py  # adb 自动重取凭据（base64 传输避开 pty 污染）
  export_all.py           # 全量导出 JSON
  dump_tokens.js          # Frida hook 现场抓 token
API.md               # 接口文档
RECON.md             # 逆向侦查记录
config.example.json  # 凭据模板
```

## 逆向依据

- 样本：`com.mi.health` v3.59.0（13 dex，R8 混淆，无加固）
- 签名链：`com.xiaomi.fitness.app.CloudInterceptor` → `zi4 / di4 / xj4 / ffc / o0l / u82`
- 数据层：`com.xiaomi.fit.fitness.persist.server.FitnessApiService`（host/path/注解）
- 键映射：`CloudKey.getCloudRequestKey`；摘要：`CloudRainbowHelper → daily_fitness/goal`

## License

MIT。仅供个人数据访问与研究用途。
