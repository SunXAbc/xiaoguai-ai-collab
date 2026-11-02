"use strict";

// 页面只与自己的桌面宿主交换 JSON；不持有管理密钥，也不直接请求本机 HTTP 服务。
const bridge = window.chrome?.webview;
// 视图状态只保留已渲染的数据和 UI 状态，所有表单值都从真实 DOM 控件读回。
const ui = {
  snapshot: {},
  ready: false,
  busy: false,
  running: false,
  main: "collaboration",
  section: "execution",
  selectedProject: "",
  notice: "",
  messageSignature: "",
  agentSignature: "",
  namesSignature: "",
  windowVisible: true,
  confirmAction: null,
  pendingAction: "", // 区分已提交业务，避免打开同一项目后弹层不关闭。
  lastSnapshotAt: 0, // 最近一次实际成功快照，不能拿旧绿色状态冒充当前连接。
};
// MCP 沿用此前的稳定字段名称，不依赖视觉位置或 CSS 选择器生成随机标识。
const fieldNames = [
  "project_path",
  "agent_name",
  "agent_role",
  "thread",
  "recipient",
  "subject",
  "body",
  "allow_write",
  "ai_count",
  "rounds",
  "concurrency",
];
// 业务入口的 DOM 标识与已有界面管理 MCP 保持一致。
const actionNames = [
  "open_project",
  "register_agent",
  "send_message",
  "export_agent_config",
  "export_ui_config",
  "export_history",
  "start_collaboration",
  "prepare_team",
  "stop_collaboration",
  "open_data_directory",
];
// 字段约束与既有管理 MCP 的 Schema 一致。
const fieldLimits = {
  project_path: 32000,
  agent_name: 48,
  agent_role: 80,
  thread: 100,
  recipient: 100,
  subject: 200,
  body: 8000,
};
// 数值字段必须在三层保持整数：真实 DOM、管理 MCP 和后端执行器。
const numericFields = {
  ai_count: [2, 50],
  rounds: [1, 3],
  concurrency: [1, 4],
};

/** 取得 id 对应的本页面控件；不存在时立即报错，不能把遗漏控件当成功。 */
function element(id) {
  const node = document.getElementById(id);
  if (!node) throw new Error("页面缺少控件：" + id);
  return node;
}

/** 向本程序原生宿主提交 value，不接受任意网络地址或执行代码字符串。 */
function post(value) {
  if (!bridge) throw new Error("桌面宿主未连接");
  bridge.postMessage(value);
}

/** 创建带固定 className 与纯文本 text 的元素；用户消息绝不作为 HTML 执行。 */
function textNode(tag, className, text = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = String(text);
  return node;
}

/** 读取 value 秒时间戳，固定按北京时间显示；零值不冒充在线。 */
function timeText(value) {
  return value > 0
    ? new Date(value * 1000).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "尚无调用记录";
}

/** 从 path 提取项目名，路径本身仍只作为文本而非 URL 或命令使用。 */
function projectName(path) {
  return (
    String(path || "")
      .replace(/[\\/]+$/, "")
      .split(/[\\/]/)
      .pop() || "我的项目"
  );
}

/** 为 name 返回固定的角色样式，不将任意输入拼入 CSS。 */
function avatarClass(name) {
  return name === "Engineer"
    ? "avatar"
    : name === "Reviewer"
      ? "avatar reviewer"
      : name === "Operator"
        ? "avatar operator"
        : "avatar other";
}

/** 显示 message 状态；error 只改变视觉，不把输入校验错误误判为整个服务离线。 */
function showNotice(message, error = false) {
  ui.notice = String(message || "");
  element("notice-text").textContent = ui.notice;
  element("notice-icon").textContent = error ? "!" : "✓";
  element("notice").classList.toggle("error", error);
  element("notice").hidden = !ui.notice;
}

/** 等待一次真正的浏览器布局刷新，用于界面管理 MCP 的实际控件回执。 */
function afterPaint() {
  return new Promise((resolve) => {
    // 隐藏测试窗口不一定产生动画帧；读取几何信息会强制实际 DOM 布局。
    const timeout = setTimeout(
      resolve,
      ui.windowVisible && !document.hidden ? 100 : 20,
    );
    if (ui.windowVisible && !document.hidden) {
      requestAnimationFrame(() =>
        requestAnimationFrame(() => {
          clearTimeout(timeout);
          resolve();
        }),
      );
    }
  });
}

