import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Col, Collapse, Empty, Form, Input, InputNumber, Layout, Progress, Row, Select, Space, Statistic, Steps, Switch, Tag, Tooltip, Typography, message } from "antd";
import { ApiOutlined, ClusterOutlined, DatabaseOutlined, DeploymentUnitOutlined, PauseCircleOutlined, PlayCircleOutlined, StopOutlined, WarningOutlined } from "@ant-design/icons";
import * as echarts from "echarts";
import { addJumpHost, createTask, loadCoverage, loadJumpHosts, loadLostConnections, loadTasks, pauseTask, resumeTask, stopTask, summarize } from "./api";
import { baseTableSeedValidationError, normalizeBaseTableFormFields } from "./baseTableForm";
import { normalizeCrudRoutingFormFields } from "./crudRoutingForm";
import type { CoverageItem, CreateTaskPayload, FuzzTask, JumpHost, WorkerState } from "./types";

const { Sider, Content } = Layout;
const { Title, Text } = Typography;

function TrendChart({ clusterRate }: { clusterRate: number }) {
  useEffect(() => {
    const element = document.getElementById("trend-chart");
    if (!element) {
      return;
    }
    const chart = echarts.init(element);
    chart.setOption({
      grid: { left: 28, right: 12, top: 20, bottom: 24 },
      xAxis: {
        type: "category",
        data: ["10:00", "10:10", "10:20", "10:30", "10:40", "10:50"],
        axisLabel: { color: "#8ea0bb" }
      },
      yAxis: { type: "value", axisLabel: { color: "#8ea0bb" }, splitLine: { lineStyle: { color: "rgba(148,163,184,.12)" } } },
      series: [
        {
          name: "SQL 执行速率",
          type: "line",
          smooth: true,
          data: Array.from({ length: 6 }, () => clusterRate),
          areaStyle: { color: "rgba(32,201,151,.16)" },
          lineStyle: { color: "#20c997", width: 3 },
          symbol: "circle"
        }
      ]
    });
    const resize = () => chart.resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      chart.dispose();
    };
  }, [clusterRate]);
  return <div id="trend-chart" className="trend-chart" />;
}

function statusTag(task: FuzzTask) {
  if (task.status === "失败") {
    return <Tag color="error">失败</Tag>;
  }
  if (task.status === "恢复检测") {
    return <Tag color="error">告警</Tag>;
  }
  if (task.status === "已暂停") {
    return <Tag color="warning">已暂停</Tag>;
  }
  if (task.status === "已停止") {
    return <Tag>已停止</Tag>;
  }
  return <Tag color="success">运行中</Tag>;
}

function mergeTasksWithEvents(current: FuzzTask[], next: FuzzTask[]) {
  const eventsByTaskId = new Map(current.map((task) => [task.task_id, task.events]));
  return next.map((task) => ({
    ...task,
    events: task.events.length > 0 ? task.events : eventsByTaskId.get(task.task_id) ?? []
  }));
}

const stepIndexByPhase = new Map([
  ["连接实例", 0],
  ["准备基表", 1],
  ["执行 SQL", 2],
  ["恢复检测", 2],
  ["已暂停", 2]
]);

function stepDescription(task: FuzzTask, phase: string, runningDescription: string) {
  if (task.status === "失败" && task.phase === phase) {
    return "失败";
  }
  if (phase === "连接实例") {
    return stepIndexByPhase.get(task.phase) === 0 && task.status !== "失败" ? "进行中" : "完成";
  }
  if (phase === "准备基表") {
    if (stepIndexByPhase.get(task.phase) === 1 && task.status !== "失败") {
      return `进行中 · ${runningDescription}`;
    }
    return (stepIndexByPhase.get(task.phase) ?? 0) > 1
      ? `完成 · ${runningDescription}`
      : `等待 · ${runningDescription}`;
  }
  if (task.status === "已暂停") {
    return "已暂停";
  }
  if (task.status === "恢复检测") {
    return "恢复检测";
  }
  if (phase === "执行 SQL" && task.status === "失败" && task.phase !== "执行 SQL") {
    return "未开始";
  }
  return runningDescription;
}

function baseTablePreparationDescription(task: FuzzTask) {
  const mode = task.expand_base_table_columns ? "扩展列" : "核心列";
  return `${mode} · 每表 10～100 行`;
}

