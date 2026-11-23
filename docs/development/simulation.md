# Simulation and NPC policy

## Public contract

`SimulationEngine.plan_window(state, plan, context) -> SimulationResolution` is a pure proposal API. It does not persist state or mutate the supplied `WorldState`. Root's job worker owns leases, commit fencing, cancellation, and event persistence.

`WorldAdvancePlan` specifies a reviewed starting tick, target tick and optional stopping conditions without a focal character. The local HTTP endpoint accepts a branch version, duration and model profile. The canonical world owner submits and controls the job under the same lease protocol as a long action. The player's character and all eligible residents participate in ordinary needs settlement. Event attribution uses the owner's account ID and leaves player control intact.

`LongActionPlan` identifies the actor, requested activity, target tick, constraints, and stop conditions. Registered activities include waiting, resting, eating, drinking, travel, paid work, study, production, and exact one-shot consumption. A paid-work plan requires a schedule tied to an active assignment. It can end on its deadline, a wage-account balance target, or affordability of a specified market listing. Standalone `socialize` still returns a rule-required interruption. Accepted constraints are `do_not_eat`, `do_not_drink`, and `do_not_rest`.

`SimulationContext` binds a proposal to `world_id`, `branch_id`, `command_id`, `world_seed`, `state_version`, and `ruleset_version`. Speed and depth use the public application enums (`fast|balanced|patient` and `standard|deep|exhaustive`). `max_step_ticks` is an explicit seconds cap. The result reports the selected profile values and exact ticks advanced. The worker uses the explicit generation seed when present, including zero. Legacy saves fall back to their stored simulation seed or world ID.

Decision requests and responses carry request ID, state version, tick, and an owner/source (`model` or `user`). A response for any other snapshot or source raises `StaleDecisionResponse`. User-owned succession choices are limited to the player's eligible membership authority, use deterministic option IDs, omit private evidence IDs, and filter hidden candidates the player does not know. An accepted choice is returned as a decision proposal and still requires RuleEngine evaluation before the worker can commit it. The simulator never converts model text directly into a world event.

## Event boundaries and deterministic replay

Each call advances one bounded interval. The next boundary considers the requested target, caller and policy step caps, scheduled events, lifespan expiry, stop conditions, and the completion time of atomic actions. Time-consuming behavior is sent through `world_engine.rules.RuleEngine`, which validates its typed event batch against the reducers before returning it. The simulator checks the returned clock contract without replaying that full batch; injected rule evaluators still receive full reducer replay.

`WorldAdvancePlan` has no focal character. It advances the player and other eligible characters in one registered background routine event, with a maximum 30-day interval per committed batch. The rule result reports an exact deprivation boundary if the last ration would make damage due at the interval endpoint; the worker then performs that same-tick resolution before planning further time. If stock runs out earlier, the simulator retries only through the reported boundary. Normal care, consumption lots, need decay and schedule handling remain reducer-validated.

Ordinary wait and rest are not clock-only jumps: their rule events update food, water, rest, health, and lifespan at the configured seconds-per-day rates. Eating and drinking consume FIFO inventory lots through matching holder accounts and consumption sinks. Production consumes the producer's owned input lots and spends its own finite output quota. The 1.7.0 content packs include a finite, 600-run `population.year_advance` schedule at exact world-year boundaries and a finite 30-day `environmental.harvest` schedule for food and water. Population occurrences advance typed age buckets for all 12 regional cohorts in one event. Monthly harvest issues only each registered policy's remaining quota into active, recipient-owned accounts and inventory lots. Sustained demographic mode enables one finite named-seat policy per region; vacancy refill draws from the age-eligible anonymous cohorts and is funded by swapping the dead seat's existing harvest recipients. Natural mode omits these policies. When an unsupported scheduled handler or an expiry requiring follow-up reaches its boundary, the window stops there for worker resolution.

The planner checks scheduled and lifespan boundaries before committing an atomic action. If the available interval cannot fit an atomic action, it leaves the action uncommitted. Durable travel represents partial progress through a registered journey, so scheduled and lifespan events can resolve while the character is in transit. Multi-leg travel continues across routine settlement and pauses at an actionable route choice.