/** 读取所有真实表单控件的当前值，不以旧快照代替用户尚未提交的输入。 */
function readFields() {
  const result = {};
  for (const name of fieldNames)
    result[name] =
      name === "allow_write"
        ? element(name).checked
        : name in numericFields
          ? Number(element(name).value)
          : element(name).value;
  return result;
}

/** 原子预校验 fields 后更新控件；开启写权限必须 confirmWrite，运行中不能切换项目或权限。 */
function setFields(fields, confirmWrite = false) {
  if (!fields || typeof fields !== "object" || Array.isArray(fields))
    throw new Error("字段必须是对象");
  for (const [name, value] of Object.entries(fields)) {
    if (!fieldNames.includes(name)) throw new Error("未知界面字段：" + name);
    if (name in numericFields) {
      const [minimum, maximum] = numericFields[name]; // 当前配置字段的明确上下限。
      if (!Number.isInteger(value) || value < minimum || value > maximum)
        throw new Error(
          name + " 必须为 " + minimum + "～" + maximum + " 的整数",
        );
      if (ui.running) throw new Error("协作运行中不能改变团队配置");
    } else if (name === "allow_write") {
      if (typeof value !== "boolean") throw new Error("写权限必须是布尔值");
      if (value && !confirmWrite)
        throw new Error("开启工程师写权限需要明确确认");
      if (ui.running) throw new Error("协作运行中不能改变写权限");
    } else {
      if (
        typeof value !== "string" ||
        value.length > fieldLimits[name] ||
        value.includes("\0")
      )
        throw new Error("字段长度或类型错误：" + name);
      if (
        ui.running &&
        ["project_path", "agent_name", "agent_role"].includes(name)
      )
        throw new Error("协作运行中不能修改该字段：" + name);
      if (
        name === "recipient" &&
        !Array.from(element(name).options).some(
          (option) => option.value === value,
        )
      )
        throw new Error("接收者不在现有下拉选项中");
    }
  }
  for (const [name, value] of Object.entries(fields)) {
    if (name === "allow_write") element(name).checked = value;
    else element(name).value = value;
  }
  updateButtons();
}

/** 读回人数、轮次和并发，明确显示最坏调用数；返回配置是否合法，不启动或重发任务。 */
function updateTeamPlan() {
  const fields = readFields(); // 只使用实际控件当前值。
  const valid = Object.entries(numericFields).every(
    ([name, [minimum, maximum]]) =>
      Number.isInteger(fields[name]) &&
      fields[name] >= minimum &&
      fields[name] <= maximum,
  ); // 拒绝手工输入的空值、小数和越界。
  element("team-plan").textContent = valid
    ? fields.ai_count +
      " 人 × " +
      fields.rounds +
      " 轮，最多 " +
      fields.ai_count * fields.rounds +
      " 次模型调用。" +
      (fields.allow_write
        ? "写入模式：全员串行，仅 Engineer 可写。"
        : "只读并发上限 " +
          Math.min(fields.concurrency, fields.ai_count - 1) +
          "；每轮最后独立审查。")
    : "请输入 2～50 人、1～3 轮和 1～4 并发。";
  return valid;
}

/** 将 main/section 设置到实际网页面板；不重建或清空表单。 */
function selectTab(main, section = "messages") {
  if (
    !["collaboration", "agents", "connection"].includes(main) ||
    !["messages", "execution"].includes(section)
  )
    throw new Error("未知页面");
  ui.main = main;
  ui.section = section;
  for (const page of document.querySelectorAll("[data-page]"))
    page.hidden = page.dataset.page !== main;
  for (const button of document.querySelectorAll("[data-main]"))
    button.classList.toggle("active", button.dataset.main === main);
  for (const button of document.querySelectorAll("[data-section]"))
    button.classList.toggle("active", button.dataset.section === section);
  element("task-panel").hidden = section !== "execution";
  element("workspace-grid").classList.toggle(
    "messages-only",
    section !== "execution",
  );
}

/** 选择当前项目真实存在的 name，并更新所有可见成员卡片的选中状态。 */
function selectAgent(name) {
  if (!(ui.snapshot.agents || []).some((agent) => agent.name === name))
    throw new Error("参与者目录中没有该身份");
  element("selected-agent").value = name;
  for (const card of document.querySelectorAll("[data-agent]"))
    card.classList.toggle("selected", card.dataset.agent === name);
}

