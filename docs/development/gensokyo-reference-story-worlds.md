# 幻想乡原作资料与有主线世界

本轮从 `99fbe54` 开出 `feature/gensokyo-reference-story-worlds`。幻想乡资料包使用独立标识，《回灯之约》保留既有包标识、版本和存档。下文是启动记录，条目数量与功能均由后续验收确认。

## 原作资料的证据

| 首批来源 | 已核对 | 仍需原文核实 |
| --- | --- | --- |
| [东方 Project 官方概述](https://touhou-project.news/touhou_project/) | 系列背景和作品范围 | 每位人物、每个地点和每种能力的具体说法 |
| [《东方红魔乡》官方简介](https://touhou-project.news/titles/th06/) | 幻想乡被雾笼罩，博丽神社的巫女灵梦着手调查 | 异变的世界内日期、红魔馆地图和通行条件 |
| [《东方妖妖梦》官方简介](https://touhou-project.news/titles/th07/) | 本应花开的季节仍持续下雪，冥界樱花是故事焦点 | 具体日期、各路线结局和八云紫条目的细节 |
| [《东方永夜抄》官方简介](https://touhou-project.news/titles/th08/) | 真满月消失，人与妖怪试图在黎明前调查并恢复满月 | 异变的准确日期、迷途竹林的具体路线与涉事人物的位置 |
| [一迅社《东方求闻史纪》书目](https://data.ichijinsha.co.jp/detail/75801063) | 书目及稗田阿求编纂《幻想乡缘起》的叙述框架 | 凡涉及书中条目的事实以正文位置校对，记录叙述者和观察视角 |

为每条事实保存作品、版本、原文定位、依据摘述与确定性。发行日期单列。来源相互冲突时保留双方，游戏中的模拟数值和剧情结果各有独立出处。统计人物时合并异名。地点资料分别登记地区、地标、异空间、组织与通行关系，且对未知方位或路线保持未知。目标覆盖 100+ 具名人物、50+ 地点和势力条目。内置互动角色采用原创或用户有权提供的角色资料，原作人物记录支持资料查询。

### 资料包出处编号

- `zun.th08.characters` 对应《东方永夜抄》原版人物手册。旧网页需按其日文编码读取。用于核实灵梦、魔理沙、八云紫等人的身份、居地和能力。 [ZUN 原版手册](https://www16.big.or.jp/~zun/html/th08man/html/chara.html)
- `ichijinsha.silent-sinner.characters` 和 `ichijinsha.silent-sinner.world` 对应一迅社的《东方儚月抄》介绍。用于核实永远亭位于迷途竹林内、红魔馆有地下图书馆，以及与这些地点有关的人物。神社的数量等随作品时期变化的说法按当时语境记录。 [人物](https://www.ichijinsha.co.jp/special/toho/character/index.html) · [世界](https://www.ichijinsha.co.jp/special/toho/world/index.html)
- `kadokawa.suzunaan.1` 和 `kadokawa.suzunaan.7` 对应书目中的角色和地点简介。 [第一卷](https://www.kadokawa.co.jp/product/321211000203/) · [第七卷馆藏书目](https://opac.kadcul.com/en/library/search/000081904?author%5Bini%5D=ZUN%EF%BD%9C%E5%8E%9F%E4%BD%9C&author_option=and&bookplate_display=hide&free_option=and&like_option=partial&page=2&publisher_option=and&sort=year_desc)
- `official.th06`、`official.th07`、`official.th08` 对应上表三部作品的官网简介。公开简介只支持故事概要，档案里的作品顺序保留为编目顺序。

## 接入方向

- 查明 `WorldBookEntry` 的可见性、检索和来源表达，以及世界包模板、摘要校验、预览、导入和原包保留能力。优先扩展共享数据结构，核对历史包行为。
- 对照既有能力登记和行动授权，检查资源、地点、时窗、技能、承诺等如何进入同一判定。不同行动系统可各自结算，同一套约束表达为上层剧情节点提供可核对的依据。
- 将世界模式、介入点、触发前提、已确认历史、原作预期走向及玩家实际结果分别建模。原作不详的日期以顺序、范围或事件条件表达。触发结果通过现有事件提交，由各分支独立重放。
- 用本地世界例子核对同一套规则可处理最小权限角色与高能力角色。普通居民可以疏散和筹措物资，能否干预灾变源头由可到达性、资源、权限及能力门槛共同决定。

## 首轮接入记录

- 世界书增加可选证据状态和原文定位。GM 只收到匹配的公开条目。既有无出处条目的序列化保持原形。
- 世界包可提供多份介入点初态。预览显示时间位置与可公开的先前事实。确认选择后启用对应的创世快照，重试同一请求时会核对介入点。时间位置可以按原作事件描述，开场世界时钟从该存档起算。
- 本地资料包 `0.1.0` 收录首批 13 名角色资料、8 个地点和 3 个异变资料条目，提供红雾出现后、满月异常夜两个开场。运行 `.runtime/venv313/Scripts/python.exe scripts/build-gensokyo-reference.py` 生成 ZIP。人物录入和世界地图还需继续扩充，后续剧情触发与能力判定仍需接入。
- 两个开场分别通过 SQLite 导入、创世回放及 2.0 备份恢复。红雾开场的 GM 检索无法取得后续满月异变条目。以「灵梦」「魔理沙」「八云紫」「迷途竹林」搜索均可取得带出处的记录。世界包和作者相关联合回归 87 项通过，前端类型检查与 lint 通过。《回灯之约》1.2 重新打包与此前归档的 SHA-256 一致。
