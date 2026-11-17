# Dependency Alert API

值班工程师面对一串服务故障告警时，需要先处理真正的**上游根因故障**，同时保留尚未恢复的
下游告警但予以抑制。本服务用 **FastAPI + SQLite（WAL）** 实现：

- 整版提交服务依赖图（`A` 依赖 `B`，则 `B` 是 `A` 的上游）；
- 带**每服务递增正整数序号**上报故障 / 恢复，支持幂等重试、冲突拒绝、迟到不回退；
- 查询时动态计算**需处理故障**与**被抑制故障**及其上游根因；
- 替换依赖图时按服务名保留状态，恢复后下游自动重新归类，无需重报；
- 停机影响试算：基于单快照只读计算拟停服务对关键入口的可用性影响（含备用上游组）；
- 所有写操作走序列化事务，并发上报 / 换版 / 查询不会混合不同已提交状态。

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （Swagger UI，可直接操作）
# 健康:  http://localhost:8000/health
```

SQLite 数据文件保存在命名卷 `alert-data`（容器内 `/data/alerts.db`）。

### 本地直接运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ALERT_DB_PATH=./data/alerts.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/graph` | 整版替换依赖图。校验失败（环路 / 未知引用 / 重名 / 重边 / 自依赖）返回 `422` 并**保留旧版** |
| `GET` | `/graph` | 读取当前已提交图（含内部修订号 `revision`） |
| `POST` | `/reports` | 上报故障 / 恢复 |
| `GET` | `/alerts` | 查询告警分类（需处理 / 被抑制 / 根因 / 路径） |
| `POST` | `/simulations/downtime` | 停机影响试算（别名 `POST /maintenance/impact`，请求/响应相同） |
| `POST` | `/admin/reset` | 清空全部数据（测试 / 运维辅助） |
| `GET` | `/health` | 健康检查 |

### 提交依赖图

```json
POST /graph
{
  "version": "v1",
  "services": [
    {"name": "A", "dependencies": ["B", "C"]},
    {"name": "B", "dependencies": ["D"]},
    {"name": "C", "dependencies": ["D"]},
    {"name": "D", "dependencies": []}
  ]
}
```

`dependencies` 中列出的是**上游**服务；所有被引用服务必须在同一次提交中出现，
整图必须是 DAG（Kahn 算法检环）。每次成功提交 `revision` 递增。

### 上报故障 / 恢复

```json
POST /reports
{"service": "B", "seq": 7, "status": "fault"}
```

序号 `seq` 为**每个服务各自维护的严格递增正整数**，规则：

| 情况 | 结果 |
| --- | --- |
| `seq > last_seq` | 应用新状态 |
| `seq == last_seq` 且内容相同 | 幂等重试，`applied=false, duplicate=true`，状态不变 |
| `seq == last_seq` 但内容不同（fault/recover 互斥） | `409 conflict`，状态不变 |
| `seq < last_seq`（迟到上报） | `409 conflict`，**绝不回退状态** |
| 服务不在当前图中 | `404 unknown_service` |
| `seq <= 0` | `422`（请求体校验） |

序号允许有空洞（如 1 之后直接 3）。每次上报（含被拒绝的）都写入 `events` 审计表。

### 查询告警

```json
GET /alerts
{
  "graph_revision": 1,
  "graph_version": "v1",
  "services": ["A", "B", "C", "D"],
  "actionable_faults": [{"service": "D", "last_seq": 1}],
  "suppressed_faults": [
    {
      "service": "B",
      "last_seq": 1,
      "root_causes": ["D"],
      "paths": [["B", "D"]]
    }
  ],
  "down_services": ["B", "D"],
  "unreported_services": ["C"],
  "healthy_services": []
}
```

## 抑制规则（核心语义）

1. 只有当前仍处于故障（down）状态的服务才会出现在 `actionable_faults` 或
   `suppressed_faults` 中，两类互斥且并集恰为全部 down 服务。
2. **根因**：故障且没有任何故障上游的服务。根因永远是需处理故障。
3. 一个非根因故障被抑制，当且仅当存在一条到达某根因的依赖路径，且**路径上每个节点都
   处于故障中**。
4. 健康节点或**从未上报**的节点会截断路径：其下游即使故障也不能经由该路径被抑制；
   若所有路径都被截断，该故障仍归入需处理。
5. 分类是查询时根据当前图 + 当前状态**动态计算**的。服务恢复、依赖图换版后立即重新
   归类，不需要下游重新上报。

## 停机影响试算（`POST /simulations/downtime`）

