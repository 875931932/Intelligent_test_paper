# PaperPact 校级教师共享知识库实施计划

> 日期：2026-11-12。状态：待实施，已按教师端范围修订。
> 设计权威：[校级教师共享知识库升级设计](../../CAMPUS_KNOWLEDGE_BASE_DESIGN.md)。
> 本次只创建设计与计划文档；以下清单不是已完成功能。
> 已有课程资料可直接用于导入和评测，不再设置“寻找资料/联系教师”前置任务。
> 本期不新增学生身份或学生端，不建设全校共享题库；先交付教师资料共享、检索问答、版本溯源及导入自己课程的能力。

## 1. 执行顺序与约束

依次交付 P0–P4，每个阶段包含行为验证与可演示结果。只在相关依赖就绪后进入下一阶段；所有新增表与结构升级仍由 `schema.py + init_db.py` 管理。三个封存命题工作流保持现有行为。

当前仓库有用户已有的修改与未跟踪材料。开始实施时重新核对 git 状态，对需要修改的现有文件及实际数据按备份要求建立可验证备份；不要把现有修改打包成自己的提交，不删除无关材料。

建议按下列工作包形成独立、可评审的变更集；开发中可调整文件粒度，不能跳过权限、旧版兼容和数据恢复验证。

## 2. P0：统一授权与基础契约

### P0.1 修复现有课程授权

- [ ] 在 `backend/app/api/v1/` 建立统一的课程 owner 授权依赖，服务端派生 actor 与 course 上下文。
- [ ] 覆盖资料、框架、知识目录、项目、试卷、档案、助手、任务查询、SSE 和文件读取路径。
- [ ] 审计本地存储 PUT 回退，正式共享环境不能提供无鉴权公共写入。
- [ ] queued 任务记录 actor；worker 执行及返回前复核当前权限。历史无 actor 任务按原 course owner 解析，无法解析则拒绝并可观察。
- [ ] 同一课程 owner 原有操作保持可用；管理角色不自动获取其他教师课程内容。

验证：两位合成教师分别有课程，用 A 身份请求 B 的列表、单资源、修改、删除、任务及会话接口，全部拒绝；不能只测伪造 course_id 与 resource_id 不匹配。增加真实测试登录路径，不能只依赖注入身份。

交付证据：权限回归报告、原课程主要操作回归、已发现缺口的前后对照。

### P0.2 定义新域权限与数据契约

- [ ] 保持 teacher/org_admin/admin 身份枚举，仅为 User 增加 authz_version；知识库角色作为独立成员关系。
- [ ] 定义 school/org 解析、知识库角色、read_scope/classification、错误码与状态枚举。
- [ ] 设计授权判定纯函数及数据库读取 Interface；逐项实现主设计第 6 节矩阵。
- [ ] 同步 AGENTS 对新增知识域的显式隔离规则，保留课程域强制 course_id。
- [ ] 教师的共享库角色与本人课程权限分开判定，reader 不能改共享库但仍可使用本人命题工作台。

验证：同校/跨校、换院/换校、停用、reader 自行升权、移交 owner、扩大范围、未知组织等测试。

## 3. P1：知识空间、发布与入库

### P1.1 新域 schema 与升级

- [ ] 新增 knowledge_spaces/memberships/upload_sessions/documents/document_versions。
- [ ] 新增 parse_runs/blocks/retrieval_profiles/index_builds/chunks、审计与必要复合约束。
- [ ] 新增 knowledge_jobs/outbox_events/model_calls；会话表可在 P2 激活，但任务结构先支持两种作用域。
- [ ] 在 init_db 增加幂等升级、索引、约束验证；授权相关失败不得静默忽略。
- [ ] 用两个隔离库验证空库初始化与旧库升级，重复升级两次结果一致。

关注：实际表名前缀统一 `knowledge_`，不创建假课程，不把旧任务表的 course_id 改可空。PostgreSQL 的复合 FK、有效版本指针和并发约束必须有集成验证。

### P1.2 空间与成员界面

- [ ] 新增 `/knowledge` 和空间路由、api domain、Zustand store 与必要手写类型。
- [ ] 实现创建、列表、成员设置、owner 移交、策略预览和归档。
- [ ] 普通 reader 教师仅见该库已发布资料；保留“我的课程”及本人的命题入口，不增加新用户身份或学生导航。
- [ ] 沿用现有组件与视觉，不重做原教师工作台。

