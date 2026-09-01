# YanQiu Public Collector

这是“研球智策”的无模型公开采集外壳，只负责读取 500.com 北京单场五个公开市场并向私有 Turso 数据库追加快照。

仓库不包含模型、数据库文件、冻结预测、历史结算或访问密钥。数据库凭据只能配置为 GitHub Actions Secrets：

- `TURSO_DATABASE_URL`
- `TURSO_AUTH_TOKEN`

首次部署时先手动运行 `Cloud source connectivity smoke test`。只有五个市场全部通过后，才创建仓库变量 `COLLECTOR_ENABLED=true`。定时任务随后每五分钟启动一次；GitHub 不保证计划任务精确准点，下一次成功运行会继续追加采集。

云端快照 ID 从 `10000000` 开始，避免以后以只追加方式合并本机历史库时发生主键冲突。

本地单元测试：

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```
