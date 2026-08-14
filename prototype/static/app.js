const appState = { data: null };
const q = (selector) => document.querySelector(selector);

function esc(value) {
  return String(value === undefined || value === null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function notice(text, kind) {
  const node = q("#message");
  node.textContent = text;
  node.className = "message " + (kind || "info");
  node.hidden = false;
  window.clearTimeout(notice.timer);
  notice.timer = window.setTimeout(() => { node.hidden = true; }, 6500);
}

async function request(path, options) {
  const response = await fetch(path, options || {});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error || ("请求失败（" + response.status + "）"));
  }
  return payload;
}

function questionRow(type, count, score) {
  const selected = (value) => value === type ? " selected" : "";
  return '<div class="section-row">' +
    '<select class="section-type">' +
    '<option value="single_choice"' + selected("single_choice") + '>单项选择题</option>' +
    '<option value="true_false"' + selected("true_false") + '>判断题</option>' +
    '<option value="fill_blank"' + selected("fill_blank") + '>填空题</option>' +
    '<option value="short_answer"' + selected("short_answer") + '>简答题</option>' +
    '<option value="comprehensive"' + selected("comprehensive") + '>综合题</option>' +
    '</select>' +
    '<input class="section-count" type="number" min="1" value="' + count + '">' +
    '<input class="section-score" type="number" min="0.5" step="0.5" value="' + score + '">' +
    '<strong class="section-subtotal">' + (Number(count) * Number(score)) + '</strong>' +
    '<button class="remove-row" type="button" title="删除">×</button>' +
    '</div>';
}

function updateTotal() {
  let total = 0;
  document.querySelectorAll(".section-row").forEach((row) => {
    const count = Number(row.querySelector(".section-count").value || 0);
    const score = Number(row.querySelector(".section-score").value || 0);
    const subtotal = count * score;
    row.querySelector(".section-subtotal").textContent = String(subtotal);
    total += subtotal;
  });
  q("#scoreTotal").textContent = "合计：" + total + " 分";
}

function materialArea(material) {
  return material.material_area === "outline" ? "outline" : "teaching_material";
}

function materialStatusLabel(status) {
  const labels = {
    staged: "待整理",
    organizing: "整理中",
    candidate: "待确认知识库",
    ready: "已发布",
    needs_teacher_review: "需教师处理",
    framework_candidate: "待确认框架",
    framework_ready: "框架已确认",
  };
  return labels[status] || "状态异常";
}

function materialStatusTag(status) {
  if (["ready", "framework_ready"].includes(status)) return "ready";
  if (["staged", "framework_candidate"].includes(status)) return "staged";
  if (status === "candidate") return "candidate";
  return "error";
}

function renderMaterialList(list, materials, area) {
  if (!materials.length) {
    list.innerHTML = area === "outline"
      ? '<p class="muted">尚未上传大纲。可一次选择教学大纲和考核大纲。</p>'
      : '<p class="muted">尚未上传教学材料。上传后请勾选需要整理的文件。</p>';
    return;
  }
  list.innerHTML = materials.map((material) => {
    const selectable = ["staged", "needs_teacher_review"].includes(material.status);
    const tag = materialStatusTag(material.status);
    const canOrganize = area === "teaching_material";
    return '<label class="material-row' + (canOrganize ? "" : " outline-material-row") + '">' +
      (canOrganize
        ? '<input type="checkbox" class="material-check" value="' + esc(material.id) + '" data-material-area="' + esc(materialArea(material)) + '"' + (selectable ? "" : " disabled") + '>'
        : '<input type="checkbox" class="outline-check" value="' + esc(material.id) + '"' + (selectable ? "" : " disabled") + '>') +
      '<span><span class="filename">' + esc(material.original_filename) + '</span><br>' +
      '<span class="material-meta">' + Math.ceil(material.size_bytes / 1024) + " KB · " + esc(material.uploaded_at) + '</span></span>' +
      '<span class="tag ' + tag + '">' + esc(materialStatusLabel(material.status)) + '</span>' +
      '</label>';
  }).join("");
}