function GeneratorIdentity({
  label,
  version,
  seed
}: {
  label: string;
  version: string | null;
  seed: string | null;
}) {
  if (!version || !seed) {
    return null;
  }
  const reproductionId = `${version}:${seed}`;
  return (
    <div className="generator-identity">
      <Text type="secondary">{label}</Text>
      <Text
        type="secondary"
        className="base-table-reproduction-id"
        copyable={{ text: reproductionId, tooltips: ["复制复现标识", "已复制"] }}
      >
        <span className="base-table-seed">{reproductionId}</span>
      </Text>
    </div>
  );
}

function BaseTableMode({ task }: { task: FuzzTask }) {
  return (
    <div className="base-table-mode">
      <Text type="secondary" className="base-table-mode-text">
        {task.expand_base_table_columns
          ? "基表模式：扩展列（200～500 列）"
          : "基表模式：核心列（42 列）"}
      </Text>
      <div className="generator-identities">
        <GeneratorIdentity label="查询种子" version={task.query_generator_version} seed={task.query_seed} />
        {task.enable_crud && (
          <GeneratorIdentity label="CRUD 种子" version={task.crud_generator_version} seed={task.crud_seed} />
        )}
        {task.expand_base_table_columns && (
          <GeneratorIdentity
            label="基表种子"
            version={task.base_table_generator_version}
            seed={task.base_table_seed}
          />
        )}
      </div>
    </div>
  );
}

function workerStateColor(state: string) {
  if (state === "疑似卡住") {
    return "error";
  }
  if (state === "已暂停" || state === "恢复 worker 会话" || state === "恢复检测") {
    return "warning";
  }
  return "processing";
}

function threadTag(worker: FuzzTask["worker_states"][number]) {
  if (worker.thread_alive === true) {
    return <Tag color="success">线程存活</Tag>;
  }
  if (worker.thread_alive === false) {
    return <Tag color="error">线程退出</Tag>;
  }
  return <Tag>线程未知</Tag>;
}

function connectionTag(worker: FuzzTask["worker_states"][number]) {
  if (worker.connection_open === true) {
    return <Tag color="success">连接打开</Tag>;
  }
  if (worker.connection_open === false) {
    return <Tag color="error">连接关闭</Tag>;
  }
  return <Tag>连接未知</Tag>;
}

function WorkerRows({ workers }: { workers: WorkerState[] }) {
  if (workers.length === 0) {
    return <div className="empty-event">暂无 worker 状态</div>;
  }
  return (
    <div className="worker-grid">
      {workers.map((worker) => (
        <div className="worker-row" key={worker.worker_id}>
          <span>{worker.worker_key ?? `worker ${worker.worker_id}`}</span>
          <Tag color={worker.db_role === "primary" ? "blue" : "purple"}>
            {worker.db_role === "primary" ? "主节点" : worker.db_role === "replica" ? "备节点" : "旧连接"}
          </Tag>
          <Tag color={workerStateColor(worker.state)}>{worker.state}</Tag>
          {threadTag(worker)}
          {connectionTag(worker)}
          <b>{worker.sql_total} 条</b>
          <div className="worker-diagnostics">
            {worker.table_name && <span>表 {worker.table_name}</span>}
            <span>目标 {worker.target ?? "-"}</span>
            <span>连接 ID {worker.connection_id ?? "-"}</span>
            <span>重连 {worker.reconnect_total ?? worker.connection_ping_reconnect_count ?? 0}</span>
            {worker.reconnecting && <Tag color="warning">持续重连</Tag>}
            {worker.needs_reconnect && <Tag color="warning">待重连</Tag>}
          </div>
          {(worker.last_error || worker.last_connection_close_reason) && (
            <code className="worker-message">{worker.last_error ?? worker.last_connection_close_reason}</code>
          )}
        </div>
      ))}
    </div>
  );
}

