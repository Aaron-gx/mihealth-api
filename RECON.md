# 小米运动健康 (com.mi.health) API 侦查记录

样本: base.apk v3.59.0 (versionCode 359000), 13 dex, 无加固(R8 混淆), arm64 .so 含 libdevice-encrypt.so。

## 目标
拿到个人健康数据接口，用于自己的项目。

## 云端架构（已确认）

- **Host**: `https://hlth.io.mi.com/`（`FitnessApiService.HTTP_HOST`, `AppModule.provideHttpConfig`）
- **Path prefix**: `app/v1/`（`FitnessApiService.HTTP_PATH`）
- **区域变体**: `region.hlth.io.mi.com`（RegionUrlSwitcher 可改写 host/secret；OAuth 回调走此域）
- **认证**: HTTP Cookie `.hlth.io.mi.com` 域: `serviceToken=...; cUserId=...; locale=...`
  （`AccountServiceCookieImpl.plantHealthCookie`；`ParameterInterceptor` 只追加 locale）
- **Token 来源**: 小米账号 serviceToken（`com.xiaomi.passport.servicetoken.ServiceTokenResult` → serviceToken/security/cUserId/userId），
  `TokenManagerImpl.getServiceToken(sid)`，sid 由远程登录组件下发（未静态确认，动态时抓）
- **签名层**（仅 `@Secret` 注解接口用；数据接口未发现 @Secret → 可能是纯 Cookie）:
  - CloudInterceptor 对匹配 URL 注入 `signature`, `rc4_hash__`, `_nonce`
  - `_nonce` = base64(8B 随机 + 4B 分钟级时间戳+timeDiff)
  - RC4 会话密钥 = base64(SHA256(b64decode(ssecurity) + b64decode(nonce)))，32B，RC4-drop1024
  - 加密请求: 值做 RC4 加密 + signature=SHA1(METHOD&path&k=v...&sessionKey)
  - 非加密请求: signature=HmacSHA256(subpath&sessionKey&nonce&k=v..., sessionKey)
  - 响应按 `encryptResponse` 用同一密钥 RC4 解密
  - 类映射: zi4(顶层) di4(签名/nonce) xj4(b64/sha256) ffc(hmac) o0l(rc4) u82(b64) m0l(rc4引擎)

## 数据端点（POST `data/`、GET `data/` 均为 `data=<json>` 参数风格待动态确认）

| 用途 | 路径 (app/v1/ 下) |
|---|---|
| 健康数据按时间 | `data/get_fitness_data_by_time` |
| 健康数据按水位 | `data/get_fitness_data_by_watermark` |
| 最新健康数据 | `data/get_latest_fitness_data` |
| 医疗数据(ECG等) 按时间/水位/最新 | `data/get_medical_data_by_time`, `..._by_watermark`, `get_latest_medical_data` |
| 运动记录 | `data/get_sport_records_by_time`, `..._by_watermark`, `get_latest_sport_record` |
| 运动汇总 | `statistics/scan_sport_summary`, `statistics/batch_get_sport_summary` |
| 聚合日报 | `data/get_aggregated_fitness_data_by_time`, `..._by_watermark` |
| 统计(活力) | `statistics/get_stat_data_by_time`, `..._by_watermark`, `statistics/get_max_watermark` |
| 项目数据(睡眠节律等) | `data/get_project_data_by_time`, `..._by_watermark`, `data/get_max_project_data_watermark` |
| 亲友数据 | `relatives/get_fitness_data`, `relatives/get_latest_data`, `relatives/get_aggregated_data` |
| 水位 | `data/get_max_watermark` (body {"type":DataType值}) |
| 类别表 | `data/get_sport_category_list` |
| 训练计划 | `smart_running/week_plan` |

DataType: FITNESS_DATA=0 SPORT_RECORD=1 RED_DOT=2 RAINBOW=3 DAILY=4 MEDICAL=5

## 数据键名 (CloudKey.getCloudRequestKey)

steps, calories, sleep, heart_rate, stress, spo2, intensity, valid_stand,
energy, goal(rainbow), pai, blood_pressure, blood_sugar, headset, weight,
vo2_max, menstruation

## 请求参数结构（确认）

- getFitnessDataByTime → `GetFitnessDataByTime{key, startTime, endTime, reverse, nextKey}` (JSON 经 Gson)
- getLatestFitnessData → `LatestFitnessDataParam{dataList:[LatestFitnessDataModel{key,limit,time}]}`
- getAggregatedDataByWM → `AggregateFitnessDataByWaterMarkParam{key?, wm, phoneId, limit}`
- deleteFitnessData → `DeleteFitnessData{phoneId, [FitnessDataKey{sid,key,time}]}`
- FitnessDataKeyParam{sourceId(sid), key, startTime, clientId}
- 返回值统一 `BaseResult<T>`，由 `HttpResultHandlerImpl.handleResponse` 解包

## 其它端点（有用）

- `/login/passtoken/app/userprofile`, `/login/passtoken/refresh`（登录态）
- `huami OAuth`: `user.huami.com/oauth2` client_id=v2N0da410c4d77c4d2fa8b3cd5e4d08ec2b（绑定华米账号导入数据用）
- `thirdparty/auth/token`, `thirdparty/refresh/token`（三方授权）

## 待动态确认

1. serviceToken 的 sid 名（Frida hook `TokenManagerImpl.getServiceToken`）
2. `data=` 参数的确切序列化（GET query or POST form；gson JSON 无空格）
3. FitnessApiService 是否有 @Secret（大概率无 → 纯 Cookie）
4. 真实返回 JSON 结构（抓包看）
5. Cookie 提取方式：WebView Cookie DB / shared_prefs / frida hook

## 文件

- dex 已解至 work/dex/classes*.dex
- 全部类名字符串: work/all_classes.txt (每 dex 截断 5000)