function renderFramework(data) {
  const summary = q("#frameworkSummary");
  const area = q("#frameworkCandidateArea");
  const framework = data.framework || {};
  const run = framework.framework_run;
  const confirmed = framework.framework;
  if (confirmed && confirmed.status === "confirmed") {
    summary.innerHTML = '<div class="run-card"><strong>命题框架已确认</strong> · ' +
      esc(confirmed.anchors.length) + ' 个范围/考核锚点。后续教学材料只会在这些锚点范围内形成候选知识点。</div>';
    area.innerHTML = '<div class="candidate-grid">' + confirmed.anchors.map(renderFrameworkAnchor).join("") + '</div>';
    return;
  }
  if (run) {
    const issues = [...(run.errors || []), ...(run.warnings || [])].filter(Boolean);
    const issueHtml = issues.length
      ? '<details class="run-issues"><summary>查看整理提示（' + issues.length + '）</summary><ul>' +
        issues.slice(0, 8).map((issue) => '<li>' + esc(issue) + '</li>').join('') +
        (issues.length > 8 ? '<li>其余提示请查看后端诊断日志。</li>' : '') +
        '</ul></details>'
      : '';
    summary.innerHTML = '<div class="run-card">大纲整理状态 <strong>' + esc(run.status) +
      '</strong> · ' + (run.stats?.anchors || 0) + ' 个候选锚点。' + issueHtml + '</div>';
  } else {
    summary.innerHTML = '<div class="run-card">尚未整理大纲。请勾选教学大纲和考核大纲后开始。</div>';
  }
  if (!run || run.status !== "awaiting_teacher_confirmation") {
    area.innerHTML = "";
    return;
  }
  const anchors = Object.values(data.framework?.candidate_anchors || {});
  area.innerHTML = '<div class="candidate-grid">' + anchors.map(renderFrameworkAnchor).join("") +
    '</div><div class="candidate-actions"><button class="primary" id="confirmFrameworkButton" type="button">确认命题框架</button></div>';
  q("#confirmFrameworkButton").addEventListener("click", () => confirmFramework().catch((error) => notice(error.message, "error")));
}

function renderFrameworkAnchor(anchor) {
  const list = (values) => Array.isArray(values) && values.length
    ? '<ul>' + values.map((value) => '<li>' + esc(value) + '</li>').join('') + '</ul>'
    : '<span class="muted">未提取</span>';
  const kind = anchor.outline_kind === "assessment" ? "考核大纲" : "教学大纲";
  return '<article class="point-card"><span class="tag ' + (anchor.outline_kind === "assessment" ? "ready" : "") + '">' +
    kind + '</span><h4>' + esc(anchor.name) + '</h4>' +
    '<p class="muted">来源：' + esc(anchor.source_material_name || '') + ' · ' + esc(anchor.source_location || '') + '</p>' +
    '<p>' + esc(anchor.scope_text || anchor.scope || '') + '</p>' +
    '<div class="framework-detail"><strong>教学要求</strong>' + list(anchor.teaching_requirements) +
    '<strong>考核要求</strong>' + list(anchor.assessment_requirements) +
    '<strong>能力层级</strong>' + list(anchor.capabilities) +
    '<strong>排除项</strong>' + list(anchor.exclusions) + '</div></article>';
}

function renderMaterials(materials) {
  renderMaterialList(q("#outlineMaterialList"), materials.filter((material) => materialArea(material) === "outline"), "outline");
  renderMaterialList(q("#teachingMaterialList"), materials.filter((material) => materialArea(material) === "teaching_material"), "teaching_material");
}

