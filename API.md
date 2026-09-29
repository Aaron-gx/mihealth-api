# 小米运动健康 (com.mi.health) 健康数据 API — 正式接口文档

| 项 | 值 |
|---|---|
| 文档版本 | 1.0 |
| 逆向对象 | 小米运动健康 `com.mi.health` v3.59.0 (versionCode 359000) |
| 协议验证 | 雷电模拟器实机登录验证（2026-09-29），Python 客户端实测打通 |
| 性质 | App 内部 API（非官方开放 API）。凭证=登录态 Cookie，仅供**本人数据**使用 |
| Base URL | `https://hlth.io.mi.com/app/v1/` |
| 传输 | HTTPS + OkHttp；请求/响应参数级加密（RC4），认证 Cookie |

---

## 1. 认证

### 1.1 请求凭据（三件套）

| 凭据 | 取值 | 来源 |
|---|---|---|
| `ssecurity` | `***REMOVED***` | WebView Cookie 库 `.wear.mi.com.internal.yrn.net` 行 `ssecurity` |
| `serviceToken` | `<redacted-service-token>` | Cookie 库 `sts-hlth.io.mi.com` 行 `serviceToken` |
| `cUserId` | `***REMOVED***` | 同上 `cUserId`；另有明文 `userId=***REMOVED***` |

提取（root 设备，SQLite）：

```bash
adb shell su -c "sqlite3 /data/data/com.mi.health/app_webview/Default/Cookies \
  'select host_key,name,value from cookies'"
```

- 有效 token 在 `sts-hlth.io.mi.com` 行；`.hlth.io.mi.com` 行的 `serviceToken=miothealth` 是 sid 标记，**不是**要发的值
- `ssecurity` 也可从 `com.xiaomi.passport` 服务令牌流获得（此处用 Cookie 库提取）

### 1.2 请求头

```
Cookie: cUserId=<cUserId>; serviceToken=<长token>; locale=zh_cn
```

> ⚠️ `userId` cookie **不要**带上，带则服务端 401。

### 1.3 凭证有效期

`serviceToken` 有服务端过期（实测即时有效）。过期后 app 会刷新；外部客户端需在 app 登录态下重新提取（见 5.1）。401 响应 `{"code":2,"message":"auth err"}`。

---

## 2. 请求/响应加密协议（CloudInterceptor + zi4 族，实测还原）

```
nonce       = b64( random(8B) | int32( minutes_since_epoch + timeDiff ) )
sessionKey  = b64( SHA256( b64decode(ssecurity) | b64decode(nonce) ) )   # 32B RC4 key
```

### 2.1 加密的请求参数（SecretData.encryptResponse=true，本 API 默认）

对每个接口，客户端发出 4 个加密参数（GET→query，POST→form）：

| 参数 | 生成步骤 |
|---|---|
| `data` | `b64( RC4(sessionKey, <JSON参数体> ) )` |
| `rc4_hash__` | 先算 `b64( SHA1( METHOD&path&明文kv&sessionKey ) )`，然后**连同它本身一起**再做 RC4 加密发出 |
| `signature` | `b64( SHA1( METHOD&path&加密后kv&sessionKey ) )`（`加密后kv` 含 rc4 后的 data 与 rc4_hash__） |
| `_nonce` | `nonce` 原文 |

其中：

- `path` = 完整路径（含 `/app/v1/`），对 GET/POST 用 `Uri.parse(path).getEncodedPath()`
- kv 串 = 按 key 字典序 `k=v` 以 `&` 连接
- `filterSignatureKeys=[data]` — 只有 `data` 参与签名/加密
- `RC4` = 标准 RC4，**丢弃前 1024 字节密钥流**（drop-1024）

### 2.2 响应解密

```
plaintext_body = RC4(sessionKey, b64decode(http_body))   # UTF-8 JSON
```