function TaskCard({
  task,
  onPause,
  onResume,
  onStop
}: {
  task: FuzzTask;
  onPause: (taskId: string) => void;
  onResume: (taskId: string) => void;
  onStop: (taskId: string) => void;
}) {
  const currentStep = stepIndexByPhase.get(task.phase) ?? 0;
  const isFailed = task.status === "失败";
  const canPause = ["新建", "连接实例", "准备基表", "执行 SQL", "恢复检测"].includes(task.status);
  const canResume = task.status === "已暂停";
  const canStop = task.status !== "已停止" && task.status !== "失败";
  const [activeDetailKeys, setActiveDetailKeys] = useState<string[]>([]);
  const detailOpen = activeDetailKeys.includes("detail");
  const dmlWorkers = task.worker_states.filter((worker) => worker.worker_type === "dml");
  const queryWorkers = task.worker_states.filter((worker) => worker.worker_type !== "dml");
  const replicaRoute = task.replica_target ?? task.target;
  const runningWorkers = task.primary_reconnecting + task.replica_reconnecting;
  const items = [
    {
      key: "detail",
      label: detailOpen ? "收起详情" : "展开详情",
      children: (
        <Row gutter={[12, 12]}>
          <Col xs={24} lg={9}>
            <Card className="detail-card" bordered={false}>
              <Text type="secondary">连接与路由</Text>
              <div className="kv-line"><span>配置名</span><b>{task.jump_host ?? "直连"}</b></div>
              <div className="kv-line"><span>主写目标</span><b>{task.primary_target}</b></div>
              <div className="kv-line"><span>备读目标</span><b>{replicaRoute}</b></div>
              <div className="kv-line"><span>复用范围</span><b>当前任务全部连接</b></div>
              <div className="kv-line"><span>查询连接</span><b>{task.query_worker_total} 个独立连接</b></div>
              <div className="kv-line"><span>DML 连接</span><b>{task.crud_worker_total} 个独立连接</b></div>
              <div className="kv-line"><span>任务编号</span><b>{task.task_id}</b></div>
            </Card>
          </Col>
          <Col xs={24} lg={15}>
            <Card className="detail-card" bordered={false}>
              <Text type="secondary">任务状态</Text>
              {task.last_error ? (
                <Alert type="error" showIcon message={task.phase} description={task.last_error} className="task-error" />
              ) : (
                <div className="empty-event">当前无失败原因</div>
              )}
              <Text type="secondary">查询 worker（{queryWorkers.length}）</Text>
              <WorkerRows workers={queryWorkers} />
              {task.enable_crud && (
                <Collapse
                  className="dml-worker-collapse"
                  ghost
                  items={[
                    {
                      key: "dml-workers",
                      label: `逐表 DML worker（${dmlWorkers.length || task.crud_worker_total}，默认折叠）`,
                      children: <WorkerRows workers={dmlWorkers} />
                    }
                  ]}
                />
              )}
            </Card>
          </Col>
          <Col xs={24}>
            <Card className="detail-card" bordered={false}>
              <Text type="secondary">最近 lost connection 事件</Text>
              <div className="event-scroll">
                {task.events.length === 0 ? (
                  <div className="empty-event">暂无事件</div>
                ) : (
                  task.events.map((event) => (
                    <div className="event-row" key={`${event.timestamp}-${event.sql}`}>
                      <div className="event-time">{event.timestamp}</div>
                      <Tag color="error">丢连</Tag>
                      <code>{event.sql}</code>
                    </div>
                  ))
                )}
              </div>
            </Card>
          </Col>
        </Row>
      )
    }
  ];

  return (
    <Card className={`task-card ${task.status === "恢复检测" || task.status === "失败" ? "task-card-alert" : ""}`} bordered={false}>
      <Row align="top" gutter={[14, 12]} className="task-card-header">
        <Col xs={24} md={9}>
          <div className="node-name">{task.node_name}</div>
          <div className="route-summary">
            <span><b>主写</b> {task.primary_target}</span>
            <span><b>备读</b> {replicaRoute}</span>
          </div>
          <BaseTableMode task={task} />
        </Col>
        <Col xs={24} md={11}>
          <div className="query-summary">
            <div><span>成功查询</span><b>{task.success_query_total}</b></div>
            <div><span>失败查询</span><b className={task.failed_query_total > 0 ? "danger-text" : ""}>{task.failed_query_total}</b></div>
            <div><span>普通错误</span><b>{task.ordinary_error_total}</b></div>
            <div><span>lost connection 事件</span><b>{task.lost_connection_total}</b></div>
          </div>
          {task.enable_crud && (
            <div className="crud-summary-block">
              <div className="crud-summary-heading"><span>CRUD 统计</span><b>成功 / 失败</b></div>
              <div className="crud-summary">
                <div><span>INSERT</span><b>{task.insert_success_total} / {task.insert_failed_total}</b></div>
                <div><span>UPDATE</span><b>{task.update_success_total} / {task.update_failed_total}</b></div>
                <div><span>DELETE</span><b>{task.delete_success_total} / {task.delete_failed_total}</b></div>
              </div>
            </div>
          )}
          <div className="reconnect-summary">
            <span>主节点重连 {task.primary_reconnect_total}</span>
            <span>备节点重连 {task.replica_reconnect_total}</span>
            {runningWorkers > 0 && <Tag color="warning">{runningWorkers} 个连接持续重连</Tag>}
          </div>
        </Col>
        <Col xs={24} md={4}>
          <Space direction="vertical" size={6}>
            {statusTag(task)}
            <Space size={6}>
              {canPause && (
                <Tooltip title="暂停任务">
                  <Button size="small" icon={<PauseCircleOutlined />} onClick={() => onPause(task.task_id)} />
                </Tooltip>
              )}
              {canResume && (
                <Tooltip title="恢复任务">
                  <Button size="small" type="primary" icon={<PlayCircleOutlined />} onClick={() => onResume(task.task_id)} />
                </Tooltip>
              )}
              {canStop && (
                <Tooltip title="停止任务">
                  <Button size="small" danger icon={<StopOutlined />} onClick={() => onStop(task.task_id)} />
                </Tooltip>
              )}
            </Space>
          </Space>
        </Col>
      </Row>
      <div className="task-card-steps">
        <Steps
          size="small"
          current={currentStep}
          status={isFailed || task.status === "恢复检测" ? "error" : task.status === "已暂停" ? "wait" : "process"}
          items={[
            { title: "连接实例", description: stepDescription(task, "连接实例", "完成") },
            { title: "准备基表", description: stepDescription(task, "准备基表", baseTablePreparationDescription(task)) },
            {
              title: "执行 SQL",
              description: stepDescription(
                task,
                "执行 SQL",
                `${task.query_worker_total} 查询${task.enable_crud ? ` + ${task.crud_worker_total} DML` : ""} · ${task.sql_rate} 条/秒`
              )
            }
          ]}
        />
      </div>
      <Collapse
        ghost
        activeKey={activeDetailKeys}
        onChange={(keys) => {
          const nextKeys = Array.isArray(keys) ? keys : keys ? [keys] : [];
          setActiveDetailKeys(nextKeys.map(String));
        }}
        items={items}
      />
    </Card>
  );
}