/** 根据实际状态设置业务按钮；停止入口不受普通提交的 busy 阻挡。 */
function updateButtons() {
  const available = ui.ready && !ui.busy && !!ui.snapshot.selected_project;
  const validTeam = updateTeamPlan(); // 团队配置只是草稿，准备成员不会调用模型。
  for (const name of actionNames) element(name).disabled = !available;
  element("open_project").disabled = !ui.ready || ui.busy || ui.running;
  element("register_agent").disabled = !available || ui.running;
  element("start_collaboration").disabled =
    !available || ui.running || !validTeam;
  element("prepare_team").disabled = !available || ui.running || !validTeam;
  for (const name of Object.keys(numericFields))
    element(name).disabled = ui.running || ui.busy;
  element("stop_collaboration").disabled = !ui.ready || !ui.running;
  element("export_ui_config").disabled = !ui.ready || ui.busy;
  element("open_data_directory").disabled = !ui.ready;
  element("browse_project").disabled = ui.running || ui.busy;
  element("allow_write").disabled = ui.running;
  element("connection-dot").classList.toggle("disconnected", !ui.ready);
  element("footer-dot").classList.toggle("disconnected", !ui.ready);
  document
    .querySelector(".connection-pill .status-dot")
    .classList.toggle("disconnected", !ui.ready);
  element("connection-text").textContent = ui.ready
    ? "本机服务已连接"
    : ui.lastSnapshotAt
      ? "连接状态暂未更新"
      : "正在连接本机服务";
}

/** now 为当前毫秒时间；成功快照超过六秒未更新时降级提示，不重试任何业务动作。 */
function refreshConnectionStatus(now = Date.now()) {
  if (!ui.ready || !ui.lastSnapshotAt || now - ui.lastSnapshotAt < 6000)
    return false;
  ui.ready = false;
  element("status").textContent =
    "连接状态暂未更新，等待本机服务恢复；不会自动重发操作";
  updateButtons();
  return true;
}

/** 只将受信任的固定按钮名 action 交给原生业务控制器；options 仅允许已验证的导出/确认参数。 */
function dispatchAction(action, id, options = {}) {
  if (!actionNames.includes(action)) throw new Error("未知业务动作");
  if (action === "stop_collaboration" && !ui.running)
    return { already_stopped: true, state: getState() };
  if (element(action).disabled) throw new Error("该业务按钮当前不可用");
  if (action === "start_collaboration" && options.confirm_start !== true)
    throw new Error("启动模型需要 confirm_start=True，并先取得用户授权");
  if (action === "export_history") {
    if (
      !/^[A-Za-z0-9_.-]{1,120}\.jsonl$/.test(options.export_filename || "") ||
      options.export_filename.includes("..")
    )
      throw new Error("请提供不含路径的 .jsonl 新文件名");
  }
  if (action !== "open_data_directory") ui.busy = true;
  ui.pendingAction = action;
  updateButtons();
  post({
    type: "action",
    action,
    id: options.from_mcp ? id : "",
    fields: readFields(),
    selected_agent: element("selected-agent").value,
    ...options,
  });
  return null;
}

