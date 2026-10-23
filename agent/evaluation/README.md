# 评测数据与运行器

`dataset.jsonl` 包含同一结算实验室模板的 40 条确定性合成变体，TIMEOUT、CACHE_DRIFT、CAPACITY、TAX、HEALTHY 各 8 条。变体覆盖配置参数、症状表述和症状中的不可信干扰文本。这 40 条不是 40 起独立生产事故，不能据此推断生产事故率。

每条数据声明 `quantity=2`、`synthetic=true` 和 `independentProductionIncident=false`。后端探测使用 `sourcePrice` 和合同税率 `0.06`。安全基线使用容量 `10` 和税率 `0.06`。每条故障样本只改变一个配置族，其余字段保持安全值。评测标签只用于结果评分。`run_eval.py` 不会把标签发送给 Java、模型提示或 Agent 工具。

离线协议测试使用 mock，不调用 DeepSeek、Java 服务或真实 HTTP：

```powershell
python -m unittest discover -s agent/tests -v
```

启动本地 Java 实验室，并设置 `CONFIG_DOCTOR_OPERATOR_TOKEN`、`CONFIG_DOCTOR_WORKER_TOKEN` 和 `DEEPSEEK_API_KEY` 后，可运行工具型评测：

```powershell
python -m agent.evaluation.run_eval --limit 3 --approve-matching
```

`--approve-matching` 只在提案的根因和动作与评测标签完全一致时调用 operator 审批接口。Java 使用固定白名单补丁，并立即执行契约复探。错误提案不会审批。恢复率分母仍为所选样本中的全部故障项，包括未诊断正确或审批失败的样本。

运行无工具、只看症状文本的结构化回答基线：

```powershell
python -m agent.evaluation.run_eval --baseline --limit 3
```

基线提示只包含症状文本，不创建 Java 案例，也不调用 Java 接口。两种模式被执行时都会调用真实 DeepSeek API。协议测试使用 mock。评测器逐条保存 token、实际返回的模型 ID、耗时、根因、动作、证据、工具错误和网络错误。失败请求不会伪计 token。摘要记录数据集与提示词 SHA-256、模型请求别名和实际返回模型 ID。

结果及逐条 trace 默认保存在 `agent/evaluation/results`。`--limit` 只选择 JSONL 文件开头的 N 条，不改变完整数据集哈希。可用 `--output-dir` 指定 `agent/evaluation` 内的其他输出目录。

## 固定证据单次模型对照

`evidence_baseline.py` 为每条样本创建本地 case/task 并 claim 任务，然后始终按 `probe`、`config` 顺序读取两项证据，再把症状和这两项实际证据一次性发送给 DeepSeek。该对照使用 `DeepSeekProvider(max_calls=1)`，不运行 Agent 工具循环，不提交提案，也不审批配置。预期根因和动作只用于本地评分，不进入 case/task 请求或模型消息。

启动本地 Java 实验室并设置 `CONFIG_DOCTOR_OPERATOR_TOKEN`、`CONFIG_DOCTOR_WORKER_TOKEN` 和 `DEEPSEEK_API_KEY` 后运行全量 40 条评测：

```powershell
python -m agent.evaluation.evidence_baseline
```

可用 `--limit N` 做小样本运行。结果、逐条 `raw` JSON 和持续更新的摘要默认写入 `agent/evaluation/results/evidence-baseline-<run-id>`。任务会保留已采集的证据但不提交提案，Java 任务租约按服务端设置自然到期。

这是“有固定证据、没有自适应工具选择”的对照。它用于比较固定查询与 Agent 的取证和诊断结果，不能据此宣称 Agent 必然优于该基线。