验证：前后端权限一致、空状态、错误状态、深链接拒绝、分页及移动宽度不溢出。

### P1.3 文件处理与可靠任务

- [ ] 复用存储/解析/嵌入 Adapter，知识域增加持久化实现。
- [ ] 任务与 outbox 同事务，支持重试、取消、租约心跳和 fencing token。
- [ ] 实现新知识 worker handler，使用独立 knowledge 队列与并发预算。
- [ ] 接入 KnowledgeCallContext 与 recorder，模型供应商通过既有工厂。
- [ ] 文档预览支持真实章节/页码，保存解析质量警告与 source_blocks。
- [ ] 从已有课程资料选固定版本导入到私有草稿；导入后的文件不依赖课程对象存活。

验证：文件校验失败、重复上传、解析失败重试、worker 崩溃、重复派发、租约易主、reader 尝试上传均得到明确结果。

### P1.4 发布与版本治理

- [ ] 实现发布前检查、分类约束、有效期、原子切换、撤回。
- [ ] 普通读者仅见当前有效发布版本；维护者使用独立草稿预览。
- [ ] 文件读取经授权代理，不向普通客户端发长期私有文件 URL。
- [ ] 新版失败保留旧版；索引重建采用新 build 完成后切换。
- [ ] 保存旧版分类、read_scope 与责任单位快照，当前版本全校共享不能自动扩大历史版本权限。

交付演示：教师 A 导入已有资料并发布给教师 B；B 可读，撤回后立即不可读，B 的本人课程命题不受影响。

## 4. P2：授权检索与个人问答

### P2.1 数据库召回

- [ ] 构建固定 tokenizer + simple tsvector + GIN 词面通道，准确标注为全文检索而非 BM25。
- [ ] 复用 pgvector 查询、RRF 和去重逻辑，权限/版本谓词同时应用于两路召回。
- [ ] 验证中文预分词、英文标识符、数字、单字查询和 query 转义。
- [ ] 查询仅取候选正文；删除对全知识库正文逐次装载的依赖。
- [ ] 在真实 PostgreSQL 上测试精确扫描、HNSW 和高选择性过滤，记录候选不足场景。
- [ ] 验证向量模型/维度迁移的新旧 build 并存与部分表达式索引，不清空仍在服务的旧向量。
- [ ] 固定 retrieval profile 和凭据引用，学校级原子切换 active profile 与 serving_build；迁移期新发布版本同样纳入校验。
- [ ] 向量失败可词面降级；问答不承担全库补索引。

验证：同一个问题在不同权限身份下只返回各自证据；邻域扩展、重排和缓存不能带入额外文档。

### P2.2 问答与来源

- [ ] 新增个人 sessions/messages，既有课程助手历史不自动搬入公共空间。
- [ ] 新增 knowledge_answer 用途及后台绑定；仅走只读回答，不暴露命题写工具。
- [ ] 回答按 claim/source_id 结构校验，来源元数据由后端构造。
- [ ] SSE 传状态，最终答案校验后返回；支持取消、重连和结果恢复。
- [ ] 历史答案重新授权，任一依赖撤权则整条隐藏，并从后续上下文移除。
- [ ] 多轮答案继承实际使用的历史证据依赖，任务详情权限不能成为查看他人问答的旁路。
- [ ] 实现无答案、部分回答、资料冲突与模型失败的独立状态。
- [ ] 增加本人反馈及可选摘录分享，不向维护者默认开放私人对话。
- [ ] 净化资料与答案 HTML/Markdown，检验恶意标题、表格和引用高亮不能注入脚本。

验证：prompt 注入、伪引用、错误页码、跨版本引用、任务中撤权、旧会话恢复、SSE 重连越权、取消后的迟到结果。

交付演示：单库问答定位原文；修改权限后历史及新回答正确收紧；固定小评测集可以重复执行。P0–P2 构成首个可演示版本。

## 5. P3：多库与命题复用

### P3.1 同校多库与治理

- [ ] 联合最多 5 个授权空间，统一候选预算与引用编号，防止某库重复片段挤占全部上下文。
- [ ] 空间与文档筛选先校验，禁止静默接受其他学校或未选择空间的文档 id。
- [ ] 实现学院教师/全校教师共享，扩大 read_scope 前展示影响预览并校验资料分类。
- [ ] 后台增加脱敏调用、任务、索引与反馈汇总，费用缺失显示 unknown。
- [ ] 加入账号并发、队列长度、频率和文件限额，记录被限流情况。

