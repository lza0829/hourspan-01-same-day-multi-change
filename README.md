# 时档（HourSpan）

**骨架：** 门店营业属性贴源入档 → 业务键变更检测 → Type2 历史行开闭 → 当前镜投影 → 历史与当前互证

- 领域：数据工程（去皮=维表 SCD / 历史开闭 / 当前镜 / 核证；不是门店后台 CRUD）
- 端口：前端 **6200**，接口 **11200**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/hourspan-NN-*`
- 栈：Python 3.12 + FastAPI + SQLite；前端 **React + Vite**
- 避让：勿撞筑层分层汇总、价舱改价调度、信汇多源去重、变流 CDC 游标、格湾占用态

本地冒烟：

```powershell
cd claude-workspace/hourspan/_scaffold/backend
pip install -r requirements.txt
$env:DATA_DIR="$PWD/data"; $env:FIXTURES_DIR="$PWD/../fixtures"
python -m uvicorn app.main:app --host 127.0.0.1 --port 11200

cd ../frontend
npm install
npm run dev
```

或：`docker compose up -d --build`（6200 / 11200）

## 已实现邻题：same_day_multi_change（同业务键同日多变更）

- 落库形态（甲）：`dim_store_hours_hist` 连续多行，同日每次属性 hash 相对前序变化按 as_of 升序关旧开新；相邻段 `valid_to = 下段 valid_from`，不重叠不留洞；`same_day_chains` / `same_day_chain_segments` 台账挂前后 hist_id。禁止只留末日一行、禁止原地覆盖。
- 失败闭（甲）：整日多段开闭、当前镜刷新、核证条写在同一事务，提交前自检双 current / 镜末段错位即回滚；注入演练（`simulate_fail_after_mirror`）可验证整日回到变更前。
- 只读核证：`GET /api/same-day/chain?store_id=&day=`（店、日、段数、各段 valid 区间、末段 hash、链态 open/superseded、违规项）；前端「同日多段链」页协同入档/写闸/镜刷新/核证。
- 夹具：`fixtures/same_day_multi_change_batch.json`（同键同日 2–3 次变更，含 hash 相同 no-op）。

测试（stdlib，无第三方依赖）：

```powershell
cd backend
$env:DATA_DIR="$PWD/data-test"
python -m unittest app.tests.test_same_day_multi_change -v
```
