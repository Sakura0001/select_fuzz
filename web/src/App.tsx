import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Col, Collapse, Empty, Form, Input, InputNumber, Layout, Progress, Row, Select, Space, Statistic, Steps, Tag, Tooltip, Typography, message } from "antd";
import { ApiOutlined, ClusterOutlined, DatabaseOutlined, DeploymentUnitOutlined, PauseCircleOutlined, PlayCircleOutlined, StopOutlined, WarningOutlined } from "@ant-design/icons";
import * as echarts from "echarts";
import { addJumpHost, createTask, loadCoverage, loadJumpHosts, loadLostConnections, loadTasks, pauseTask, resumeTask, stopTask, summarize } from "./api";
import type { CoverageItem, CreateTaskPayload, FuzzTask, JumpHost } from "./types";

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
      return "进行中";
    }
    return (stepIndexByPhase.get(task.phase) ?? 0) > 1 ? "完成" : "等待";
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
  const canPause = task.status === "执行 SQL" || task.status === "恢复检测";
  const canResume = task.status === "已暂停";
  const canStop = task.status !== "已停止" && task.status !== "失败";
  const [activeDetailKeys, setActiveDetailKeys] = useState<string[]>([]);
  const detailOpen = activeDetailKeys.includes("detail");
  const items = [
    {
      key: "detail",
      label: detailOpen ? "收起详情" : "展开详情",
      children: (
        <Row gutter={12}>
          <Col span={9}>
            <Card className="detail-card" bordered={false}>
              <Text type="secondary">跳板机信息</Text>
              <div className="kv-line"><span>配置名</span><b>{task.jump_host ?? "直连"}</b></div>
              <div className="kv-line"><span>目标实例</span><b>{task.target}</b></div>
              <div className="kv-line"><span>复用范围</span><b>当前任务全部连接</b></div>
              <div className="kv-line"><span>并发线程</span><b>{task.thread_count} 个 worker</b></div>
              <div className="kv-line"><span>任务编号</span><b>{task.task_id}</b></div>
            </Card>
          </Col>
          <Col span={15}>
            <Card className="detail-card" bordered={false}>
              <Text type="secondary">任务状态</Text>
              {task.last_error ? (
                <Alert type="error" showIcon message={task.phase} description={task.last_error} className="task-error" />
              ) : (
                <div className="empty-event">当前无失败原因</div>
              )}
              <div className="worker-grid">
                {task.worker_states.length === 0 ? (
                  <div className="empty-event">暂无 worker 状态</div>
                ) : (
                  task.worker_states.map((worker) => (
                    <div className="worker-row" key={worker.worker_id}>
                      <span>worker {worker.worker_id}</span>
                      <Tag color={workerStateColor(worker.state)}>{worker.state}</Tag>
                      {threadTag(worker)}
                      {connectionTag(worker)}
                      <b>{worker.sql_total} 条</b>
                      <div className="worker-diagnostics">
                        <span>连接 ID {worker.connection_id ?? "-"}</span>
                        <span>连接 {worker.connection_connect_count ?? "-"}</span>
                        <span>关闭 {worker.connection_close_count ?? "-"}</span>
                        <span>ping 重连 {worker.connection_ping_reconnect_count ?? "-"}</span>
                        {worker.needs_reconnect && <Tag color="warning">待重连</Tag>}
                      </div>
                      {(worker.last_error || worker.last_connection_close_reason) && (
                        <code className="worker-message">{worker.last_error ?? worker.last_connection_close_reason}</code>
                      )}
                    </div>
                  ))
                )}
              </div>
            </Card>
          </Col>
          <Col span={24}>
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
      <Row align="middle" gutter={14}>
        <Col span={5}>
          <div className="node-name">{task.node_name}</div>
          <Text type="secondary">{task.target} · {task.jump_host ?? "直连"}</Text>
        </Col>
        <Col span={12}>
          <Steps
            size="small"
            current={currentStep}
            status={isFailed || task.status === "恢复检测" ? "error" : task.status === "已暂停" ? "wait" : "process"}
            items={[
              { title: "连接实例", description: stepDescription(task, "连接实例", "完成") },
              { title: "准备基表", description: stepDescription(task, "准备基表", "每表 10 行") },
              { title: "执行 SQL", description: stepDescription(task, "执行 SQL", `${task.thread_count} 线程 · ${task.sql_rate} 条/秒`) }
            ]}
          />
        </Col>
        <Col span={3}>
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
        <Col span={4}>
          <div className="query-summary">
            <div><span>成功查询</span><b>{task.success_query_total}</b></div>
            <div><span>失败查询</span><b className={task.failed_query_total > 0 ? "danger-text" : ""}>{task.failed_query_total}</b></div>
            <div><span>普通错误</span><b>{task.ordinary_error_total}</b></div>
            <div><span>lost connection 事件</span><b>{task.lost_connection_total}</b></div>
          </div>
        </Col>
      </Row>
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
      const task = await createTask({
        ...values,
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
      <Sider width={248} className="side">
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

        <Row gutter={12} className="metric-row">
          <Col span={6}><Card bordered={false}><Statistic title="运行任务" value={metrics.activeTasks} suffix="个" /></Card></Col>
          <Col span={6}><Card bordered={false}><Statistic title="成功查询" value={metrics.sqlTotal} /></Card></Col>
          <Col span={6}><Card bordered={false}><Statistic title="lost connection 事件" value={metrics.lostConnection} valueStyle={{ color: "#ff7875" }} prefix={<WarningOutlined />} /></Card></Col>
          <Col span={6}><Card bordered={false}><Statistic title="集群速率" value={metrics.clusterRate} suffix="条/秒" /></Card></Col>
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

        <Row gutter={16}>
          <Col span={16}>
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
          <Col span={8}>
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
                    thread_count: 1
                  }}
                  onFinish={handleStartTask}
                >
                  <Form.Item name="jump_host" label="跳板机配置">
                    <Select options={jumpOptions} />
                  </Form.Item>
                  <Row gutter={8}>
                    <Col span={14}><Form.Item name="host" label="数据库地址" rules={[{ required: true, message: "请输入数据库地址" }]}><Input /></Form.Item></Col>
                    <Col span={10}><Form.Item name="port" label="端口" rules={[{ required: true, message: "请输入端口" }]}><InputNumber min={1} max={65535} className="full-input" /></Form.Item></Col>
                  </Row>
                  <Row gutter={8}>
                    <Col span={12}><Form.Item name="username" label="用户名" rules={[{ required: true, message: "请输入用户名" }]}><Input /></Form.Item></Col>
                    <Col span={12}><Form.Item name="password" label="密码" rules={[{ required: true, message: "请输入密码" }]}><Input.Password /></Form.Item></Col>
                  </Row>
                  <Form.Item name="node_name" label="任务名称" rules={[{ required: true, message: "请输入任务名称" }]}>
                    <Input />
                  </Form.Item>
                  <Form.Item
                    name="thread_count"
                    label="并发线程数"
                    rules={[{ required: true, message: "请输入并发线程数" }]}
                  >
                    <InputNumber min={1} max={128} className="full-input" />
                  </Form.Item>
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
                    <Col span={12}><Form.Item name="name" label="配置名" rules={[{ required: true, message: "请输入配置名" }]}><Input /></Form.Item></Col>
                    <Col span={12}><Form.Item name="username" label="SSH 用户" rules={[{ required: true, message: "请输入 SSH 用户" }]}><Input /></Form.Item></Col>
                  </Row>
                  <Row gutter={8}>
                    <Col span={16}><Form.Item name="host" label="跳板机地址" rules={[{ required: true, message: "请输入跳板机地址" }]}><Input /></Form.Item></Col>
                    <Col span={8}><Form.Item name="port" label="SSH 端口" rules={[{ required: true, message: "请输入 SSH 端口" }]}><InputNumber min={1} max={65535} className="full-input" /></Form.Item></Col>
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