同一 nonce/sessionKey 解密；401/非加密响应走明文 `{"code":-8/-4/2,...}`。

### 2.3 非加密路径（encryptResponse=false，本 API 中少见）

`zi4.d`：`signature = HmacSHA256( subpath & sessionKey & nonce & k=v... , sessionKey )`，`_nonce` 同上，无 `rc4_hash__`。

### 2.4 实测关键实现陷阱

| 现象 | 真相 |
|---|---|
| 分页 `nextKey` 传了没用 | 服务端认的是 **`next_key`**（蛇形）。`nextKey` 静默忽略，永远返回第一页 |
| `startTime=0/1` + nextKey | 游标冻结，重复返回同一页 → 不能这样全量拉 |
| 分页正确用法 | 传**近期** `startTime` + `next_key` 游标，向更老历史回溯，直到 `has_more=false` |
| 同一分钟多条记录 | 分页边界重叠 + 多 sid 源（手表/手机）→ 按 `(sid,time)` 去重，取每分钟最大值 |

---

## 3. 通用结构与约定

- `data` 参数体 = **无空格紧凑 JSON**（Gson 序列化形式）
- 时间戳单位：**接口用 ms（13 位）**；返回的 `time`/`start_time` 等字段是 **秒（10 位）**、`update_time`/`watermark` 为毫秒/序号
- 外层统一 `{"code":int,"message":str,"result":obj|null}`
  - `code=0` 成功；`-8` invalid params；`-4` empty tag；`2` auth err
- 分页字段：`data_list` / `sport_records`、`has_more`、`next_key`（base64 游标）

---

## 4. 端点目录（app/v1/ 下，均为 GET unless noted；参数在 `data` 中）

### 4.1 健康数据（分钟级原始记录）

| 端点 | `data` 参数 | 说明 |
|---|---|---|
| `data/get_fitness_data_by_time` | `{key,startTime,endTime,reverse,next_key}` | 按时间窗口+游标分页 |
| `data/get_fitness_data_by_watermark` | `{phoneId,waterMark}` | 按增量水位（新设备可能无数据） |
| `data/get_latest_fitness_data` | `{dataList:[{key,limit,time}]}` | 各键最新条（参数待校准，返回-8） |
| `data/get_max_watermark` | `{"type":0..5}` | DataType 水位（增量同步锚点） |

**key（数据类型，CloudKey.getCloudRequestKey 实测全集）**：

| key | 数据 |
|---|---|
| `steps` | 步数/距离/热量 `{time,steps,distance(m),calories}` 分钟级 |
| `calories` | `{time,calories}` 分钟级 |
| `heart_rate` | `{time,bpm,type}` 分钟级 |
| `sleep` | 睡眠段 `{bedtime,device_wake_up_time,avg_hr,avg_spo2,avg_breath,sleep_deep_duration(min),sleep_light_duration,sleep_rem_duration,items[]}` |
| `spo2` | `{time,spo2}` |
| `stress` | 压力 `{time,stress_value,...}` |
| `weight` | `{time,weight(kg),...}` |
| `blood_pressure` | 血压 `{systolic,diastolic,...}` |
| `blood_sugar` | 血糖 `{value,...}` |
| `pai` | PAI 活力指数 |
| `energy` | 能量 `{time,energy}` |
| `intensity` | 强度活动 |
| `valid_stand` | 站立 `{time,stand}` |
| `vo2_max` | `{time,vo2_max}` |
| `menstruation` | 经期 |
| `headset` | 耳机噪声 |
| `goal` | 三环目标(rainbow) |

**fitness 返回项结构**：

```json
{"sid":"hlth.gen_...|***REMOVED***|xiaomisports_app",
 "key":"steps","time":1790651460,
 "value":"<内嵌JSON字符串>","zone_offset":28800,
 "zone_name":"Asia/Shanghai","update_time":...,"watermark":...}
```

### 4.2 医疗数据

