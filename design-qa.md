# Product Design QA — 策略运行剧场

- source visual truth path: `/Users/xueds/.codex/generated_images/019f49d9-146a-7493-b23f-1b399cecc427/exec-f8111061-ea0f-4a24-8935-d5fc1868c14e.png`
- implementation screenshot path: `/tmp/quant-theater-dashboard-final-v3.jpg`
- viewport: `1440 × 1024`
- state: 已登录；`paper_v2` 空仓；策略观察进程停止；因子判断阶段聚焦
- comparison input: 同一次视觉检查中打开了选定设计图和最终浏览器截图

## Findings

当前没有可执行的 P0、P1 或 P2 问题。

- 字体与层级：实现保留了设计图的大标题、窄体数字、低对比辅助文本和红色强调层级；中文换行、行高和数字对齐均正常。
- 间距与布局：左侧导航、中部策略剧场、右侧风险栏的三栏比例与设计图一致；首屏流程、四张阶段卡、策略说明和最近事件保持相同阅读顺序。
- 颜色与视觉令牌：背景、边框、暖白文字、朱红能量色和绿色健康状态均已集中到主题变量；语义色对比满足暗色界面阅读需求。
- 图片与资产：红色能量场使用项目内压缩后的真实栅格资产 `web/frontend/src/assets/strategy-energy-field.jpg`；界面图标统一使用 Tabler 图标库，没有占位图、手绘 SVG 或文本符号替代。
- 文案与内容：设计稿中的演示行情被替换为真实账户净值、候选池、T+1 状态和风险预算；空账户状态有明确说明，没有伪造交易或目标仓位。
- 交互：四阶段按钮可切换焦点；左侧工作区可进入组合持仓并返回策略剧场；登录、退出、交易筛选和日志控制保留原有能力。
- 响应式与可访问性：`390 × 844` 下无页面水平溢出；导航可横向滚动；按钮具备可访问名称和选中状态；保留 `prefers-reduced-motion` 降低动态效果支持。

## Full-view comparison evidence

- 设计图：三栏结构约为 `194 / 1066 / 227`，首屏流程区后接四阶段卡片、说明条和事件时间线。
- 实现：浏览器计算布局为 `194 / 1018 / 228`，在较窄的 1440 像素目标视口中保持相同比例和内容顺序。
- 主视觉、阶段聚焦、风险仪表、空仓状态及底部事件区域均在首屏可见，没有遮挡、裁切或持久控件溢出。

## Focused-region comparison evidence

没有额外裁切局部图。源图与实现截图均以接近 1440 像素的原始分辨率打开，导航文字、四阶段卡片、风险仪表和事件区在完整视图中可直接辨认，局部裁切不会增加有效判断信息。

## Comparison history

### Iteration 1

- earlier findings:
  - [P2] 实现首轮流程区比设计图矮约 20 像素，导致阶段卡片和事件区纵向节奏偏紧。
  - [P2] 右侧时钟和风险仪表区域过短，账户指标开始位置明显早于设计图。
  - [P2] 策略说明及事件辅助文字偏小。
- fixes made:
  - 流程区高度由 324 调整为 342，阶段卡片最小高度调整为 400。
  - 左侧品牌/运行状态区和右侧时钟/风险仪表区按设计图比例加高。
  - 说明文字、事件标题及阶段标题字号提高，并增加事件区最小高度。
- post-fix visual evidence: `/tmp/quant-theater-dashboard-final-v3.jpg`
- result: P2 问题已消除。

## Primary interactions tested

- 登录成功并进入 `/quantify/`。
- 点击“组合持仓”后显示对应工作区，再点击“返回策略剧场”恢复首屏。
- 点击“阶段 4：T+1 执行”后 `aria-pressed` 更新为 `true`。
- `390 × 844` 视口中 `bodyScrollWidth <= bodyClientWidth`，不存在页面级横向溢出。
- 浏览器控制台错误及警告：`0`。

## Follow-up polish

- [P3] 当净值只有一个快照时，迷你曲线只显示单点；积累第二个快照后会自动形成曲线。

## Implementation checklist

- [x] 选定视觉稿已解析并落地
- [x] 核心导航和阶段交互可用
- [x] 真实数据接口保持不变
- [x] 桌面与移动端浏览器验证完成
- [x] 前端测试与生产构建通过
- [x] 无 P0/P1/P2 遗留问题

final result: passed