### P3.2 知识到课程的固定版本导入

- [ ] 实现源 read + allow_course_import 与目标 owner 的双边授权。
- [ ] 明确展示独立副本和普通撤回不能回收已交付内容的语义。
- [ ] 复制源文件与兼容解析产物到 course_id 路径，创建课程独立版本。
- [ ] 写 course_knowledge_imports 来源映射与许可快照，支持幂等及源更新提示。
- [ ] 验证源删除、空间撤回、课程删除不会误删另一域数据。
- [ ] 通过原框架、知识目录、蓝图、合同和生成链完成一次回归，不修改封存引擎。

交付演示：跨两库综合回答、固定版本导入课程、源更新后原课程与已冻结内容保持可复现。

## 6. P4：评测、容量与交付

### P4.1 固定数据与实验

- [ ] 用现有资料建立 100 条人工核对题与证据定位，按主设计分类和分组拆分。
- [ ] 70 条开发集调参，30 条保留集验收；记录文档版本、身份、查询配置和模型。
- [ ] 比较现有 BM25 基线、新全文词面、仅向量、混合检索；有明确收益再评估重排。
- [ ] 记录证据召回、引用支持/完整性、拒答、失败、耗时和费用。
- [ ] 失败案例保留，不仅展示最佳结果；不将合成数据结果表述为真实教师使用效果。

### P4.2 运行、迁移与回滚

- [ ] 1 万/5 万/10 万块逐级测量，硬件、过滤选择率、并发和冷热状态进入报告。
- [ ] 通过权限全矩阵、PostgreSQL 集成、任务故障注入与原命题回归。
- [ ] 完成数据库与对象存储备份恢复演练；核对文件哈希和版本引用。
- [ ] 灰度启用知识域，验证关闭知识开关仍保留授权修复和原命题功能。
- [ ] 执行前端 build/lint 与浏览器流程检查，避免额外引入测试框架。

### P4.3 展示资料

- [ ] 更新功能说明，清楚区分原有、已升级与未来规划。
- [ ] 录制主设计中的 5 分钟演示，展示权限拒绝、版本变化与真实引用。
- [ ] 输出评测报告、关键工程决策及已知限制；简历只引用已验证成果。

## 7. 建议文件落点

以下目录和文件是拟新增，不代表当前仓库已有：

```text
backend/app/domain/knowledge_base/
  models.py                 空间、版本、索引、授权与回答契约
  policies.py               权限、分类、状态转换纯函数
backend/app/services/
  knowledge_space_service.py
  knowledge_document_service.py
  knowledge_search_service.py
  knowledge_answer_service.py
  knowledge_import_service.py
backend/app/api/v1/
  course_access.py           课程授权统一依赖
  knowledge_base.py         知识域路由；按复杂度再拆为同目录路由组
backend/app/infrastructure/tasks/
  knowledge_tasks.py        知识域任务持久化与 handler
frontend/src/pages/knowledge-center/
frontend/src/pages/knowledge-reader/
frontend/src/pages/knowledge-ask/
frontend/src/components/knowledge/
frontend/src/api/domains/knowledgeBase.ts
frontend/src/stores/knowledgeBaseStore.ts
backend/tests/unit/test_knowledge_policies.py
backend/tests/integration/test_knowledge_access.py
backend/tests/integration/test_knowledge_lifecycle.py
backend/tests/integration/test_knowledge_retrieval_postgres.py
backend/tests/integration/test_knowledge_course_import.py
backend/tests/integration/test_course_access_matrix.py
docs/evaluation/knowledge-base/
```

命名避免与现有 `domain/knowledge`、`api/v1/knowledge.py`、`pages/knowledge` 混淆；原有“知识目录”仍专指命题考点/知识卡。

## 8. 阶段完成记录模板

每个阶段报告包含：实际变更、验证命令与结果、使用的数据/模型/环境、失败项与限制、回滚方法、下一阶段依赖。没有执行的检查明确标注未执行。

本轮完成的是主设计与实施计划。业务代码、数据库、模型绑定、部署和求职材料尚未调整。