function diagnosticText(record) {
  const response = record.response || {};
  const request = record.request || {};
  const parts = [
    record.at,
    "阶段=" + (record.stage || "—"),
    "结果=" + (record.outcome || "—"),
    "第" + (record.attempt || 1) + "次",
  ];
  if (record.http_status) parts.push("HTTP " + record.http_status);
  if (record.elapsed_ms !== undefined && record.elapsed_ms !== null) parts.push(record.elapsed_ms + "ms");
  if (record.gateway_request_id || record.provider_request_id || response.response_id) {
    parts.push("请求ID=" + (record.provider_request_id || record.gateway_request_id || response.response_id));
  }
  if (response.finish_reason !== undefined && response.finish_reason !== null) parts.push("finish=" + response.finish_reason);
  if (response.content_length !== undefined && response.content_length !== null) parts.push("content=" + response.content_length + "字");
  if (response.reasoning_content_length !== undefined && response.reasoning_content_length !== null) parts.push("reasoning=" + response.reasoning_content_length + "字");
  if (response.raw_body_length !== undefined && response.raw_body_length !== null) parts.push("响应体=" + response.raw_body_length + "字节");
  if (response.knowledge_points_type) parts.push("knowledge_points=" + response.knowledge_points_type);
  if (Array.isArray(response.json_top_level_keys)) parts.push("顶层字段=" + response.json_top_level_keys.join(","));
  if (request.batch_index) parts.push("批次=" + request.batch_index);
  if (record.error) parts.push("原因=" + record.error);
  return parts.join(" · ");
}

function renderOrganization(data) {
  const org = data.organization;
  const run = org.candidate_run;
  const active = org.active_counts;
  q("#organizationSummary").innerHTML = '<div class="run-card">' +
    "已发布索引 v" + data.index_version + "：" + active.materials + " 份资料、" +
    active.knowledge_points + " 个知识点、" + active.evidence_chunks + " 条证据。" +
    (run ? "<br>候选整理 " + esc(run.id) + "：状态 <strong>" + esc(run.status) + "</strong>，" +
      (run.stats.files || 0) + " 份文件、" + (run.stats.chunks || 0) + " 条可用证据、" +
      (run.stats.knowledge_points || 0) + " 个知识点。" +
      ((run.stats.excluded_chunks || 0) ? " 已排除 " + run.stats.excluded_chunks + " 条非教学/非考核内容。" : "") +
      (run.stats.organize_concurrency ? " 文件并行度 " + run.stats.organize_concurrency + "。" : "") : "") +
    "</div>";
  const area = q("#candidateArea");
  if (!run) {
    area.innerHTML = "";
    return;
  }
  const messages = (run.warnings || []).concat(run.errors || []);
  const diagnostics = (data.model_diagnostics || []).filter((record) =>
    (record.stage === "organize" || record.stage === "organize_validation") &&
    (!run.started_at || !record.at || record.at >= run.started_at)
  );
  const feedback = (messages.length ? '<div class="run-card"><strong>整理提示</strong><br>' + messages.map(esc).join("<br>") + "</div>" : "") +
    (diagnostics.length ? '<details class="run-card"><summary><strong>模型诊断（最近 ' + diagnostics.length + ' 条）</strong></summary><p class="muted">仅记录请求元数据、响应长度、完成原因和错误摘要；不保存密钥或资料正文。</p>' +
      diagnostics.map((record) => "<div>" + esc(diagnosticText(record)) + "</div>").join("") + "</details>" : "");
  if (run.status !== "awaiting_teacher_confirmation") {
    area.innerHTML = feedback || '<div class="run-card">本次整理未形成可确认的候选知识库，请检查文件解析提示和模型诊断。</div>';
    return;
  }
  const cards = data.candidate_knowledge_points.map((point) => {
    return '<article class="point-card">' +
      '<span class="tag ' + (point.importance === "key" ? "ready" : "") + '">' +
      (point.importance === "key" ? "重点" : "普通") + "</span>" +
      "<h4>" + esc(point.name) + "</h4>" +
      "<p>" + esc(point.chapter) + " · 置信度 " + Math.round(point.confidence * 100) + "%</p>" +
      "<p>" + esc(point.description || point.review_reason || "无额外说明") + "</p>" +
      "<p>关联证据：" + point.evidence_ids.length + " 条</p>" +
      "</article>";
  }).join("");
  area.innerHTML = feedback +
    "<h3>候选知识点（确认后才进入 RAG）</h3>" +
    '<div class="candidate-grid">' + cards + "</div>" +
    '<div class="candidate-actions"><button class="primary" id="publishButton" type="button">确认并发布候选知识库</button></div>';
  q("#publishButton").addEventListener("click", () => publishCandidate().catch((error) => notice(error.message, "error")));
}

