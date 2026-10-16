# 共享契约说明

协议版本为 `1.0`。Schema 只描述本项目传输对象，不是 OpenAI 或 CLIProxyAPI 的原始返回结构。

Windows → Ubuntu：`POST /internal/v1/telemetry`，`Authorization: Bearer tel_...`，Content-Type application/json，上限64KiB，使用 `telemetry.schema.json`，响应使用 `telemetry-reply.schema.json`。

Ubuntu → Windows Bridge：经受限SSH隧道 `GET /internal/v1/health`，`Authorization: Bearer wkr_...`，响应使用 `health.schema.json`。只返回本机脱敏状态；不能返回原始上游响应、账户对象或配置。

`additionalProperties:false` 必须在运行时生效，不能只存在文档里。接收方拒绝多余字段时不要打印原始body。所有日期字段实际输出为UTC的Z形式；JSON Schema的date-time通常也接受偏移表示，业务校验必须归一化UTC。

Schema之外还需实现：生产环境拒绝source=mock；worker_id与认证Key绑定；seq/instance防重放；同一quota内bucket_id+window_id唯一；captured_at不得因缓存转发而变化；明显未来时间和时钟偏差处理；quota status=ok需有可用窗口或credits；mismatch/unverified的数据不能当成已确认账号额度展示；账户epoch变化不得沿用旧快照。

当 `quota=null`，表示本次心跳没有新的额度查询结果，不清空旧快照。当 quota.status 非ok，记录查询失败原因，最新成功快照独立保留；不要用失败对象中的空windows替换之前的成功数据。`captured_at` 的null不能解释成当前时间。

当 `account_usage=null`，表示未提供账号级汇总。account_usage的summary与本网关流水口径不同，页面不能相加。daily_buckets最多90条是本协议容量限制，不宣称上游仅有90天；Windows应截取近期允许条数并在页面说明范围，完整历史可后续另建分页接口。

心跳响应最多一个refresh_quota命令。命令去重与状态变化必须持久或按TTL安全恢复；不能执行任何未在Schema中的命令。命令收到后最迟在TTL范围内开始处理；过期返回expired。重复seq可以返回原ack，但不更新last_seen。

HTTP状态：认证失败401，格式/业务约束失败400或422（实现固定一种），过大413，设备被禁用403。服务器不要把带秘密的Schema validation错误原文返回。协议破坏性升级必须修改version并协同两端；未知版本拒绝而不是猜测字段语义。

固定JSON样本均为合成测试数据，不含用户账号或真实使用记录。fixtures中的窗口长度不是生产默认值。
