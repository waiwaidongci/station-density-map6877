# TESTING — 补测空区（gb-0042）

## 求解器 oracle

`test_empty_box.py` 中的 `brute()` 枚举六个边界的所有候选组合，作为独立 oracle；
测试对随机小样本比对最优边界与体积，另覆盖盒面不占据、同体积字典序、检查点恢复与输入校验和。

```bash
cd backend
python -m pytest ../test-artifacts/gb-0042/test_empty_box.py -q
```

运行环境：Python 3.11，需安装 `requirements.txt`；接口测试使用 `fakeredis` 替换 Redis。

## 接口与并发流程

- `test_api_flow_and_permissions`：viewer 403、范围越界/顺序颠倒/缺项 422、完成后 resume 409、发布结果回查盒内无站。
- `test_stale_worker_cannot_publish_after_range_change`：改动范围后旧 token 的检查点/发布/失败写入全部被条件 UPDATE 拒绝。
- `test_crash_resume_and_idempotent_retry`：模拟进程中断后从检查点继续，结果与独立完整求解一致；重复投递幂等。
- `test_worker_restart_requeues_orphaned_running_search`：worker 重启时遗留的 running 搜索重新入队并从检查点完成。

## 浏览器核对（人工）

1. 用 analyst 登录，打开任一已完成任务的「完整结果」页。
2. 在「补测空区」表单核对默认范围（冻结样本最小值/最大值），提交后观察状态从排队→搜索中→已发布。
3. 发布后核对边界、跨度、体积与 `volume_exact`，确认盒面提示与同体积规则文案。
4. 修改任一范围后重新提交，确认修订号递增、旧结果不会短暂回显。

截图：`7-section.png`（桌面已发布态）、`8-section-mobile.png`（390px）、`5-pending-after-edit.png`（改范围后旧答案消失、显示排队）。