/** 安全显示 body 文本；代码围栏也使用 textContent，不解释任何 HTML。 */
function renderBody(container, body) {
  const parts = String(body || "").split("```"); // 只识别代码围栏，始终按文本渲染。
  for (let index = 0; index < parts.length; index++) {
    // 偶数段是正文，奇数段是代码；index 保留两者在原消息中的顺序。
    if (index % 2 === 0) {
      container.append(textNode("span", "", parts[index]));
      continue;
    }
    const content = parts[index]; // 保留原始代码中的换行与字符，不作为 HTML 解析。
    const newline = content.indexOf("\n"); // 可选的首行语言标识。
    const firstLine = newline >= 0 ? content.slice(0, newline).trim() : ""; // 代码围栏后的首行候选语言。
    // language 只决定文本标签和移除首行，不用于加载语法脚本或执行代码。
    const language = /^[A-Za-z][A-Za-z0-9_+#.-]{0,23}$/.test(firstLine)
      ? firstLine
      : "";
    const block = textNode("div", "code-block"); // 固定样式容器，不创建可执行元素。
    const pre = textNode("pre", ""); // 可滚动代码区，长行不撑破消息布局。
    pre.append(
      textNode("code", "", language ? content.slice(newline + 1) : content),
    );
    block.append(textNode("div", "code-language", language || "CODE"), pre);
    container.append(block);
  }
}

/** 将 messages 渲染为聊天流；只有原本在底部时才跟随新消息。 */
function renderMessages(messages) {
  const signature = JSON.stringify(messages);
  if (signature === ui.messageSignature) return;
  ui.messageSignature = signature;
  const stream = element("transcript");
  const atBottom =
    stream.scrollHeight - stream.scrollTop - stream.clientHeight < 45;
  stream.replaceChildren();
  element("message-count").textContent = String(messages.length);
  if (!messages.length) {
    const empty = textNode("div", "empty-state");
    empty.append(
      textNode("div", "empty-illustration", "✳"),
      textNode("h3", "", "从一个清晰的目标开始"),
      textNode(
        "p",
        "",
        "让 Engineer 负责实现，Reviewer 独立审查。\n每一条协作结论，都能在这里追溯。",
      ),
    );
    stream.append(empty);
    return;
  }
  for (const message of messages) {
    const article = textNode("article", "message");
    article.dataset.messageId = String(message.id);
    const avatar = textNode(
      "span",
      avatarClass(message.sender),
      message.sender.slice(0, 1),
    );
    const content = textNode("div", "message-content");
    const heading = textNode("div", "message-heading");
    heading.append(
      textNode("strong", "", message.sender),
      textNode("span", "type-tag", String(message.kind).toUpperCase()),
      textNode("time", "message-time", timeText(message.created)),
    );
    const body = textNode("div", "message-body");
    renderBody(body, message.body);
    const footer = textNode("div", "message-footer");
    footer.append(
      textNode("span", "thread-tag", message.thread),
      textNode(
        "span",
        "",
        "→ " +
          (message.deliveries || [])
            .map(
              (delivery) =>
                delivery.recipient + (delivery.acknowledged ? " · 已确认" : ""),
            )
            .join("，"),
      ),
    );
    content.append(
      heading,
      textNode("div", "message-subject", message.subject),
      body,
      footer,
    );
    article.append(avatar, content);
    stream.append(article);
  }
  if (atBottom) stream.scrollTop = stream.scrollHeight;
}

/** 渲染真实成员目录与身份选项，最近活动不等于在线或正在推理。 */
function renderAgents(agents) {
  const active = new Set(
    (ui.snapshot.run?.active || []).map((item) => item.agent),
  ); // 执行器的真实活动成员，不是最近活动时间。
  const submitted = new Set(
    (ui.snapshot.run?.stages || []).map((item) => item.agent),
  ); // 当前任务已提交结果的成员。
  const signature = JSON.stringify([agents, [...active], [...submitted]]);
  if (signature === ui.agentSignature) return;
  ui.agentSignature = signature;
  const names = agents.map((agent) => agent.name);
  const selected = element("selected-agent").value || "Engineer";
  if (JSON.stringify(names) !== ui.namesSignature) {
    const recipient = element("recipient").value;
    element("recipient").replaceChildren();
    for (const name of [
      "Engineer + Reviewer",
      ...names.filter((name) => name !== "Operator"),
    ]) {
      const option = textNode(
        "option",
        "",
        name === "Engineer + Reviewer" ? "工程师 + 审查员" : name,
      );
      option.value = name;
      element("recipient").append(option);
    }
    element("recipient").value = Array.from(element("recipient").options).some(
      (option) => option.value === recipient,
    )
      ? recipient
      : "Engineer + Reviewer";
    element("selected-agent").replaceChildren();
    for (const name of names) {
      const option = textNode("option", "", name);
      option.value = name;
      element("selected-agent").append(option);
    }
    element("selected-agent").value = names.includes(selected)
      ? selected
      : names.includes("Engineer")
        ? "Engineer"
        : names[0] || "";
    ui.namesSignature = JSON.stringify(names);
  }
  element("team-strip").replaceChildren();
  element("agent-directory").replaceChildren();
  for (const agent of agents) {
    const card = textNode(
      "button",
      "team-card" + (agent.seen > 0 ? " has-activity" : ""),
    );
    card.dataset.agent = agent.name;
    card.title =
      agent.role + "；" + timeText(agent.seen) + "（不代表当前在线）";
    const meta = textNode("span", "team-meta");
    meta.append(
      textNode("strong", "", agent.name),
      textNode(
        "small",
        "",
        agent.role +
          (active.has(agent.name)
            ? " · 执行中"
            : submitted.has(agent.name)
              ? " · 已提交"
              : ""),
      ),
    );
    card.append(
      textNode("span", avatarClass(agent.name), agent.name.slice(0, 1)),
      meta,
      textNode("span", "member-indicator"),
    );
    card.addEventListener("click", () => selectAgent(agent.name));
    element("team-strip").append(card);
    const row = textNode("div", "agent-row");
    row.dataset.agent = agent.name;
    const description = textNode("div", "");
    description.append(
      textNode("strong", "", agent.name),
      textNode("small", "", agent.role),
    );
    row.append(
      textNode("span", avatarClass(agent.name), agent.name.slice(0, 1)),
      description,
      textNode(
        "div",
        "agent-seen",
        timeText(agent.seen) +
          "\n" +
          (agent.name === "Operator" ? "人工 / 管理入口" : "项目协作身份"),
      ),
    );
    row.addEventListener("click", () => selectAgent(agent.name));
    element("agent-directory").append(row);
  }
  element("nav-agent-count").textContent = String(agents.length);
  element("agent-total").textContent = agents.length + " 位参与者";
  if (element("selected-agent").value)
    selectAgent(element("selected-agent").value);
}

/** 显示不含凭据的连接详情和实际运行阶段。 */
function renderDetails(snapshot) {
  const run = snapshot.run || {};
  element("run-badge").textContent =
    run.status === "running"
      ? "进行中"
      : run.status === "finished"
        ? "已结束"
        : run.status === "failed"
          ? "异常"
          : run.status === "stopped"
            ? "已停止"
            : "未运行";
  element("run-stage").textContent =
    run.stage || "准备好目标后，开始第一轮协作。";
  element("run-steps").replaceChildren();
  const total =
    run.planned_calls ||
    (run.id ? 4 : element("ai_count").value * element("rounds").value); // 旧任务固定四次，不用当前草稿改写旧进度。
  const progress = document.createElement("progress"); // 人数很多时仍清楚展示实际完成数。
  progress.className = "run-progress";
  progress.max = total;
  progress.value = (run.stages || []).length;
  progress.setAttribute("aria-label", "已完成的成员调用");
  element("run-steps").append(
    progress,
    textNode("span", "run-progress-label", progress.value + " / " + total),
  );
  const values = [
    ["本机 MCP 服务", "http://127.0.0.1:" + snapshot.port + "/mcp"],
    ["项目 ID", snapshot.selected_project || "尚未选择"],
    ["后端服务版本", snapshot.version || "连接中"],
    ["当前运行阶段", run.stage || "未启动模型"],
    ["配置目录", snapshot.connection_directory || "data/connections"],
    ["授权范围", "消息身份按项目隔离 · 界面管理单独授权"],
  ];
  element("details").replaceChildren();
  for (const [label, value] of values) {
    const pair = textNode("div", "details-pair");
    pair.append(textNode("dt", "", label), textNode("dd", "", value));
    element("details").append(pair);
  }
}

/** 应用原生宿主的真实快照，再读取 DOM 生成对应 MCP 业务操作的完成回执。 */
async function applySnapshot(snapshot) {
  if (snapshot.error) {
    if (snapshot.done) ui.busy = false;
    if (snapshot.fatal) ui.ready = false;
    showNotice(snapshot.error, true);
    if (snapshot.done) ui.pendingAction = "";
    updateButtons();
  } else {
    const changed = ui.selectedProject !== snapshot.selected_project;
    ui.snapshot = snapshot;
    ui.selectedProject = snapshot.selected_project || "";
    ui.ready = true;
    ui.lastSnapshotAt = Date.now();
    ui.running = snapshot.run?.status === "running";
    if (snapshot.done) ui.busy = false;
    const project = (snapshot.projects || []).find(
      (item) => item.id === ui.selectedProject,
    );
    if (project) {
      if (changed || !element("project_path").value)
        element("project_path").value = project.root;
      element("sidebar-project-name").textContent = projectName(project.root);
      element("sidebar-project-root").textContent = project.root;
      element("breadcrumb-project").textContent = projectName(project.root);
    }
    renderAgents(snapshot.agents || []);
    renderMessages(snapshot.messages || []);
    renderDetails(snapshot);
    element("status").textContent =
      "MCP 已连接 · 127.0.0.1:" +
      snapshot.port +
      " · " +
      (snapshot.run?.stage || "未启动模型");
    element("lease-text").textContent = snapshot.reservations?.length
      ? snapshot.reservations
          .map((item) => item.path + " → " + item.owner)
          .join("；")
      : "暂无文件租约 · 修改前申请，完成后释放";
    if (snapshot.notice) showNotice(snapshot.notice);
    updateButtons();
    if (changed || (snapshot.done && ui.pendingAction === "open_project"))
      element("project-modal").hidden = true;
    if (snapshot.done) ui.pendingAction = "";
  }
  if (snapshot.ui_id) {
    await afterPaint();
    post({
      type: "ui_result",
      id: snapshot.ui_id,
      result: { state: getState(), notice: snapshot.notice || "" },
      error: snapshot.error || "",
    });
  }
}

/** 生成真实网页控件的状态；消息、控件可见性与字段来自本次已渲染页面。 */
function getState(messageLimit = 3) {
  const controls = {};
  for (const name of [
    ...fieldNames,
    ...actionNames,
    "browse_project",
    "transcript",
    "details",
  ]) {
    const node = element(name);
    controls[name] = {
      id: name,
      enabled: !node.disabled,
      visible_on_active_tab:
        node.getClientRects().length > 0 &&
        getComputedStyle(node).visibility !== "hidden",
    };
  }
  const count = Math.max(0, Math.min(10, Number(messageLimit) || 0));
  return {
    app: "XiaoguaiCollab",
    ui_protocol: 1,
    renderer: "webview2",
    theme: "light",
    window_visible: ui.windowVisible,
    ready: ui.ready,
    busy: ui.busy,
    project_id: ui.selectedProject,
    selected_agent: element("selected-agent").value,
    main_tab: ui.main,
    section: ui.section,
    fields: readFields(),
    recipient_options: Array.from(element("recipient").options).map(
      (option) => option.value,
    ),
    controls,
    participants: ui.snapshot.agents || [],
    messages: count ? (ui.snapshot.messages || []).slice(-count) : [],
    run: ui.snapshot.run || {},
    status: element("status").textContent,
    notice: ui.notice,
    layout: layoutMetrics(),
    security: {
      untrusted_message_markup: document.querySelectorAll(
        ".message-body img,.message-body script,.message-body iframe",
      ).length,
      code_block_count: document.querySelectorAll(".message-body pre code")
        .length,
      local_scripts_only: Array.from(document.scripts).every(
        (script) =>
          !script.src || new URL(script.src).origin === location.origin,
      ),
    },
  };
}

/** 检查实际 DOM 布局的基本可读性和横向边界，不以手写参数代替浏览器结果。 */
function layoutMetrics() {
  const active = document.querySelector("[data-page]:not([hidden])");
  const style = getComputedStyle(element("body"));
  return {
    viewport_width: innerWidth,
    viewport_height: innerHeight,
    host_size: ui.hostSize || null,
    device_pixel_ratio: devicePixelRatio,
    horizontal_overflow: document.documentElement.scrollWidth > innerWidth + 1,
    active_page_overflow: active
      ? active.scrollWidth > active.clientWidth + 1
      : false,
    message_font_px: parseFloat(
      getComputedStyle(
        document.querySelector(".message-body") || element("transcript"),
      ).fontSize,
    ),
    input_font_px: parseFloat(style.fontSize),
    readable_controls: Array.from(
      document.querySelectorAll("button,input,select,textarea"),
    )
      .filter((node) => node.getClientRects().length && !node.disabled)
      .every(
        (node) =>
          node.getBoundingClientRect().width >= 20 &&
          node.getBoundingClientRect().height >= 15,
      ),
  };
}

/** 执行管理 MCP 的白名单命令，所有结果从真实 DOM 读取后交回原生宿主。 */
async function handleCommand(command) {
  try {
    const args = command.arguments || {};
    if (
      command.operation !== "state" &&
      ui.busy &&
      !(command.operation === "invoke" && args.action === "stop_collaboration")
    )
      throw new Error("界面正在处理其他业务操作，请等待原操作结束");
    if (command.operation === "state") {
      await afterPaint();
      post({
        type: "ui_result",
        id: command.id,
        result: getState(args.message_limit),
        error: "",
      });
      return;
    }
    if (command.operation === "set_fields")
      setFields(args.fields, args.confirm_write === true);
    else if (command.operation === "select_tab")
      selectTab(args.main, args.section);
    else if (command.operation === "select_agent") selectAgent(args.name);
    else if (command.operation === "invoke") {
      const immediate = dispatchAction(args.action, command.id, {
        ...args,
        from_mcp: true,
      });
      if (immediate)
        post({
          type: "ui_result",
          id: command.id,
          result: immediate,
          error: "",
        });
      return;
    } else throw new Error("未知界面命令");
    await afterPaint();
    post({
      type: "ui_result",
      id: command.id,
      result: { state: getState() },
      error: "",
    });
  } catch (error) {
    post({
      type: "ui_result",
      id: command.id,
      result: {},
      error: String(error.message || error),
    });
  }
}

/** color 为浏览器解析后的不透明 RGB 色值；返回用于文字对比度计算的相对亮度。 */
function colorLuminance(color) {
  const channels = color
    .match(/[\d.]+/g)
    ?.slice(0, 3)
    .map(Number); // 浏览器实际计算的红绿蓝分量。
  if (!color.startsWith("rgb(") || channels?.length !== 3)
    throw new Error("文字对比度检查需要不透明 RGB 色值：" + color);
  const linear = channels.map((channel) => {
    const value = channel / 255; // 将当前颜色分量归一化。
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  }); // 将三个 sRGB 分量转为线性亮度。
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
}

/** 从真实 CSS 计算关键正文、辅助说明和按钮文字的对比度；不把装饰图形或禁用态冒充文字。 */
function textContrastMetrics() {
  const samples = [
    ["导航", ".nav-item:not(.active)", ".sidebar"],
    ["选中导航", ".nav-item.active", ".nav-item.active"],
    ["侧栏标题", ".sidebar-label", ".sidebar"],
    ["项目路径", ".project-card p", ".sidebar"],
    ["成员角色", ".team-card small", ".team-card"],
    ["消息正文", ".message-body", ".conversation-panel"],
    ["消息时间", ".message-time", ".conversation-panel"],
    ["对话提示", ".conversation-hint", ".conversation-hint"],
    ["表单标签", ".field-label", ".task-panel"],
    ["面板说明", ".panel-subtitle", ".task-panel"],
    ["权限说明", ".switch-line small", ".permission-box"],
    ["主按钮", ".button.primary", ".button.primary"],
    ["次按钮", ".button.secondary", ".button.secondary"],
    ["状态栏", ".app-status", "body"],
  ]; // 每项显式指定真实承载背景，避免把透明层误算为黑色。
  const checks = []; // 只记录实际存在的 DOM 样本和计算结果。
  for (const [name, selector, backgroundSelector] of samples) {
    const foreground = document.querySelector(selector); // 当前文字控件。
    const background = document.querySelector(backgroundSelector); // 文字下面的不透明背景控件。
    if (!foreground || !background) continue;
    const light = colorLuminance(getComputedStyle(foreground).color); // 文字实际亮度。
    const dark = colorLuminance(getComputedStyle(background).backgroundColor); // 背景实际亮度。
    const ratio =
      (Math.max(light, dark) + 0.05) / (Math.min(light, dark) + 0.05); // 明暗无关的实际对比比值。
    checks.push({ name, ratio: Number(ratio.toFixed(2)) });
  }
  return {
    minimum_ratio: Math.min(...checks.map((check) => check.ratio)),
    checks,
  };
}

/** 仅布局测试模式使用的夹具检查，不请求后端、不启动模型、不保存项目数据。 */
async function runLayoutCheck() {
  const before = readFields();
  const selected = element("selected-agent").value;
  setFields({
    body: "网页布局测试的中文目标",
    subject: "保留任务参数",
    thread: "layout-test",
  });
  for (const [main, section] of [
    ["collaboration", "messages"],
    ["collaboration", "execution"],
    ["agents", "messages"],
    ["connection", "messages"],
  ]) {
    selectTab(main, section);
    await afterPaint();
    const metrics = layoutMetrics();
    if (
      metrics.horizontal_overflow ||
      metrics.active_page_overflow ||
      !metrics.readable_controls
    )
      throw new Error("网页布局存在溢出或不可用控件：" + main + "/" + section);
  }
  if (
    element("body").value !== "网页布局测试的中文目标" ||
    element("subject").value !== "保留任务参数" ||
    element("thread").value !== "layout-test"
  )
    throw new Error("切换页面改变了草稿");
  // 这些是明确标注的前端夹具检查，不发起网络请求，也不伪造后端业务成功。
  const fixture = { ...ui.snapshot, done: true, ui_id: "" }; // 当前布局数据，不包含真实操作票据。
  element("project-modal").hidden = false;
  ui.pendingAction = "open_project";
  await applySnapshot(fixture);
  if (!element("project-modal").hidden)
    throw new Error("打开同一项目后弹层未关闭");
  if (!refreshConnectionStatus(ui.lastSnapshotAt + 7000) || ui.ready)
    throw new Error("连接状态未按实际快照时效降级");
  await applySnapshot({ ...fixture, done: false });
  if (!ui.ready || element("body").value !== "网页布局测试的中文目标")
    throw new Error("连接恢复没有保留草稿");
  setFields(before, true);
  if (selected) selectAgent(selected);
  selectTab("collaboration", "execution");
  await afterPaint();
  // 本程序按钮有 160ms 的颜色过渡；静态对比度检查等待过渡结束，不重试任何业务动作。
  await new Promise((resolve) => setTimeout(resolve, 200));
  const contrast = textContrastMetrics(); // 在实际渲染后的样式中验证本轮高对比度目标。
  if (contrast.checks.length < 12 || contrast.minimum_ratio < 4.5)
    throw new Error("关键文字对比度不足：" + JSON.stringify(contrast));
  return {
    passed: true,
    renderer: "webview2",
    theme: "light",
    fixture_only: true,
    model_started: false,
    page_visibility_checks: 4,
    input_and_selection_preserved: true,
    same_project_dialog_closes: true,
    stale_connection_and_recovery_checked: true,
    text_contrast: contrast,
    layout: layoutMetrics(),
  };
}

// 绑定人工界面事件。人工与 MCP 走同一条原生业务通道，只有人工使用选择目录/保存文件对话框。
for (const button of document.querySelectorAll("[data-main]"))
  button.addEventListener("click", () =>
    selectTab(button.dataset.main, ui.section),
  );
for (const button of document.querySelectorAll("[data-section]"))
  button.addEventListener("click", () =>
    selectTab("collaboration", button.dataset.section),
  );
for (const button of document.querySelectorAll("[data-close]"))
  button.addEventListener("click", () => {
    element(button.dataset.close).hidden = true;
  });
element("project-switch").addEventListener("click", () => {
  element("project-modal").hidden = false;
  element("project_path").focus();
});
element("browse_project").addEventListener("click", () =>
  post({ type: "browse" }),
);
element("notice-close").addEventListener("click", () => showNotice(""));
// 人工配置变化只刷新计划和按钮，不调用模型；MCP 走同一批 DOM 控件。
for (const name of [...Object.keys(numericFields), "allow_write"])
  element(name).addEventListener("input", updateButtons);
element("selected-agent").addEventListener("change", () =>
  selectAgent(element("selected-agent").value),
);
for (const action of actionNames)
  element(action).addEventListener("click", () => {
    try {
      if (action === "start_collaboration") {
        element("confirm-title").textContent = "启动真实 AI 协作？";
        element("confirm-text").textContent =
          element("team-plan").textContent +
          " 将使用本机已登录 CLI 的模型额度。" +
          (element("allow_write").checked
            ? "仅 Engineer 可以修改当前项目，全员串行，其他成员与审查员只读。"
            : "当前是只读分析模式，不允许修改源码。") +
          "任务和代码会发送给 CLI 使用的模型服务。";
        ui.confirmAction = () =>
          dispatchAction(action, "human-" + crypto.randomUUID(), {
            confirm_start: true,
          });
        element("confirm-modal").hidden = false;
      } else if (action === "export_history") post({ type: "human_export" });
      else dispatchAction(action, "human-" + crypto.randomUUID());
    } catch (error) {
      showNotice(error.message, true);
    }
  });
element("confirm-cancel").addEventListener("click", () => {
  ui.confirmAction = null;
  element("confirm-modal").hidden = true;
});
element("confirm-accept").addEventListener("click", () => {
  const action = ui.confirmAction;
  ui.confirmAction = null;
  element("confirm-modal").hidden = true;
  try {
    if (action) action();
  } catch (error) {
    showNotice(error.message, true);
  }
});

// 接收原生快照、真实管理命令及纯布局测试指令；不执行从消息正文来的脚本。
if (bridge)
  bridge.addEventListener("message", async (event) => {
    const message = event.data;
    if (message.type === "snapshot") await applySnapshot(message.value);
    else if (message.type === "ui_command") await handleCommand(message.value);
    else if (message.type === "window_state")
      ui.windowVisible = !!message.visible;
    else if (message.type === "host_size")
      ui.hostSize = {
        width: message.width,
        height: message.height,
        dpi: message.dpi,
      };
    else if (message.type === "picked_folder") {
      element("project_path").value = message.path;
      element("project-modal").hidden = false;
    } else if (message.type === "layout_check") {
      try {
        post({ type: "layout_result", result: await runLayoutCheck() });
      } catch (error) {
        post({
          type: "layout_result",
          result: { passed: false, error: error.message },
        });
      }
    }
  });
selectTab("collaboration", "execution");
updateButtons();
if (bridge) post({ type: "ready" });
else showNotice("这是本地页面，需从小怪桌面程序打开才能连接 MCP。", true);
// 仅用于窗口内的状态显示；不会创建后台定时任务或自动重放消息/启动请求。
if (bridge) setInterval(refreshConnectionStatus, 1000);
