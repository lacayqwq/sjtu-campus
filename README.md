# 交大校园 · SJTU Campus

面向上海交通大学学生的 Codex 技能和 Kimi 插件。用自己的校园账号查询 Canvas 课程、作业和公告，查看研究生课表、校园邮件、当天课程直播及历史回放。课程资料可整理为带目录、可自测和打印的单文件 HTML 讲义。

## 安装

在希望长期保留源码的目录运行以下命令。符号链接让 Codex 直接读取仓库中的技能，之后更新仓库即可更新这一份安装。

```sh
git clone https://github.com/lacayqwq/sjtu-campus.git
cd sjtu-campus
mkdir -p ~/.codex/skills
ln -s "$PWD/skills/sjtu-campus" ~/.codex/skills/sjtu-campus
```

如果已经装过同名技能，先检查现有目录或链接，选择要维护的那一份。Kimi 用户可在支持本地插件的客户端导入仓库根目录，其中包含 `kimi.plugin.json`。

## 开始使用

桌面集成面向 macOS，基础脚本使用 Python 3.10 或更新版本。以下命令从仓库根目录运行。

```sh
SKILL_DIR="$PWD/skills/sjtu-campus"
python3 "$SKILL_DIR/scripts/canvas.py" configure
python3 "$SKILL_DIR/scripts/canvas.py" courses
python3 "$SKILL_DIR/scripts/classroom.py" live
```

首次配置 Canvas 会通过本机弹窗收集个人令牌，保存到 macOS 钥匙串。课堂视频复用 Canvas 登录，只查询当前账号权限内的数据。直播命令返回当天节次，再用 `live --video <节次ID>` 查看当前可用机位。

- **日常校园事务**：Canvas 作业与评论、教务处和课程公告、校园邮箱、课表、个人待办和 macOS 日历。
- **课堂资料**：课程回放、多机位直播、本地可选 FunASR 分块转写、字幕，以及离线 HTML 讲义。
- **定时汇报**：工作区接入说明涵盖公告去重、紧急 DDL 和作业审核。启用时需另行配置调度器、工作区脚本与通知渠道。水源社区功能依赖单独配置的水源 MCP。

具体参数、转写依赖和操作示例见[技能说明](skills/sjtu-campus/SKILL.md)。课表映射沿用 Dela 的部分实现，许可证和来源见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。