function renderBlueprint(blueprint) {
  const node = q("#blueprintPreview");
  if (!blueprint) {
    node.innerHTML = '<p class="muted">发布资料库后，在此构建可执行题位计划。</p>';
    return;
  }
  const typeText = blueprint.sections.map((section) => section.label + " " + section.count + "×" + section.score_per_item).join("；");
  const coverage = blueprint.coverage.map((item) => "<span>" + esc(item.name) + " · " + item.planned_items + " 题</span>").join("");
  node.innerHTML = "<strong>蓝图已确认（原型）</strong><br>" +
    esc(blueprint.course.name) + " · " + blueprint.paper_total_score + " 分 · " +
    blueprint.course.duration_minutes + " 分钟 · 索引 v" + blueprint.index_version + "<br>" +
    "题位：" + blueprint.plan_items.length + " 道；题型：" + esc(typeText) +
    '<div class="coverage">' + coverage + "</div>";
}

function itemAnswer(item) {
  const answer = item.answer || {};
  if (answer.correct_option) return "正确选项：" + answer.correct_option;
  if (answer.value) return "判断：" + answer.value;
  if (Array.isArray(answer.accepted_answers)) return "可接受答案：" + answer.accepted_answers.join(" / ");
  return answer.reference_answer || "未提供";
}

function renderPaper(paper) {
  const node = q("#paperOutput");
  if (!paper) {
    node.innerHTML = '<p class="muted">完成蓝图后可生成候选卷。请先在界面检查候选知识点和题位分配。</p>';
    return;
  }
  const sections = paper.sections.map((section) => {
    const items = section.items.map((item, index) => {
      const options = item.options ? '<ul class="options">' + item.options.map((option) => "<li>" + esc(option.label) + ". " + esc(option.text) + "</li>").join("") + "</ul>" : "";
      const evidence = (item.evidence_ids || []).map((id) => paper.evidence_snapshot[id]).filter(Boolean).map((record) => {
        const shortText = record.text.slice(0, 220) + (record.text.length > 220 ? "…" : "");
        return "<li>" + esc(record.source) + '<br><span class="muted">' + esc(shortText) + "</span></li>";
      }).join("");
      const rules = (item.scoring_rules || []).map((rule) => esc(rule.criterion) + "（" + esc(rule.score) + "分）").join("；");
      return '<article class="paper-item">' +
        "<h4>" + (index + 1) + ". " + esc(item.stem) + ' <span class="tag">' + section.score_per_item + " 分</span></h4>" +
        options +
        '<details class="answer-details"><summary>查看答案、评分细则与依据</summary>' +
        "<p><strong>答案：</strong>" + esc(itemAnswer(item)) + "</p>" +
        "<p><strong>解析：</strong>" + esc(item.analysis || item.answer?.explanation || "—") + "</p>" +
        "<p><strong>评分：</strong>" + rules + "</p>" +
        "<p><strong>证据：</strong></p><ul class=\"evidence-list\">" + evidence + "</ul></details>" +
        "</article>";
    }).join("");
    return '<section class="paper-section"><h3>' + esc(section.label) + "（" + section.count + " 题，共 " + section.score + " 分）</h3>" + items + "</section>";
  }).join("");
  node.innerHTML = '<div class="quality-card"><strong>结构化质量检查通过</strong> · 模型 ' + esc(paper.model) +
    " · 索引 v" + paper.index_version + " · " + paper.quality_report.checks.generated_item_count +
    "/" + paper.quality_report.checks.planned_item_count + " 题 · 证据覆盖 " +
    paper.quality_report.checks.evidence_coverage + "</div>" + sections;
}

