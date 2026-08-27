# 可复制到下游项目 `AGENTS.md` 的 GPU Infra 约定

将下面代码块复制到需要通过 Agent 进行 kernel 开发和 GPU 评测的项目中。把 `/absolute/path/to/gpu-infra` 替换为本机实际路径；若 `$gpu-infra` 已安装，则无需固定本地路径。

```markdown
## GPU kernel 评测

- 涉及 kernel 正确性、性能测试、A800/B200、PTXBench、FIBServe、KDA 或 GPU 队列时，使用 `$gpu-infra`。若该 Skill 尚未安装，先读取 `/absolute/path/to/gpu-infra/skills/gpu-infra/SKILL.md`，并以当前 checkout 的 `AGENTS.md`、CLI 和 task 合同为准。
- 提交前运行 `kernelctl task-check`；多个候选优先使用异步 `submit-many` 或 `fleet-submit-many`，让 Agent 继续并行探索，不要通过长时间 `wait` 占住工作流。
- `agent-gpu-broker` 是唯一 GPU 分配者。正确性检查可按 task 使用 `shared`；benchmark、sanitizer 和 profiler 必须使用 `exclusive`；不得绕过 broker 直接占卡。
- task/evaluator 拥有 workload、正确性和原始测量。进程结束不等于正确；`invalid` 是 judge 拒绝，SSH、daemon、broker、超时或结果缺失是 `unknown`，不得把 unknown 当成功或空闲。
- route 接受后固定到原 `(node_id, run_id)`，观察失败时不得自动换节点、重提或覆盖 receipt。只回收终态证据，artifact mirror 只作只读副本。
- 复用 FIBServe 等长驻 evaluator 时，先运行 `service-preflight`，复用一个 broker-held deployment；仍有 active consumer 时不得停止服务。
- 未获明确授权，不得重启或升级生产 broker/daemon、停止共享服务、取消他人任务、绕过 GPU 锁、删除 node-owned 证据、改变 evaluator/workload 验收边界。
- 不要例行添加 SHA-256 字段或重复遍历整棵目录；只有真实完整性或内容寻址边界需要且 mismatch 会改变下一步动作时才使用。
```

完整操作命令和证据语义由 [`../skills/gpu-infra/SKILL.md`](../skills/gpu-infra/SKILL.md) 维护，本片段不复制具体远程主机、socket、GPU 编号或 task 参数。
