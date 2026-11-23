# API数据接入与消息流处理平台

面向第三方API和消息队列的数据接入、流式处理、水位管理和断点续采的采集治理平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8017/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5197/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8017`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| API连接器 | `api_connector` | API连接器 | 连接器名称、API端点、认证方式 |
| 消息流源 | `stream_source` | 消息流源 | 源名称、中间件类型、Topic/Queue名 |
| 偏移量管理 | `offset_manager` | 偏移量记录 | 消费组、Topic/Queue、分区/分片 |
| API调用调度 | `api_scheduler` | 调用计划 | 计划名称、关联API、Cron表达式 |
| 记录检查点 | `record_checkpoint` | 检查点 | 关联接入任务、检查点类型、检查点值 |
| 消息解析器 | `message_parser` | 解析模板 | 模板名称、消息格式、字段映射 |
| 去重中心 | `dedup_center` | 去重键 | 去重键名、关联数据源、去重窗口秒数 |
| 水位追踪 | `watermark_tracker` | 水位记录 | 数据源、水位字段、当前水位 |
| 频控保护 | `rate_guard` | 频控规则 | 规则名称、限流维度、每秒上限 |
| 失败隔离区 | `failure_isolation` | 隔离记录 | 失败消息ID、失败原因、原始Topic |
| 流数据处理审计 | `stream_audit` | 审计明细 | 处理时间、数据源、消息总数 |
| 批量消息回放 | `batch_replay` | 回放任务 | 回放范围、开始时间、结束时间 |
| 字段转换器 | `field_transform` | 转换规则 | 规则名称、源字段、目标字段 |
| API调用日志 | `api_logger` | 调用日志 | 调用时间、API名称、请求参数摘要 |
| 数据落地配置 | `data_sink` | 落地配置 | 配置名称、目标存储类型、写入模式 |
| Schema注册中心 | `schema_registry` | Schema版本 | Schema名称、版本号、字段定义 |
| 流式数据导出 | `stream_export` | 导出任务 | 任务名称、数据源、导出格式 |
| 续采策略 | `resume_policy` | 续采规则 | 规则名称、续采起点类型、续采步长 |
| 消息载荷存储 | `payload_store` | 原始载荷 | 采集批次、消息ID、原始载荷 |
| 吞吐量告警 | `throughput_alarm` | 告警规则 | 规则名称、监控数据源、吞吐量下限 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 耗时字段迁移（API调用日志）

调用耗时从"文本备份"迁移为可运算的整数毫秒，规则集中放在
`backend/app/services/elapsed.py`：

- 整数或整数文本按毫秒读取；合法的 `0` 是有效测量（快速响应）；
- 空值、空白和 `未记录`、`n/a` 等无法识别的文本进入未知明细，毫秒保持为空，
  任何统计都不拿 0 补齐；
- 每行同时保留 `耗时原文`，迁移前的实际文本可以逐行追查；
- 重算只依赖结案时固化的输入快照，不依赖日志行之后是否被更正。

上线期间的双读入口：

| 入口 | 用途 |
| --- | --- |
| `GET /api/api_logger`（及详情、导出） | 旧字段形状：`耗时毫秒` 仍是字符串，条数与分页不变 |
| `GET /api/api_logger/legacy` | 与旧列表等价的完整旧视图 |
| `GET /api/api_logger/elapsed` | 逐行核对：原始文本 / 毫秒值 / 状态 / 未知原因 |
| `POST /api/api_logger/migration` | 分批迁移，`stop_after` 可中断，重入即续跑 |
| `POST /api/api_logger/migration/rollback` | 按批或全部回滚；补正过的行保留补正值 |
| `GET /api/api_logger/migration` | 迁移进度、剩余行数和批次边界 |

调度结案关联本轮日志：

| 入口 | 用途 |
| --- | --- |
| `POST /api/api_scheduler/{id}/close` | 固化本轮输入快照与慢请求统计；`revision_of` 出修订 |
| `GET /api/api_scheduler/{id}/closures` | 版本历史，逐版复核统计与快照是否一致 |

结案统计把"有效样本"和"未知数量"分列，慢请求率的分母固定为有效样本数。
同一轮再次结案必须显式给出 `revision_of`：输入没变时沿用同版结果，输入被更正
（如把文字占位补正为真实毫秒）时生成修订版并列出被更正的日志；两版都保留
各自的输入快照，旧版结论不会被新版覆盖。

### 核对脚本

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -v      # 单元测试
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8123 &
.venv/bin/python scripts/verify_elapsed_migration.py    # 端到端场景
```

端到端脚本覆盖：`0`、`12`、`105`、文字占位四笔 → 迁移到一半写入新的 `50ms`
→ 结案 → 补正占位为 `200` → 出修订 → 回滚 → 核对旧入口与导出兼容。


领域：数据工程
子方向：数据采集、爬虫与RPA