function render(data) {
  appState.data = data;
  q("#modelChip").textContent = data.model.configured ? "模型：" + data.model.name + " · 已配置" : "模型：" + data.model.name + " · 未配置密钥";
  q("#indexStatus").textContent = data.index_version ? "当前 RAG 索引：v" + data.index_version + "（" + data.knowledge_points.length + " 个知识点）" : "当前 RAG 索引：未发布";
  const frameworkConfirmed = data.framework?.confirmed;
  q("#generateButton").disabled = !data.blueprint || !data.model.configured;
  q("#organizeButton").disabled = !frameworkConfirmed;
  renderMaterials(data.materials);
  renderFramework(data);
  renderOrganization(data);
  renderBlueprint(data.blueprint);
  renderPaper(data.paper);
}

async function refresh() {
  const payload = await request("/api/state");
  render(payload.state);
}

async function upload(files, materialArea) {
  if (!files.length) return;
  const form = new FormData();
  form.append("material_area", materialArea);
  [...files].forEach((file) => form.append("files", file));
  const areaName = materialArea === "outline" ? "大纲" : "教学材料";
  notice("正在上传 " + files.length + " 份" + areaName + "…");
  const payload = await request("/api/upload", { method: "POST", body: form });
  render(payload.state);
  notice(areaName + "已进入暂存区。" + (materialArea === "outline" ? "大纲不会参与当前资料整理。" : "请主动选择资料后整理。"), "success");
}

async function organize() {
  if (!appState.data?.framework?.confirmed) throw new Error("请先整理并确认教学大纲和考核大纲形成命题框架。");
  const ids = [...document.querySelectorAll(".material-check:checked")].map((input) => input.value);
  if (!ids.length) throw new Error("请先勾选至少一份暂存资料。");
  const materialsById = new Map((appState.data?.materials || []).map((material) => [material.id, material]));
  const invalidMaterial = ids.map((id) => materialsById.get(id)).find((material) => !material || materialArea(material) !== "teaching_material");
  if (invalidMaterial) throw new Error("大纲区文件不能参与当前资料整理，请仅选择教学材料区文件。");
  q("#organizeButton").disabled = true;
  notice("正在提取文本、切分证据并整理知识点，请稍候…");
  try {
    const payload = await request("/api/organize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ material_ids: ids }),
    });
    render(payload.state);
    notice("候选知识库已生成，请检查后确认发布。", "success");
  } finally {
    q("#organizeButton").disabled = false;
  }
}

async function organizeOutlines() {
  const ids = [...document.querySelectorAll(".outline-check:checked")].map((input) => input.value);
  if (!ids.length) throw new Error("请先勾选教学大纲和考核大纲。");
  q("#organizeOutlineButton").disabled = true;
  notice("正在提取教学范围与考核要求，形成待确认命题框架…");
  try {
    const payload = await request("/api/organize-outline", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ material_ids: ids }),
    });
    render(payload.state);
    notice("已生成大纲命题框架候选，请检查后确认。", "success");
  } finally {
    q("#organizeOutlineButton").disabled = false;
  }
}

async function confirmFramework() {
  const payload = await request("/api/confirm-framework", { method: "POST" });
  render(payload.state);
  notice("命题框架已确认，现在可以整理教学材料。", "success");
}

async function publishCandidate() {
  const payload = await request("/api/publish", { method: "POST" });
  render(payload.state);
  notice("候选知识库已发布到当前 RAG 索引。", "success");
}

function blueprintPayload(form) {
  const values = Object.fromEntries(new FormData(form).entries());
  values.sections = [...document.querySelectorAll(".section-row")].map((row) => ({
    question_type: row.querySelector(".section-type").value,
    count: row.querySelector(".section-count").value,
    score_per_item: row.querySelector(".section-score").value,
  }));
  return values;
}