值班工程师计划在维护窗口停掉一台服务前，用该接口确认关键入口是否仍有可用的
备用上游。试算**只读**：不修改告警、上报状态与审计事件；数据取自**同一个已提交
快照**（图 + 最新上报状态在同一读事务内读取），因此并发换图 / 上报不会产生混版结果。

### 请求

```json
{
  "stopped_service": "db",
  "entries": ["ingress"],
  "backup_groups": [
    {"service": "ingress", "members": ["auth", "cache", "standby"]}
  ]
}
```

- `stopped_service`：拟停服务，必须在当前图中，否则 `422`。
- `entries`：关键入口，至少一个；未知或重复的入口名返回 `422`。
- `backup_groups`：各服务的备用上游组（可省略）。**组成员必须是该服务在当前图中的
  直接依赖**；未知服务、非直接依赖、组内重复成员、空组一律 `422`。同一服务可以声明
  多个组，未被任何组覆盖的依赖按未分组依赖逐个要求可用。

### 可用性语义

- 只有上报过 `recover`（当前 `up`）的节点内在可用；**故障或从未上报的节点不可用**。
- 可用性沿依赖边**传递**：一个服务可用，当且仅当
  1. 自身内在可用且不是拟停服务；
  2. 每个未分组直接依赖都可用；
  3. 每个备用上游组**至少有一台成员可用**（成员自身的上游故障也会使该成员不可用，
     即备用服务的上游故障同样计入）。
- 停机后的试算把 `stopped_service` 强制视为不可用，其余规则不变。

### 响应

```json
{
  "graph_revision": 2,
  "graph_version": "prod-2",
  "stopped_service": "db",
  "already_unavailable_entries": [],
  "newly_interrupted_entries": [
    {
      "entry": "ingress",
      "reasons": [
        {
          "type": "group_lost_available_member",
          "detail": "backup upstream group ['auth', 'cache', 'standby'] of service 'ingress' has no available member",
          "dependency": null,
          "group_members": ["auth", "cache", "standby"]
        }
      ]
    }
  ],
  "after_unavailable_entries": ["ingress"]
}
```

- `already_unavailable_entries`：**停机前**本来就不可用的入口。
- `newly_interrupted_entries`：停机前可用、停机后不可用的入口，并逐条说明原因：
  - `self_stopped`：入口自身就是拟停服务；
  - `group_lost_available_member`：某个备用上游组失去全部可用成员（附组内成员）；
  - `dependency_unavailable`：未分组直接依赖不可用，原因沿依赖链递归到最深的失败点
    （可能最终指向拟停服务），`dependency` 字段标出直接依赖。
- `after_unavailable_entries`：停机后全部不可用入口（前两者的并集）。
- 尚未提交任何图时调用返回 `409 no_graph`。

## 换版与状态保留

- 服务状态（up/down、最后序号）独立于图存储，按服务名为主键。换版后同名服务的状态与
  序号历史原样保留；被移除服务的状态行不删除但不会出现在查询中，将来重新加入时恢复可见。
- 新图中首次出现的服务归入 `unreported_services`。

## 并发模型

- SQLite 开启 WAL；每个写事务以 `BEGIN IMMEDIATE` 开始，写者全局串行化，遇
  `SQLITE_BUSY` 短时重试。
- 读事务持有单一已提交快照，因此换版与查询并发时，每次 `/alerts` 看到的节点集、边与
  修订号必然属于同一个已提交版本，不会出现混合状态。
- 每个请求使用独立的线程级连接，请求结束即关闭。

## 自测

`tests/selftest.py` 仅依赖标准库，对运行中的服务做端到端校验：

```bash
python tests/selftest.py                      # 默认 http://127.0.0.1:8000
BASE_URL=http://localhost:8000 python tests/selftest.py
# 容器内：docker compose exec alert-api python tests/selftest.py
```

覆盖：图校验与原子替换、序号幂等 / 冲突 / 迟到语义、全故障路径抑制与健康 / 未上报截断、
恢复即时重分类、换版状态保留，以及同序号并发重试、同序号异内容竞争、多序号乱序竞争、
并发换版、换版与查询交错等关键并发场景；并包含停机影响试算的无效引用拒绝、未上报节点
不可用、可用性传递、备用组保活 / 全灭归因、备用成员上游故障计入、自身停机归因、试算只读
与并发快照一致性等场景（共 92 项断言）。

## 目录结构

```
app/
  main.py                FastAPI 路由与异常映射
  database.py            SQLite 连接、schema、事务管理
  graph_service.py       图校验、整版替换、告警分类
  report_service.py      上报序号语义
  simulation_service.py  停机影响试算（只读，单快照）
  schemas.py             Pydantic 模型
  errors.py              领域错误
tests/selftest.py        端到端自测（标准库）
Dockerfile, docker-compose.yml, requirements.txt
```