`data/get_medical_data_by_time`、`data/get_medical_data_by_watermark`、`data/get_latest_medical_data` — 同 fitness 参数，key 覆盖 ECG 等医疗项。

### 4.3 项目数据（睡眠节律/日记/经期等）

`data/get_project_data_by_time`、`data/get_project_data_by_watermark`、`data/get_max_project_data_watermark`
— 实测含 `sleep_wake_circle`（睡眠节律环，project_id=1000，value `{cycle_day,status}`）等。

### 4.4 运动记录

| 端点 | 参数 | 说明 |
|---|---|---|
| `data/get_sport_records_by_time` | `{startTime,endTime,next_key,limit}` | 运动记录列表（游泳/跑步/骑行等） |
| `data/get_sport_records_by_watermark` | 类似 | 按水位 |
| `data/get_latest_sport_record` | `{...}` | 最新一条 |
| `data/get_sport_category_list` | `{}` | 运动类别枚举 |
| `statistics/scan_sport_summary` | `{startTime,endTime,next_key}` | 按次汇总 |
| `statistics/batch_get_sport_summary` | `{...}` | 批量汇总 |

sport_records 项：

```json
{"sid":"xiaomisports_app","key":"outdoor_running|swimming|...",
 "time":1685918920,"category":"running","value":"{...}",
 "zone_offset":28800,"update_time":...,"watermark":...}
```

运动 value 字段：`{sport_type,duration(s),distance(m),calories,avg_pace(s/km),avg_hrm,max_hrm,steps,train_effect,...}`

### 4.5 聚合日报 / 统计 / 健康摘要

| 端点 | 参数 | 说明 |
|---|---|---|
| `data/get_aggregated_fitness_data_by_time` | `{tag,key,startTime,endTime,cursor,reverse}` | tag=`daily_fitness`（实测，非 `daily`）；key=`goal`=每日目标达成 |
| `data/get_aggregated_fitness_data_by_watermark` | `{key,wm,phoneId,limit}` | 聚合增量 |
| `statistics/get_stat_data_by_time` | `{tag,key,next_key,startTime,endTime,limit,reverse}` | 活力/能量统计 |
| `statistics/get_stat_data_by_watermark` | 类似 | |
| `statistics/get_max_watermark` | `{tag,key}` | |
| `statistics/mark_migrate_hlthcenter` | `{...}` | |
| `statistics/trigger_stat` | POST `{...}` | |

**健康摘要（daily_fitness/goal）实测结构**：

```json
{"sid":"default","tag":"daily_fitness","key":"goal","time":1790640000,
 "value":"{\"goal_items\":[{\"achieved_value\":428,\"target_value\":600,\"field\":2},
           {\"achieved_value\":5478,\"target_value\":10000,\"field\":1},
           {\"achieved_value\":12,\"target_value\":30,\"field\":4}],
           \"stand_goal\":{\"achieved_value\":0,\"target_value\":12,\"field\":3},
           \"date_time\":1790640000}"}
```

`field` 映射（实测）：`1=steps` `2=calories` `3=stand_hours` `4=active_minutes`。

本地网关路由：`GET /api/daily_goals?days=14` → 已解析为按日 `goals{steps,calories,active_minutes,stand_hours}`（带去重）。

### 4.5.1 增量同步：水位变更流（实测语义）

每个数据族有**独立**的向前推进式变更流端点，核心参数只有一个 `waterMark`（`phoneId` 可省）：

| 族 | 端点 | 列表字段 | 批次上限 |
|---|---|---|---|
| fitness | `data/get_fitness_data_by_watermark` | `data_list` | 502 |
| sport | `data/get_sport_records_by_watermark` | `sport_records` | 100 |
| medical | `data/get_medical_data_by_watermark` | `data_list` | — |
| project | `data/get_project_data_by_watermark` | `data_list` | — |
| aggregated | `data/get_aggregated_fitness_data_by_watermark` | `data_list` | 参数名是 `wm` 而非 `waterMark` |

