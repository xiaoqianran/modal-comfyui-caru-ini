# Task 命名规范

适用范围：`tasks/*.json`、Task JSON 内的 `name`、`outputs/<name>/`，以及脚本新建的批量任务。

## 格式

```text
NNN[-SSS][-YYYYMMDD]-short-description
```

- **NNN**：必填，从 `001` 起的三位任务序号，**必须位于最前面**。按任务创建顺序递增。
- **SSS**：可选的三位子任务序号，同一故事的多阶段任务可使用 `007-001`、`007-002`。
- **YYYYMMDD**：可选，置于编号之后，以连字符隔开；不用日期作为文件名前缀。
- **short-description**：必填，小写英文和数字，以 `-` 分隔单词。
- 任务名称只用小写 ASCII 字母、数字及连字符，不使用空格或下划线。
- 同一批任务的文件名（去掉 `.json`）与 Task JSON 的 `name` 必须一致；默认本地输出目录为 `outputs/<name>/`。
- **新建**任务必须符合规范；已经在执行或存在历史输出的旧任务不批量重命名，以免破坏恢复状态和输出索引。

## 示例

| 用途 | 名称 |
| --- | --- |
| 独立任务 | `008-storyboard-h3-batch` |
| 附带日期 | `008-20261031-storyboard-h3-batch` |
| 故事子阶段 | `007-005-before-sunset-natural-motion` |
| 下一任务 | `009-20261101-new-story` |

不符合规范：`20261031_storyboard_h3_batch`、`storyboard-008`、`008_storyboard`。

## 约定

- `tasks/` 中的序号是**业务任务编号**，与 `workflows/` 中模型工作流来源前缀无关；后者遵循 [WORKFLOW_NAMING.md](WORKFLOW_NAMING.md)。
- 新的任务生成脚本应读取目标 JSON 文件的 stem，验证上述格式，再使用该值写入 `name`，避免名称不一致。
- 单独的镜头可在任务内部继续使用 `shot_01`、`shot_02` 等名称；三位编号只约束顶层任务。
