import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Col, Collapse, Form, Input, InputNumber, Layout, Progress, Row, Select, Space, Statistic, Steps, Tag, Typography, message } from "antd";
import { ApiOutlined, ClusterOutlined, DatabaseOutlined, DeploymentUnitOutlined, PlayCircleOutlined, WarningOutlined } from "@ant-design/icons";
import * as echarts from "echarts";
import { addJumpHost, createTask, loadJumpHosts, loadTasks, summarize } from "./api";
import type { CreateTaskPayload, FuzzTask, JumpHost } from "./types";

const { Sider, Content } = Layout;
const { Title, Text } = Typography;

function TrendChart() {
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
          data: [380, 421, 407, 455, 431, 448],
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
  }, []);
  return <div id="trend-chart" className="trend-chart" />;
}

function statusTag(task: FuzzTask) {
  if (task.status === "恢复检测") {
    return <Tag color="error">告警</Tag>;
  }
  if (task.status === "已停止") {
    return <Tag>已停止</Tag>;
  }
  return <Tag color="success">运行中</Tag>;
}

function TaskCard({ task }: { task: FuzzTask }) {
  const items = [
    {
      key: `${task.task_id}-detail`,
      label: "展开详情",
      children: (
        <Row gutter={12}>
          <Col span={9}>
            <Card className="detail-card" bordered={false}>
              <Text type="secondary">跳板机信息</Text>
              <div className="kv-line"><span>配置名</span><b>{task.jump_host ?? "直连"}</b></div>
              <div className="kv-line"><span>目标实例</span><b>{task.target}</b></div>
              <div className="kv-line"><span>复用范围</span><b>当前任务全部连接</b></div>
              <div className="kv-line"><span>任务编号</span><b>{task.task_id}</b></div>
            </Card>
          </Col>
          <Col span={15}>
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
    <Card className={`task-card ${task.status === "恢复检测" ? "task-card-alert" : ""}`} bordered={false}>
      <Row align="middle" gutter={14}>
        <Col span={5}>
          <div className="node-name">{task.node_name}</div>
          <Text type="secondary">{task.target} · {task.jump_host ?? "直连"}</Text>
        </Col>
        <Col span={12}>
          <Steps
            size="small"
            current={task.status === "恢复检测" ? 2 : 2}
            status={task.status === "恢复检测" ? "error" : "process"}
            items={[
              { title: "连接实例", description: "完成" },
              { title: "准备基表", description: "每表 10 行" },
              { title: "执行 SQL", description: task.status === "恢复检测" ? "恢复检测" : `${task.sql_rate} 条/秒` }
            ]}
          />
        </Col>
        <Col span={3}>{statusTag(task)}</Col>
        <Col span={4}>
          <Statistic title="lost connection" value={task.lost_connection_total} valueStyle={{ color: task.lost_connection_total > 0 ? "#ff7875" : "#95de64" }} />
        </Col>
      </Row>
      <Collapse ghost items={items} />
    </Card>
  );
}

function App() {
  const [tasks, setTasks] = useState<FuzzTask[]>([]);
  const [jumpHosts, setJumpHosts] = useState<JumpHost[]>([]);
  const [taskForm] = Form.useForm<CreateTaskPayload>();
  const [jumpForm] = Form.useForm<JumpHost>();
  const metrics = useMemo(() => summarize(tasks), [tasks]);

  useEffect(() => {
    loadTasks().then(setTasks);
    loadJumpHosts().then(setJumpHosts);
  }, []);

  const handleStartTask = async (values: CreateTaskPayload) => {
    const task = await createTask({
      ...values,
      node_name: values.node_name || `${values.host}:${values.port}`,
      jump_host: values.jump_host || null
    });
    setTasks((current) => [task, ...current]);
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
          <Col span={6}><Card bordered={false}><Statistic title="已执行 SQL" value={metrics.sqlTotal} /></Card></Col>
          <Col span={6}><Card bordered={false}><Statistic title="lost connection" value={metrics.lostConnection} valueStyle={{ color: "#ff7875" }} prefix={<WarningOutlined />} /></Card></Col>
          <Col span={6}><Card bordered={false}><Statistic title="集群速率" value={metrics.clusterRate} suffix="条/秒" /></Card></Col>
        </Row>

        <Row gutter={16}>
          <Col span={16}>
            <Space direction="vertical" size={12} className="task-list">
              {tasks.map((task) => <TaskCard key={task.task_id} task={task} />)}
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
                    jump_host: ""
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
                  <Alert type="info" showIcon message="后台会自动创建并使用 test 库，任务表单无需填写目标库。" className="form-note" />
                  <Button type="primary" block icon={<PlayCircleOutlined />} htmlType="submit">启动任务</Button>
                </Form>
              </Card>
              <Card title="跳板机管理" bordered={false}>
                <Form
                  layout="vertical"
                  form={jumpForm}
                  initialValues={{ name: "jump-prod", host: "", port: 22, username: "ops", private_key_path: "" }}
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
                  <Form.Item name="private_key_path" label="私钥路径">
                    <Input placeholder="可选，例如 ~/.ssh/id_rsa" />
                  </Form.Item>
                  <Button block onClick={() => jumpForm.submit()}>保存跳板机</Button>
                </Form>
              </Card>
              <Card title="执行速率趋势" bordered={false}>
                <TrendChart />
              </Card>
              <Alert
                type="error"
                showIcon
                message="最近 lost connection"
                description="同一节点 10 分钟内只记录第一次事件，大屏统计按去重后事件数展示。"
              />
              <Card title="覆盖进度" bordered={false}>
                <Space direction="vertical" className="coverage">
                  <Text>SELECT 结构</Text><Progress percent={58} strokeColor="#20c997" />
                  <Text>表达式算子</Text><Progress percent={42} strokeColor="#3b82f6" />
                  <Text>向量函数</Text><Progress percent={35} strokeColor="#f59e0b" />
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