Simulation event IDs are deterministic UUID5 values scoped by command ID, world seed, state version, tick, and action boundary. A retry against the same state and input therefore proposes the same batch. After commit, the worker must reload state and plan from the new version; a model decision from an earlier version is stale.

## NPC policy and knowledge

`AgentPolicy` ranks only ordinary, low-impact self-care actions against the authorized plan. It can select registered eating or resting rules when the actor's typed need reaches its critical threshold. If critical food need has no owned food lot, it returns a structured `npc.survival.food_shortage` request with bounded choices. Equal top-priority personal goals interrupt with the actor's own goal IDs and titles; the policy does not choose between them arbitrarily.

`build_agent_knowledge` constructs a character-scoped snapshot. It includes knowledge held or owned by that character, public records, explicitly shared records, that character's active goals, and public-facing fields of local visible entities. It does not include another character's private knowledge or hidden global event history. The 1.7.0 content packs each seed one authored hidden historical site near the opening settlement. A successful history search creates a private observation and unlocks the short site route only for that discoverer; the hidden flag remains set for other characters. Query memory is not inferred from the world's full event log.

Knowledge acquisition and social decisions still flow through typed domain/rule events. Messages are not broadcast by this layer. Delayed delivery requires a registered queue/channel event before a recipient can learn the message.

## Implemented slice and limits

The current slice has tested pure wait, rest, food consumption, route movement integration, skill training, recipe production, goal-conflict decisions, decision freshness, stop conditions, scheduled boundaries, population-year progression, and exact lifespan death events. `RuleEngine` emits one typed `core.life_ended.v1` event for each character expiring at the boundary. NPC-only expiry returns an advanced window at that same tick so the next lease window can process succession; a dead plan actor stops after cleanup and is never asked to act again. At the next state version, a dead leader or office holder produces a fresh `organization.succession` decision request with living members and vacancy as choices. User-owned succession requests appear when the player has an elected/council membership or explicit appointment authority; other political choices are model-owned. The accepted choice becomes a CASed `core.governance_succession.v1` event with the prior death event as causation. Already-overdue lifespan records are closed at the current tick so a stale active character cannot keep acting. Registered quota renewal and population-year schedules resolve through the RuleEngine; a scheduled phase earlier than lifespan resolution runs first at coincident ticks. Unknown scheduled handlers pause at their due tick.

Annual anonymous population cohorts have deterministic aging, births, and deaths. Sustained mode promotes eligible adults from those cohorts into vacant named seats at the next eligible monthly harvest. The modern 1.7 and 1.8 acceptance runs each fell to 237 active non-key residents at a lifespan commit in year 13. The 1.7 harvest replaced the three residents 25 world days later. A 600-year capacity pass requires checking every full commit from genesis through the exact target, including the separation between death and refill. See the [acceptance matrix](acceptance-matrix.md) for the verified intervals and counts. Named residents still lack a general autonomous round-robin, migration and private-message delivery system.

For the next sustained pack revision, add two explicit acceptance cases. Ordinary turnover should be supportable from a roster with enough staffed seats and food capacity to absorb concurrent vacancies. Early replacement, where enabled, should promote an existing eligible resident in the same transaction as the vacancy and transfer finite supplies to cover the remainder of the harvest interval. The transition must preserve the deceased character and account for both people in the regional census. No eligible adult or exhausted supplies can still leave a recorded vacancy. Existing runs keep their original rules and historical minima. A fresh run under any revised policy must pass the same per-commit audit.

Speed and depth are accepted and retained independently in each resolution. They do not yet change the number of LLM candidates or simulation detail; the explicit `max_step_ticks` and policy cap are the controls that currently affect window size. This avoids implying speed/depth effects that are not implemented.

## Testing

`tests/agents` covers private-memory isolation and ordinary food prioritization. `tests/simulation` exercises deterministic event batches, pure input behavior, need-threshold stops, scheduled/lifespan boundaries (including simultaneous NPC expiries, same-tick phase ordering, and the following succession decision gate), overdue-life catch-up, decision freshness/source checks, typed succession replay, eating, training, and finite-quota production. `tests/api/test_simulation_jobs.py` verifies worker commits and event replay. Rule-unit tests live in `tests/rules` and are separate from these simulation contract tests.