行为（实测要点）：

- 响应 `{data_list, watermark, has_more}`；**返回的 `watermark` 就是下一次要传的游标**
- `waterMark=0` 是"寻位"：返回该流**起点水位**且列表为空，需再用该水位请求一次才拿到数据
- **空页正常**：会出现 `data_list: []` 但 `watermark` 前进的批次（删除记录/占位）
- `limit` 参数**不生效**（fitness 恒 502/页，sport 恒 100/页）
- 推进到 `has_more=false` 即追平；把最终水位落库，下次从它继续 → 增量通常个位数请求

`data/get_max_watermark?type=N` 取当前最新水位（`0=FITNESS_DATA 1=SPORT_RECORD 2=RED_DOT 3=RAINBOW 4=DAILY 5=MEDICAL`），
用于首次回填后**播种游标**（否则下一轮会把历史重拉一遍）。

> 全量回填不要走水位流：从流起点推进到最新需上万次请求（水位跨度与记录数不成比例）。
> 时间窗 + `next_key` 翻页更划算。本仓库 `sync.py` 即此策略：**回填用时间、增量用水位**。

翻页的一个补充规则（实测）：稀疏数据类型（steps/intensity）单页可跨数周，因此"整页早于窗口即停页"
的判据要加上"已持有窗口内记录"的前置条件，否则会把窗口截短。

### 4.5.2 单条运动明细 / 轨迹（实测）

| 端点 | 参数（`data`） | 说明 |
|---|---|---|
| `operate/get_sport_operational_data` | `{sid, key, start_time, end_time, locale}` | 单条运动的扩展数据：`route_info`（轨迹引用）+ `course_data`（课程） |
| `running_route/get_user_routes_list` | `{start_time, next_key, limit, reverse, origin_type}` | 轨迹库列表（`route_list`），`start_time` 单位是 **ms** |
| `running_route/get_user_routes_info` | `{route_ids: [...]}` | 轨迹详情（含 FDS 文件引用） |
| `healthapp/service/gen_download_url` | 见 `FDSRequestParam`（`items`, `sid`） | 生成 FDS 预签名下载 URL（轨迹/导出文件走这里） |

`key` 必须是记录自身的运动类型（`outdoor_running` / `pool_swimming` …），
`start_time`/`end_time` 取该记录 `value` 里的同名字段（秒），缺一即 `invalid params`。

**GPS 轨迹不在运动记录里**：记录 `value` 的 79 个字段（跑步）全是汇总指标，无坐标；
单条轨迹经 `route_info.file`（FDS 对象）或轨迹库 `route_id` 获取。实测本账号
52 条记录 `route_info` 全为 null、轨迹库 0 条 —— 云端确实没有存轨迹数据。

### 4.5.3 运动记录可用字段（跑步为例，79 个）

汇总：`distance/duration/calories/total_cal/recover_time/train_effect/train_load*/vitality`
跑姿：`avg_cadence/avg_stride/avg_vertical_amplitude/avg_vertical_stride_ratio/
avg_touchdown_duration/avg_touchdown_air_ratio/forefoot_landing_duration/
heel_landing_duration/golpe_landing_duration/max_contact_time`
预测：`five_kilometre/half_marathon/full_marathon_grade_prediction_duration`、`running_ability_index/level`
心率区间：`hrm_warm_up/fat_burning/aerobic/anaerobic/extreme_duration`、`reserve_hr_zone`
其它：`vo2_max(+level)`、`training_experience`、`training_status`、`highlight_events`、
`cloud_course_id/designated_course`（课程绑定）、`rise/fall/max/min/avg_height`
游泳另有 `avg_swolf/best_swolf/avg_stroke_freq/turn_count/pool_width/valid_duration`。

### 4.6 亲友（relatives）

