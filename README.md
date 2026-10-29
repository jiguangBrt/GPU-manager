# GPU-manager

浅色、简洁的本机 GPU 监控面板。通过已有 SSH 密钥或 SSH 别名读取公司 Linux 服务器，服务器端无需安装服务。支持在 Linux / Windows 本机运行，始终监听 `127.0.0.1`。

## 启动

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，并确保本机 `ssh` 命令可用。首次使用先获取源码，然后在项目目录执行：

```bash
git clone https://github.com/jiguangBrt/GPU-manager.git
cd GPU-manager
```

```bash
uv sync --locked --no-dev
uv run --locked --no-dev python -E run.py
```

浏览器打开 **http://127.0.0.1:8766**。uv 会在项目内创建独立 `.venv`，使用锁定的依赖。无需激活虚拟环境，也无需安装 Node.js。

也可运行 Linux 的 `./start.sh`，或 Windows 的 `start.bat`。端口被占用时：

```bash
uv run --locked --no-dev python -E run.py --port 8770
```

启动命令中的 `-E` 会忽略系统的 Python 环境变量，避免 ROS 等已有环境干扰项目依赖。

在终端按 Ctrl+C 停止。Linux 已实际运行验证；Windows 使用相同 Python 代码及系统 OpenSSH，当前未在 Windows 实机验证。

## 连接服务器

1. 在**运行本网站的同一台机器、同一个系统账户**下，确认免交互 SSH 登录可用。例如：

   ```bash
   ssh user@gpu.example.com
   ```

2. 点击页面“监控设置”，添加 SSH 别名（如 `company-gpu`）或 `user@gpu.example.com`，保存即可。端口和密钥路径可留空，以沿用本机 SSH config 和 Agent。支持添加多台服务器。
3. 首次连接需要你在终端核对服务器主机指纹。带密码的密钥应先通过 `ssh-add` 加载到 Agent；网站不会弹出密码输入框，也不存储密钥内容或登录密码。

Windows 请在“可选功能”中启用 OpenSSH 客户端，配置位于 `%USERPROFILE%\.ssh\config`；Linux 位于 `~/.ssh/config`。密钥路径填写运行网站的本机路径（Windows 例如 `C:\Users\you\.ssh\id_ed25519`），不要填写服务器上的路径。VPN、非默认端口或跳板机可以在 SSH config 中配置。主机指纹校验始终开启。

## 页面行为

- 直接平铺所有显卡，默认一台八卡服务器，按 nvidia-smi 编号排序；不筛选、不推荐或自动判定可用。
- 展示显存占用、实际剩余显存、瞬时 GPU 利用率、温度、功耗及进程数量；单位 GiB = MiB / 1024。
- 点击某张卡后，额外实时查询用户名、PID、进程类型、进程名、完整启动命令与占用显存。进程列表有独立刷新按钮，显示采集时间。用户名权限不足或进程已退出时显示“未知”。
- 默认每 5 秒采样一次，设置可调至 5–60 秒（实际周期还包含 SSH 命令耗时）。首页每 2 秒读取本机缓存，不会每 2 秒 SSH。
- 仅展示当前状态，不保存历史采样或绘制趋势图。断连立即标记数据过期，并从汇总指标中排除；保留最后一次状态供查看。
- 连接配置保存在本地 `.local/config.json`，不上传、不写入版本管理。复制项目到另一台机器时，需要复制此配置或在页面重新添加，并配置该机器自己的 SSH 密钥。
- 未添加服务器时，显示 `examples/nvidia-smi.txt` 中的虚构的八卡示例快照，并标明非实时数据。已配置但连接失败时不会退回示例数据。

仅执行只读查询：`nvidia-smi -q -x`，打开详情时增加 `ps -eo pid=,user:64=,args=`；后者只在本机保留对应 GPU PID 的信息。不会提交、结束或修改远端任务。当前远端采集适用于安装了 NVIDIA 驱动和 `ps` 的 Linux 服务器。

## 验证

```bash
uv sync --locked --group dev
uv run python -E -m pytest -q
```

覆盖 XML 指标与用户名解析、示例快照、SSH 调用方式、本地配置保存、断连过期、跨站写入拦截和详情查询。浏览器与真实 SSH 的检查另行执行。
