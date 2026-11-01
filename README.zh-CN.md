# bus-bridge

> 项目状态：vibe-coding 产物, 未完整审查

给沙盒用的私有 D-Bus 会话，只把桌面真正需要的三条通道接到宿主机：

- 通知（`org.freedesktop.Notifications`）
- 托盘（`org.kde.StatusNotifierWatcher` / `org.kde.StatusNotifierItem`，含 DBusMenu）
- 媒体播放器（`org.mpris.MediaPlayer2.*`，沙盒 → 宿主）

[English](README.md)

## 架构

沙盒里跑着一条自己的 `dbus-daemon`，配置里没有任何 service 目录，与宿主总线
没有任何共享：宿主的名字、可激活服务和 portal 在沙盒内都不存在。

桥只把上面三条通道接到这条总线上。其余所有 D-Bus 消息都留在沙盒里，多个应用
共享一条完整、无限制的总线，可以随意申请名字、互相发消息、注册对象。

### 三个适配器

| 适配器 | 方向 | 做法 |
| --- | --- | --- |
| `notifications` | 双向 | 沙盒侧由桥持有 `org.freedesktop.Notifications`；桥作为宿主通知服务的客户端，维护两套 id 的映射（含 `replaces_id`、`CloseNotification`、`NotificationClosed`、`ActionInvoked`）。应用退出时关闭其遗留通知。 |
| `sni` | 双向 | 沙盒侧由桥持有 `org.kde.StatusNotifierWatcher`；应用注册托盘项时，桥在宿主上申请一个 `org.kde.StatusNotifierItem-<pid>-<n>` 名字并向宿主 watcher 注册，再把属性读取、`Activate`、`NewTitle`/`NewIcon`、DBusMenu 全部按原路径转发。 |
| `mpris` | 沙盒 → 宿主 | 监听沙盒内 `org.mpris.MediaPlayer2.*` 名字的获取/释放，在宿主上以同名（冲突时加 `.sandbox` 后缀）导出，转发调用、属性与信号。 |

### 为什么每个导入服务要单独一条宿主连接

D-Bus 上一条连接只有一个 unique name，所有信号都带着这个 sender。
如果多个沙盒服务共用一个连接导出，宿主侧 `sender='org.foo'` 的匹配会把
`org.bar` 的信号也收进来。因此 `Router.import_service()` 为每个被导出的
服务新建一条宿主连接。

## 安装

```sh
python3 -m venv --system-site-packages ~/.venvs/deskcage
. ~/.venvs/deskcage/bin/activate
pip install .
```

也可以 `pipx install --system-site-packages .`。

## 使用

```sh
bus-bridge doctor                 # 检查环境与宿主服务
bus-bridge run -- bash            # 起一条私有总线并在其中执行命令
bus-bridge serve --state-dir DIR  # 只起总线+桥，打印地址后常驻
```

`run` 会等桥把名字都注册好再执行命令，命令退出后销毁总线。

通知默认不重写 `sender-pid` hint；如果宿主的通知服务依赖它匹配应用图标，
加 `--pid-hint` 打开（桥会异步查询发送方 PID，首次开销一次同步调用）。

手动组合：

```sh
bus-bridge bus --state-dir /tmp/sb        # 打印 unix:path=...
bus-bridge bridge --private unix:path=/tmp/sb/bus
```

## 测试

```sh
tests/run_all.sh
```

每个测试都会自建两条独立的 `dbus-daemon`（模拟宿主与沙盒）和一个桥进程：

- `test_notifications.py`：Notify、id 映射、替换、关闭、动作、服务端关闭通知
- `test_mpris.py`：宿主可见播放器、方法调用、属性读取、`PropertiesChanged`、
  `Seeked` 中继、播放器退出后名字消失
- `test_sni.py`：托盘项注册（名字式与路径式）、属性、`Activate`、`NewTitle`、
  DBusMenu、多应用共存、退出清理
- `test_isolation.py`：沙盒名字不外泄、宿主名字不可见、无 activatable 服务
- `test_pid_hint.py`：`--pid-hint` 不会在过滤器里死锁
- `test_cli.py`：`bus-bridge run` 传入的私有总线可用且干净
- `test_packaging.py`：从模拟的安装目录（site-packages）运行 `-m bus_bridge` 和
  `bus-bridge run`，不依赖源码树

测试依赖 PyGObject（`python-gobject`）。

## 已知边界

- **不桥接 portal。** xdg-desktop-portal 的权限与身份绑定在调用方上，
  正确做法是每个沙盒单独开一条 `xdg-dbus-proxy` 通道，而不是复用这条桥。
- **MPRIS 只有沙盒 → 宿主方向。** 沙盒应用看不到宿主上已有的播放器。
- 托盘菜单对象按路径转发；如果某个应用把菜单放在非常规路径且不发
  `NewMenu`，需要额外声明。
- 宿主通知/托盘服务缺席时，桥仍然会正常持有沙盒侧名字，调用返回错误。
- 一个沙盒只对应一个桥进程；多个沙盒互不共享私有总线。