`relatives/get_fitness_data`、`relatives/get_latest_data`、`relatives/get_aggregated_data`
— 看授权亲友的数据，参数含 `target`（对方 userId）。

### 4.7 其它

`smart_running/week_plan`（周训练计划）、`data/get_sport_category_list`、`api/v1/token`、`api/v4/detail/config`、`login/passtoken/app/userprofile`、`login/passtoken/refresh`（登录态）、`huamisport/migrate/*`（华米账号导入）、`thirdparty/auth/token`、`thirdparty/refresh/token`（三方授权）。

### 4.8 DataType 枚举（`data/get_max_watermark` 的 type）

`FITNESS_DATA=0` `SPORT_RECORD=1` `RED_DOT=2` `RAINBOW=3` `DAILY=4` `MEDICAL=5`

---

## 5. 凭证刷新与维护

### 5.1 token 过期后的恢复（实测流程）

1. 在已登录设备上重新提取 WebView Cookie：
   `sqlite3 /data/data/com.mi.health/app_webview/Default/Cookies 'select host_key,name,value from cookies'`
2. 用 `sts-hlth.io.mi.com` 行 `serviceToken`、`cUserId` + `.wear.mi.com.internal.yrn.net` 行 `ssecurity` 更新配置。
3. 不建议拿 `.hlth.io.mi.com` 行的 `serviceToken`（那是 sid 标记）。

### 5.2 adb 自动重取（`tools/refresh_credentials.py`，实测坑）

`adb exec-out su -c "cat <file>"` **不能用**：`su` 会把命令放进 pty 执行，终端行规则把文件里的
`\n` 改写成 `\r\n`——36864 字节的 SQLite 文件会到达 36887 字节、页边界错位，SQLite 报
`database disk image is malformed`。正确姿势：

```bash
adb exec-out su -c "base64 /data/data/com.mi.health/app_webview/Default/Cookies"   # 本地 b64decode
```

base64 是纯文本、对行规则免疫；兜底方案是 `su -c cp` 到 `/data/local/tmp` 再 `adb pull`（二进制安全）。
脚本还做了尺寸规整：定位 `SQLite format 3\0` 魔数、按头部 `page_size × page_count` 截断。

### 5.3 实现注意

- `ssecurity` 在 token 轮换时可能变；若大量 401 先重提取 ssecurity
- `_nonce` 需要时钟同步；客户端与服务器时差靠 `getServiceToken` 返回的 `timeDiff` 校正（外部实现用本地即可，nonce 有效性以分钟计，偏差容忍大）
- RC4 drop-1024 是必需（`o0l` 构造函数丢弃 `o0l.b=1024B`）
- 401 自愈：客户端在鉴权失败时调用 `on_auth_error` 钩子（重取凭据→`reload_credentials`），
  成功后**自动重试当次请求**；注意重建 client 的代码要重新读配置文件，否则会拿着旧凭据继续失败

---

## 6. 附加数据族（实测补充）

### 6.1 饮食 / 体重管理（`WeightApiService`）

`diet_records` 走蛇形参数（`start_time`/`end_time` 单位**秒**）：

| 端点 | 说明 |
|---|---|
| `data/get_diet_records_by_time` | 饮食记录，参数 `{start_time, end_time, dining(0=全部), limit, reverse, next_key}` |
| `statistics/batch_get_diet_summary` | 饮食汇总（参数待校准，本账号返回 -8） |
| `diet/food_list` `diet/food_detail` `diet/food_search` `diet/food_collect(_list/_cancel)` | 食物库/收藏 |
| `diet/diet_advice` | 饮食建议 |
| `plan/goal/weight_loss/{build,today,update,user/get,user/profile_set,estimate_date,estimate_weight}` | 减重计划 |
| `data/delete_diet_records` · `data/up_diet_records` | 删除/上传 |

### 6.2 其它已确认端点