## Implementation references

The ordering and stop-before-boundary design follows event-scheduling guidance from [SimPy's time and scheduling guide](https://simpy.readthedocs.io/en/stable/topical_guides/time_and_scheduling.html) and its [simulation core](https://github.com/simpx/simpy/blob/master/src/simpy/core.py). The policy/event separation and explicit model update lifecycle are informed by Mesa's [model API](https://github.com/mesa/mesa/blob/main/mesa/model.py). These projects are architectural references; their APIs or behaviors are not embedded in this engine.

## Scheduled NPC employment

The world-advance worker opens a shift for an uncontrolled NPC who holds an active assignment, is at its workplace and has a matching daily work window. The attendance record binds the assignment revision, workplace and scheduled interval. At the scheduled end tick, the reducer verifies attendance and recomputes the wage using the existing account and balance rules. A death, departure or loss of the assignment before settlement cancels unpaid attendance. Insufficient funds also close the attendance without a wage. The occurrence identity and settlement checks prevent duplicate payment across retries, split windows and replays.

Background care continues through work periods and resolves at its true time boundaries. A world-advance window can aggregate routine processing up to the next known material boundary. Completed attendance and payment must be recalculable from authoritative state before committing. Only NPCs with a live assignment, matching work window and registered payable terms create work boundaries. This keeps inactive and unassigned schedules from multiplying the cost of a 600-year run.

The worker includes eligible shift starts, interruptions and end ticks in its boundary calculation, including when a boundary equals the requested target. Ordinary person-specific actions retain their existing time flow. Autonomous job search, hiring and travel to the workplace remain outside this path.

## Durable travel across world events

The Nine Bridges Vale test uses a registered day-long road whose travel interval contains another character's deprivation settlement. The current atomic movement event can record the completed trip. It has no state for a character already on the route when the world processes a different event. A leased travel job must report arrival only after an actual movement or arrival event.

Leased travel saves a departure with the route, endpoints, departure tick and planned arrival tick. A pending journey remains in the branch event stream, with a single scheduled resolution, and the traveller has no local position while in transit. Subsequent windows settle ordinary background activities and scheduled events before the exact arrival or hazard tick. Recovery and forks retain the committed departure and schedule. A paused job may resume the same journey. Cancelling a job stops that job's advancement and the journey stays on the branch. An unavailable destination pauses its resolution for later handling. The atomic movement representation remains available for ordinary movement and replay of existing saves.

The separation between a traveller's waiting process and other processes that may wake or interrupt it follows SimPy's discrete-event process model. [SimPy process interaction](https://simpy.readthedocs.io/en/latest/topical_guides/process_interaction.html)

## 聊天与面板的持续工作验收

聊天可以沿用当前角色已保存的工作时段，也可以按玩家明确指定的首班和间隔安排。长行动面板填写同一岗位、首班、间隔和时限后，使用相同的工作计划校验及持久化作业。两个入口都查验当前工作地点、任职与岗位有效期，拒绝重叠班次，并只从可见的挂牌与存档里解析储蓄目标。排队时，世界时间、工资和库存保持原值。

在 Windows 的项目 Python 3.13 环境运行以下检查。用例经过聊天和长行动 HTTP 入口、隔离 SQLite 作业、暂停与重新领取租约，以及从创建快照开始的事件回放。聊天意图使用结构化测试输出，避免将此检查误记为真实模型验收。

```powershell
.\.runtime\venv313\Scripts\python.exe -m pytest -q tests/api/test_work_chat_contract.py -k 'a_month_of_work or reject_work_from_away'
```

两条路径都结算 30 笔工资，首班结束同时处理 NPC 寿终，窗口截止同时处理生产排程。暂停后由新工作器续跑。结薪日期、余额、商品库存、排程状态和回放哈希均有断言，同一请求重试可取回原作业。普通动作与模拟的衔接及真实模型的自然语言验收仍需单独验证。