function App() {
  const [tasks, setTasks] = useState<FuzzTask[]>([]);
  const [jumpHosts, setJumpHosts] = useState<JumpHost[]>([]);
  const [coverage, setCoverage] = useState<CoverageItem[]>([]);
  const [backendConnected, setBackendConnected] = useState(true);
  const [taskForm] = Form.useForm<CreateTaskPayload>();
  const [jumpForm] = Form.useForm<JumpHost>();
  const expandBaseTableColumns = Form.useWatch("expand_base_table_columns", taskForm) ?? false;
  const enableCrud = Form.useWatch("enable_crud", taskForm) ?? false;
  const replicaHost = Form.useWatch("replica_host", taskForm) ?? "";
  const metrics = useMemo(() => summarize(tasks), [tasks]);
  const taskIds = useMemo(() => tasks.map((task) => task.task_id).sort().join(","), [tasks]);
  const refreshTasks = async () => {
    const result = await loadTasks();
    setBackendConnected(result.backendConnected);
    setTasks((current) => mergeTasksWithEvents(current, result.tasks));
  };
  const coverageByCategory = useMemo(() => {
    const groups = new Map<string, { total: number; hit: number }>();
    coverage
      .filter((item) => item.implemented)
      .forEach((item) => {
        const current = groups.get(item.category) ?? { total: 0, hit: 0 };
        current.total += 1;
        if (item.hit_count > 0) {
          current.hit += 1;
        }
        groups.set(item.category, current);
      });
    return Array.from(groups.entries()).map(([category, value]) => ({
      category,
      percent: value.total === 0 ? 0 : Math.round((value.hit / value.total) * 100),
      hit: value.hit,
      total: value.total
    }));
  }, [coverage]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const refresh = async () => {
      const result = await loadTasks();
      if (cancelled) {
        return;
      }
      setBackendConnected(result.backendConnected);
      setTasks((current) => mergeTasksWithEvents(current, result.tasks));
      timer = window.setTimeout(refresh, 1000);
    };
    refresh();
    loadJumpHosts().then((result) => {
      if (!cancelled) {
        setJumpHosts(result);
      }
    });
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const refresh = async () => {
      const nextCoverage = await loadCoverage();
      if (cancelled) {
        return;
      }
      setCoverage(nextCoverage);
      timer = window.setTimeout(refresh, 5000);
    };
    refresh();
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const refresh = async () => {
      const ids = taskIds.length > 0 ? taskIds.split(",") : [];
      if (ids.length > 0) {
        const rows = await Promise.all(ids.map(async (taskId) => [taskId, await loadLostConnections(taskId)] as const));
        if (cancelled) {
          return;
        }
        const eventsByTaskId = new Map(rows);
        setTasks((current) => current.map((task) => ({ ...task, events: eventsByTaskId.get(task.task_id) ?? task.events })));
      }
      timer = window.setTimeout(refresh, 5000);
    };
    refresh();
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [taskIds]);

  const handleStartTask = async (values: CreateTaskPayload) => {
    try {
      const baseTableFields = normalizeBaseTableFormFields(values);
      const crudRoutingFields = normalizeCrudRoutingFormFields(values);
      const task = await createTask({
        ...values,
        ...baseTableFields,
        ...crudRoutingFields,
        node_name: values.node_name || `${values.host}:${values.port}`,
        jump_host: values.jump_host || null
      });
      setTasks((current) => [task, ...current]);
      loadCoverage().then(setCoverage);
      if (task.status === "失败") {
        message.error(task.last_error ?? "任务启动失败");
      } else {
        message.success("任务已启动");
      }
    } catch {
      message.error("任务启动失败，请检查数据库连接和账号密码");
    }
  };

  const handlePauseTask = async (taskId: string) => {
    try {
      await pauseTask(taskId);
      await refreshTasks();
      message.success("任务已暂停");
    } catch {
      message.error("暂停任务失败");
    }
  };

  const handleResumeTask = async (taskId: string) => {
    try {
      await resumeTask(taskId);
      await refreshTasks();
      message.success("任务已恢复");
    } catch {
      message.error("恢复任务失败");
    }
  };

  const handleStopTask = async (taskId: string) => {
    try {
      await stopTask(taskId);
      await refreshTasks();
      message.success("任务已停止");
    } catch {
      message.error("停止任务失败");
    }
  };

  const handleSaveJumpHost = async (values: JumpHost) => {
    const saved = await addJumpHost(values);
    setJumpHosts((current) => {
      const others = current.filter((item) => item.name !== saved.name);
      return [...others, saved];
    });
    jumpForm.resetFields();
    message.success("跳板机配置已保存");
  };

  const jumpOptions = [
    { label: "不使用跳板机", value: "" },
    ...jumpHosts.map((item) => ({
      label: `${item.name} · ${item.username}@${item.host}`,
      value: item.name
    }))
  ];

  return (
    <Layout className="app-shell">
      <Sider width={248} className="side" breakpoint="lg" collapsedWidth={0}>
        <div className="brand">
          <div className="brand-mark" />
          <div>
            <div className="brand-title">sql_fuzz</div>
            <div className="brand-subtitle">PolarDB MySQL 模糊测试控制台</div>
          </div>
        </div>
        <Space direction="vertical" size={8} className="nav">
          <Button type="primary" icon={<DeploymentUnitOutlined />} block>运行监控</Button>
          <Button icon={<DatabaseOutlined />} block>任务面板</Button>
          <Button icon={<ApiOutlined />} block>SQL 日志</Button>
          <Button icon={<ClusterOutlined />} block>覆盖统计</Button>
        </Space>
      </Sider>
      <Content className="main">
        <div className="topbar">
          <div>
            <Title level={2}>运行监控</Title>
            <Text type="secondary">操作界面与监控大屏合并展示。任务启动后自动进入任务面板。</Text>
          </div>
          <Space>
            <Button>导入节点</Button>
            <Button type="primary" icon={<PlayCircleOutlined />}>新建测试任务</Button>
          </Space>
        </div>

        <Row gutter={[12, 12]} className="metric-row">
          <Col xs={24} sm={12} lg={6}><Card bordered={false}><Statistic title="运行任务" value={metrics.activeTasks} suffix="个" /></Card></Col>
          <Col xs={24} sm={12} lg={6}><Card bordered={false}><Statistic title="成功查询" value={metrics.sqlTotal} /></Card></Col>
          <Col xs={24} sm={12} lg={6}><Card bordered={false}><Statistic title="lost connection 事件" value={metrics.lostConnection} valueStyle={{ color: "#ff7875" }} prefix={<WarningOutlined />} /></Card></Col>
          <Col xs={24} sm={12} lg={6}><Card bordered={false}><Statistic title="集群速率" value={metrics.clusterRate} suffix="条/秒" /></Card></Col>
        </Row>

        {!backendConnected && (
          <Alert
            type="warning"
            showIcon
            message="后端未连接"
            description="当前页面没有加载任何默认任务数据。请先启动 FastAPI 后端，或检查 /api 代理配置。"
            className="form-note"
          />
        )}

        <Row gutter={[16, 16]}>
          <Col xs={24} xl={16}>
            <Space direction="vertical" size={12} className="task-list">
              {tasks.length === 0 ? (
                <Card bordered={false}>
                  <Empty description={backendConnected ? "暂无任务" : "后端未连接，暂无任务"} />
                </Card>
              ) : (
                tasks.map((task) => (
                  <TaskCard
                    key={task.task_id}
                    task={task}
                    onPause={handlePauseTask}
                    onResume={handleResumeTask}
                    onStop={handleStopTask}
                  />
                ))
              )}
            </Space>
          </Col>
          <Col xs={24} xl={8}>
            <Space direction="vertical" size={16} className="right-panel">
              <Card title="新建任务" bordered={false}>
                <Form
                  layout="vertical"
                  form={taskForm}
                  initialValues={{
                    node_name: "local-mysql",
                    host: "127.0.0.1",
                    port: 3306,
                    username: "root",
                    password: "",
                    jump_host: "",
                    replica_host: "",
                    replica_port: null,
                    thread_count: 16,
                    enable_crud: false,
                    expand_base_table_columns: false,
                    base_table_generator_version: "v1",
                    base_table_seed: ""
                  }}
                  onFinish={handleStartTask}
                >
                  <Form.Item name="jump_host" label="跳板机配置">
                    <Select options={jumpOptions} />
                  </Form.Item>
                  <Row gutter={8}>
                    <Col xs={24} sm={14}><Form.Item name="host" label="数据库地址" rules={[{ required: true, message: "请输入数据库地址" }]}><Input /></Form.Item></Col>
                    <Col xs={24} sm={10}><Form.Item name="port" label="端口" rules={[{ required: true, message: "请输入端口" }]}><InputNumber min={1} max={65535} className="full-input" /></Form.Item></Col>
                  </Row>
                  <Row gutter={8}>
                    <Col xs={24} sm={14}>
                      <Form.Item
                        name="replica_host"
                        label="备节点 IP / 地址（可选）"
                        extra="填写后，所有随机查询只走备节点。"
                      >
                        <Input placeholder="例如 10.0.0.12" />
                      </Form.Item>
                    </Col>
                    <Col xs={24} sm={10}>
                      <Form.Item name="replica_port" label="备节点端口" extra="留空继承主节点端口">
                        <InputNumber min={1} max={65535} className="full-input" placeholder="继承" />
                      </Form.Item>
                    </Col>
                  </Row>
                  <Row gutter={8}>
                    <Col xs={24} sm={12}><Form.Item name="username" label="用户名" rules={[{ required: true, message: "请输入用户名" }]}><Input /></Form.Item></Col>
                    <Col xs={24} sm={12}><Form.Item name="password" label="密码" rules={[{ required: true, message: "请输入密码" }]}><Input.Password /></Form.Item></Col>
                  </Row>
                  <Form.Item name="node_name" label="任务名称" rules={[{ required: true, message: "请输入任务名称" }]}>
                    <Input />
                  </Form.Item>
                  <Form.Item
                    name="thread_count"
                    label="备节点查询线程数"
                    rules={[{ required: true, message: "请输入查询线程数" }]}
                  >
                    <InputNumber min={1} max={128} className="full-input" />
                  </Form.Item>
                  <Form.Item
                    name="enable_crud"
                    label="启用逐表 CRUD"
                    valuePropName="checked"
                    extra="开启后，74 张永久基表各启动 1 个主节点 DML 线程。"
                  >
                    <Switch checkedChildren="已开启" unCheckedChildren="已关闭" />
                  </Form.Item>
                  {enableCrud && (
                    <Alert
                      type="warning"
                      showIcon
                      message="主写备读并发已开启"
                      description={replicaHost.trim()
                        ? "INSERT / UPDATE / DELETE 只走主节点；查询线程只走备节点，每个 worker 独占一个连接。"
                        : "INSERT / UPDATE / DELETE 只走主节点；尚未填写备节点，查询暂时复用主节点，每个 worker 仍独占连接。"}
                      className="form-note"
                    />
                  )}
                  <Form.Item
                    name="expand_base_table_columns"
                    label="扩展基表列（每表 200～500 列）"
                    valuePropName="checked"
                  >
                    <Switch checkedChildren="已开启" unCheckedChildren="已关闭" />
                  </Form.Item>
                  {expandBaseTableColumns && (
                    <>
                      <Form.Item
                        name="base_table_generator_version"
                        label="生成器版本"
                        rules={[{ required: true, message: "请选择生成器版本" }]}
                      >
                        <Select options={[{ label: "v1", value: "v1" }]} />
                      </Form.Item>
                      <Form.Item
                        name="base_table_seed"
                        label="复现种子"
                        extra="留空时由后端生成；填写时仅支持规范的无符号 64 位十进制整数。"
                        rules={[
                          {
                            validator: async (_, value: unknown) => {
                              const error = baseTableSeedValidationError(value);
                              if (error) {
                                throw new Error(error);
                              }
                            }
                          }
                        ]}
                      >
                        <Input inputMode="numeric" autoComplete="off" maxLength={20} placeholder="留空自动生成，例如 12345" />
                      </Form.Item>
                    </>
                  )}
                  <Alert type="info" showIcon message="后台会自动创建并使用 test 库，任务表单无需填写目标库。" className="form-note" />
                  <Button type="primary" block icon={<PlayCircleOutlined />} htmlType="submit">启动任务</Button>
                </Form>
              </Card>
              <Card title="跳板机管理" bordered={false}>
                <Form
                  layout="vertical"
                  form={jumpForm}
                  initialValues={{ name: "jump-prod", host: "", port: 22, username: "ops", password: "", private_key_path: "" }}
                  onFinish={handleSaveJumpHost}
                >
                  <Row gutter={8}>
                    <Col xs={24} sm={12}><Form.Item name="name" label="配置名" rules={[{ required: true, message: "请输入配置名" }]}><Input /></Form.Item></Col>
                    <Col xs={24} sm={12}><Form.Item name="username" label="SSH 用户" rules={[{ required: true, message: "请输入 SSH 用户" }]}><Input /></Form.Item></Col>
                  </Row>
                  <Row gutter={8}>
                    <Col xs={24} sm={16}><Form.Item name="host" label="跳板机地址" rules={[{ required: true, message: "请输入跳板机地址" }]}><Input /></Form.Item></Col>
                    <Col xs={24} sm={8}><Form.Item name="port" label="SSH 端口" rules={[{ required: true, message: "请输入 SSH 端口" }]}><InputNumber min={1} max={65535} className="full-input" /></Form.Item></Col>
                  </Row>
                  <Form.Item name="password" label="SSH 密码">
                    <Input.Password placeholder="推荐填写账号密码；使用私钥时可留空" />
                  </Form.Item>
                  <Form.Item name="private_key_path" label="私钥路径">
                    <Input placeholder="可选，例如 ~/.ssh/id_rsa" />
                  </Form.Item>
                  <Button block onClick={() => jumpForm.submit()}>保存跳板机</Button>
                </Form>
              </Card>
              <Card title="执行速率趋势" bordered={false}>
                <TrendChart clusterRate={metrics.clusterRate} />
              </Card>
              <Alert
                type="error"
                showIcon
                message="最近 lost connection"
                description="同一节点 10 分钟内只记录第一次事件，大屏统计按去重后事件数展示。"
              />
              <Card title="覆盖进度" bordered={false}>
                <Space direction="vertical" className="coverage">
                  {coverageByCategory.length === 0 ? (
                    <Text type="secondary">等待任务生成 SQL 后统计覆盖。</Text>
                  ) : (
                    coverageByCategory.map((item, index) => (
                      <div key={item.category} className="coverage-row">
                        <Text>{item.category} · {item.hit}/{item.total}</Text>
                        <Progress percent={item.percent} strokeColor={["#20c997", "#3b82f6", "#f59e0b", "#ff7875", "#9254de"][index % 5]} />
                      </div>
                    ))
                  )}
                </Space>
              </Card>
            </Space>
          </Col>
        </Row>
      </Content>
    </Layout>
  );
}

export default App;