`data/last_achieved_goals`、`data/reoprt_achieved_goals`[sic]、`data/report_device_info`、
`data/up_fitness_data`、`data/up_sport_records`、`data/up_medical_data`、`data/up_project_data`、
`data/up_aggregated_fitness_data`、`data/up_third_raw_data`、`data/up_huami_raw_data`。

### 6.3 未打通 / 受限项（如实记录）

| 项 | 状态 |
|---|---|
| `healthapp/service/gen_download_url`（FDS 预签名下载） | 连接被服务端中断；疑需 App 上下文或额外头，未打通 |
| `statistics/get_stat_data_by_time` | 参数已按 bean 对齐仍返回空集，账号无对应功能数据 |
| `data/get_latest_fitness_data` | 参数待校准（返回 -8）；用 `by_time` 取最新一条即可 |
| `pv.hlthopen.io.mi.com`（官方开放 API） | 需 OAuth client_id，非个人可用 |

---

## 7. 本地网关 API（server.py，推荐项目直接对接）

启动：`python server.py` → `http://127.0.0.1:8567`
默认**读本地 SQLite**（先跑 `python sync.py backfill`）；无库时自动回退实时接口。

| 路由 | 说明 |
|---|---|
| `GET /api/health` | 认证状态 + 存储模式 + 行数 |
| `POST /api/sync` · `GET /api/sync/status` | 触发增量同步 / 查询进度（看板"同步"按钮用它） |
| `GET /api/families` | **数据清单**：各 family/key 行数与时间范围 + 数据源（手表/手机/App 线） |
| `GET /api/db/<family>` | **通用多维查询**：`?key=&start=&end=&sid=&dedup=0/1&order=&limit=&fields=meta` |
| `GET /api/db/<family>/keys` | 该族有哪些 key 及行数、数据源 |
| `GET /api/agg/<family>/<key>` | **服务端聚合**：`?field=<内层字段>&agg=sum\|max\|min\|avg\|count&bucket=<秒>&start=&end=&sid=` |
| `GET /api/export.csv` | 任意族/键导出 CSV：`?family=&key=&start=&end=` |
| `GET /api/sport_types` | 运动类型清单（含各类型条数） |
| `GET /api/sport_detail` | 单条运动扩展数据（`route_info`/`course_data`）：`?sid=&key=&start=&end=`（秒） |
| `GET /api/routes` · `/api/routes/info?ids=` | GPS 轨迹库列表 / 轨迹详情 |
| `GET /api/diet?days=` | 饮食记录（库优先） |
| `GET /api/watermark_feed/<family>?wm=` | 通用水位流（fitness\|sport\|medical\|project），walk 到最新 |
| `GET /api/fds_url` | FDS 预签名下载（受限于 §6.3，未打通） |
| `GET /api/overview` | 今日卡片（步数/热量/心率/血氧/最近睡眠/体重/PAI） |
| `GET /api/series/<key>?hours=24` | 指定键时间序列（默认去重+按时间窗） |
| `GET /api/fitness/<key>?start=0&end=<ms>` | **全量分钟级** fitness 数据（start<=0 → 翻页到底） |
| `GET /api/medical?start=&end=&key=` | 医疗记录 |
| `GET /api/project?start=&end=&key=` | 项目数据（睡眠节律等） |
| `GET /api/stat/<key>?tag=daily&start=&end=` | 统计数据 |
| `GET /api/aggregated?tag=daily&key=&start=&end=` | 聚合日报 |
| `GET /api/daily_goals?days=14` | **健康摘要**：每日目标达成（步数/热量/活动/站立，按日去重） |
| `GET /api/sport_records?days=30` | 运动记录 |
| `GET /api/sport_summary?start=&end=` | 运动汇总 |
| `GET /api/sport_categories` | 运动类别枚举 |
| `GET /api/max_watermark?type=0..5` | 数据水位 |
| `GET /api/watermark/<key>?wm=&limit=` | 按水位增量拉取 |
| `GET /api/latest?keys=steps,sleep` | 各键最新条 |
| `GET /api/relatives/<sub>?target=&start=&end=` | 亲友数据 |
| `GET /api/raw/<path>?data=<json>` | 透传任意 app/v1 端点（返回密文原文） |
| `GET /api/keys` | 全数据键清单 |
| `GET /` | 数据看板页面 |

