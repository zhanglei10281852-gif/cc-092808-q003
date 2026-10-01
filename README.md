# 司法鉴定检材流转与复核服务

本项目是面向司法鉴定机构的 Python 后端服务，用于登记委托或移送案件、接收带封识的检材、记录保管位置与流转、执行专业检验、安排复核并处理环境和质量告警。案件、检材、检验记录和领用审批都保存在本地 SQLite 中，关键写入带版本或幂等键，适合在单个 Linux 应用容器内运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `data/forensics.db`，也可以通过 `FORENSICS_DATABASE_PATH` 指向其他 `.db`、`.sqlite` 或 `.sqlite3` 文件。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查为 `GET /api/system/health`。首次使用可调用 `POST /api/auth/bootstrap` 创建管理员，再通过 `POST /api/auth/login` 取得 Bearer 会话令牌。鉴定业务接口统一位于 `/api/forensics`。

## 测试与构建检查

```bash
python -m pytest
python -m compileall -q app tests
```

下面两条命令分别检查 HTTP 入口和完整的入库演示链路：

```bash
python -m app.cli smoke
python -m app.cli demo
```

## 业务边界

- `app/forensics/cases.py` 管理委托机构、案件档案、委托资料与受理状态。
- `app/forensics/custody.py` 管理检材、库位容量、容器摆放、流转、领用和冻结。
- `app/forensics/examinations.py` 管理检验规程、取样、观察记录、检验结果与复核日程。
- `app/forensics/quality.py` 管理温湿度读数、偏离告警和检材领用审批。
- `app/api`、`app/services` 和 `app/repositories` 提供身份、权限、审计、后台作业及维护能力。

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时写事务。案件档案、库位、容器摆放、检验任务和领用申请采用版本号防止旧请求覆盖新状态；入库、移库、取样和传感读数使用业务键去重。专业检验保留采用的规程版本和每个检查点的观察记录，完成后可依据鉴定专业及风险策略生成下一次复核日期。会话令牌只保存摘要，审计记录不保存明文密码或令牌。

## 领用占用边界

领用批准在单个事务内原子完成三件事：申请状态带版本谓词翻转、检材账面可用量按明细数量条件扣减、向 `specimen_allocations` 写入可追踪的 held 占用（与领用明细一对一）。任一条件不满足（版本过期、并发竞争、数量不足、检材被冻结）整个事务回滚，不留下孤立占用或扣量；同一申请重试受申请状态谓词与占用唯一约束双重保护，不会重复占用。

检材的剩余可分配量 = 初始量 − 实际耗用（取样/领用/报废等流水净额）− 尚未履行的批准占用，冻结检材不参与分配。拒绝不建立占用；已批准申请取消时把 held 占用标记 released 并退回数量；履行时占用转 consumed 并写出库流水（账面可用量不再二次扣减）。申请详情的每笔明细附带占用记录与最终去向，检材详情展示每笔占用、耗用、冻结标记和剩余可分配量。

遗留的旧版已批准数据通过核对与修复入口处置：

```bash
python -m app.cli audit-allocations            # 只读核对：账实不符、超分、无占用背书、孤立占用
python -m app.cli repair-allocations           # 事务内修复：清孤立、补占用、整单超分退回重审、重校账面
```

对应接口为 `GET /api/forensics/allocations/audit` 与 `POST /api/forensics/allocations/repair`。
