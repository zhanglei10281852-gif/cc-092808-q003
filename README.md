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
python -m app.cli release-doctor            # 仅核对历史领用占用
python -m app.cli release-doctor --repair   # 核对并修复
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查为 `GET /api/system/health`。首次使用可调用 `POST /api/auth/bootstrap` 创建管理员，再通过 `POST /api/auth/login` 取得 Bearer 会话令牌。鉴定业务接口统一位于 `/api/forensics`。

领用申请的接口为 `POST /releases`、`/releases/{id}/submit`、`/releases/{id}/decision`、`/releases/{id}/cancel`、`/releases/{id}/fulfill`，核对修复入口为 `GET /releases/doctor/check` 与 `POST /releases/doctor/repair`。

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

领用审批采用占用模型：批准动作以版本与状态双重条件更新锁定申请行，并在同一事务内为每个明细建立 `release_allocations` 占用（`held`）。检材的可分配量为物理可用量减去全部未履行占用；后续申请、取样与检验排期都同时考虑真实耗用、未履行占用和冻结状态。拒绝不建占用，取消释放占用（`released`），履行把占用转为真实耗用（`consumed`，写入领用台账事件）。任一中间步骤失败都通过保存点回滚，不留孤立明细。申请详情、检材详情和 `/specimens/{id}/reconcile` 可以查看每笔占用、剩余可分配量和最终去向；`release-doctor` 负责核对并修复既有已批准数据。