响应统一 `{ok,items|count,has_more}`；`has_more=true` 表示还有历史未拉完（加大 `pages` 或改用 watermark）。

**多维组合示例**

```bash
# 某数据源（手表 ***REMOVED***）的步数·近 7 天·倒序
/api/db/fitness?key=steps&sid=***REMOVED***&start=<ms>&order=desc&limit=100
# 全历史每日步数合计（服务端聚合，不解码行）
/api/agg/fitness/steps?field=steps&agg=sum&bucket=86400
# 每周最高心率
/api/agg/fitness/heart_rate?field=bpm&agg=max&bucket=604800
# 只要元数据（时间/来源/键），不要 value 体
/api/db/fitness?key=steps&fields=meta&limit=100000
# 按运动类型过滤（中文名见看板下拉）
/api/sport_records?days=3650&type=outdoor_running
```

**性能设计**：去重是**写入时维护的物化表**（`dedup`，主键 `family,key,ts`），
原始行完整保留在 `records`。早期用视图实现时，SQLite 会对全表物化窗口函数——
实测大数据读取 20~80s；改成物化表后同样的读取降到 0.5s 内，聚合走一次 SQL 不解码行。

---

## 7. Python 客户端（mihealth_client.py）

```python
from mihealth_client import MiHealthClient, load_config, client_from_config
cfg = load_config()                       # config.json > 环境变量 MIH_*
C = client_from_config(cfg)               # 含区域/限流/重试/401 钩子

# 增量：水位流（返回 items, 最新水位, has_more）
items, wm, more = C.sync_by_watermark("data/get_fitness_data_by_watermark", "data_list", start_wm)
wm_now = C.get_max_watermark(0)           # 播种游标
```

```python
# 旧式直连（保留兼容）
from mihealth_client import MiHealthClient
C = MiHealthClient(SSECURITY, SERVICE_TOKEN, CUSER_ID)

# 窗口查询（自动翻页+窗口过滤）
C.get_fitness_data_all("heart_rate", start_ms, end_ms)

# 全量历史（翻页到底；服务端 next_key 实际无视 startTime，客户端过滤）
C.get_fitness_data_all("steps", 1, now, full_history=True)

# 原始单页
C.get_fitness_data_by_time("sleep", start_ms, end_ms, next_key=None)
C.get_sport_records_by_time(start_ms, end_ms)

# 已验证 crypto 原件：
#   RC4(drop=1024), gen_nonce, session_key, signed_encrypted, decrypt_response
```

---

## 8. 数据真实性说明（与官方口径的差异）

- **分钟级心率约 1215/天**：手环正常佩戴下的采样密度（全天佩戴）。
- **步数/卡路里 value 是分钟增量**，非累计值；聚合请自行累加或乘时段。
- **`nextKey`（驼峰）在 `data` 参数体内被忽略**（服务端按 `next_key` 解析）。这是逆向比对 jadx 类字段名与线上行为的结论——客户端 bean 叫 `nextKey`，服务端只认 `next_key`。SDK 里 Gson 会按声明名序列化，可能服务端兼容双写，但实测只 `next_key` 生效。
- **relatives / latest / watermark** 按 phoneId 归属：本机 `device_info.default_id` 提取的 phoneId 对 wm 接口返回空（尚未同步表），属正常——以 `next_key` 翻页路径为准。

---

*本文件由逆向工程产生，字段名以反编译代码（com.xiaomi.fitness.* / com.xiaomi.fit.fitness.*）为权威；实测值见 `work/API.md` 历次验证。*