async function createBlueprint(event) {
  event.preventDefault();
  const payload = await request("/api/blueprint", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(blueprintPayload(event.currentTarget)),
  });
  render(payload.state);
  notice("蓝图已构建。请核对题位与知识点覆盖后生成候选卷。", "success");
  q("#paperPanel").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function createPaper() {
  q("#generateButton").disabled = true;
  notice("正在按题型分批调用 DeepSeek，并进行结构化校验。生成可能需要几分钟…");
  try {
    const payload = await request("/api/generate", { method: "POST" });
    render(payload.state);
    notice("候选试卷已生成。展开每题可审查答案、评分细则和证据。", "success");
  } finally {
    q("#generateButton").disabled = false;
  }
}

function initializeUi() {
  q("#sectionRows").innerHTML = [
    questionRow("single_choice", 15, 2),
    questionRow("true_false", 10, 2),
    questionRow("fill_blank", 10, 2),
    questionRow("short_answer", 4, 5),
    questionRow("comprehensive", 1, 10),
  ].join("");
  updateTotal();
  q("#outlineFileInput").addEventListener("change", (event) => {
    const input = event.currentTarget;
    upload(input.files, "outline").catch((error) => notice(error.message, "error")).finally(() => { input.value = ""; });
  });
  q("#teachingMaterialFileInput").addEventListener("change", (event) => {
    const input = event.currentTarget;
    upload(input.files, "teaching_material").catch((error) => notice(error.message, "error")).finally(() => { input.value = ""; });
  });
  q("#selectAllOutlines").addEventListener("click", () => {
    const inputs = [...document.querySelectorAll(".outline-check:not(:disabled)")];
    inputs.forEach((input) => { input.checked = true; });
    if (!inputs.length) notice("当前没有可整理的大纲文件。", "info");
  });
  q("#clearOutlineSelection").addEventListener("click", () => {
    document.querySelectorAll(".outline-check").forEach((input) => { input.checked = false; });
  });
  q("#selectAllTeachingMaterials").addEventListener("click", () => {
    const inputs = [...document.querySelectorAll(".material-check:not(:disabled)")];
    inputs.forEach((input) => { input.checked = true; });
    if (!inputs.length) notice("当前没有可整理的教学材料。", "info");
  });
  q("#clearTeachingMaterialSelection").addEventListener("click", () => {
    document.querySelectorAll(".material-check").forEach((input) => { input.checked = false; });
  });
  q("#organizeOutlineButton").addEventListener("click", () => organizeOutlines().catch((error) => notice(error.message, "error")));
  q("#organizeButton").addEventListener("click", () => organize().catch((error) => notice(error.message, "error")));
  q("#blueprintForm").addEventListener("submit", (event) => createBlueprint(event).catch((error) => notice(error.message, "error")));
  q("#generateButton").addEventListener("click", () => createPaper().catch((error) => notice(error.message, "error")));
  q("#addSectionButton").addEventListener("click", () => { q("#sectionRows").insertAdjacentHTML("beforeend", questionRow("short_answer", 2, 5)); updateTotal(); });
  q("#sectionRows").addEventListener("input", updateTotal);
  q("#sectionRows").addEventListener("change", updateTotal);
  q("#sectionRows").addEventListener("click", (event) => {
    const button = event.target.closest(".remove-row");
    if (button) { button.closest(".section-row").remove(); updateTotal(); }
  });
  q("#resetButton").addEventListener("click", async () => {
    if (!window.confirm("确定重置吗？这会清空本次内存状态和本地暂存上传文件。")) return;
    try {
      const payload = await request("/api/reset", { method: "POST" });
      render(payload.state);
      notice("原型状态和本地暂存上传文件已重置。", "success");
    } catch (error) {
      notice(error.message, "error");
    }
  });
  document.querySelectorAll(".step").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".step").forEach((item) => item.classList.remove("is-active"));
      button.classList.add("is-active");
      q("#" + button.dataset.target).scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
}

initializeUi();
refresh().catch((error) => notice("无法连接原型服务：" + error.message, "error"));
