# 状态效果战斗演练素材

这一组素材用仓库现有的状态效果内核（`effects/`）演示四种战斗节奏与一个
反例。素材本身是可编辑工程，不是预录视频：画面里的伤害/回血数字由
`effects.kernel` 的规则在回放时算出。

## 目录

```
drills/source/*.json   可编辑的源素材：角色、事件、相机标记、说明
drills/out/*.json      生成产物：命令序列 + 回放帧 + 期望生命曲线
drills/playlist.json   播放清单：每段的素材引用、sha256、观察点
tools/generate_drills.py  生成器（读取源素材，经现有运行器执行）
tools/curve.py            独立生命曲线计算器（第二实现）
tools/play_drills.py      播放器（把素材装入现有运行器回放）
tools/verify_drills.py    完整性校验 / 清空重建
```

## 用法

```bash
# 生成素材并写播放清单
python3 tools/generate_drills.py

# 回放全部（或指定 id）
python3 tools/play_drills.py
python3 tools/play_drills.py fast_stacking
python3 tools/play_drills.py --list

# 校验引用完整性；--rebuild 会先清空 drills/out 与播放清单再重建
python3 tools/verify_drills.py
python3 tools/verify_drills.py --rebuild

# 全部测试
python3 -m unittest discover -s tests
```

## 五段演练

| id | 节奏 | 观察的状态关系 |
| --- | --- | --- |
| `low_frequency_high_intensity` | 低频高强度 | bleed 每 4 拍一跳；暂停不补发跳伤 |
| `fast_stacking` | 快速叠层 | 同源 STACK 叠强度、异源独立实例 |
| `mutual_suppression` | 互相抑制 | freeze 挂起 burn 整段窗口，解除后剩余期原样恢复 |
| `source_revocation` | 来源撤销 | STACK 随源移除；REFRESH 保留并自然到期 |
| `illegal_trigger_cycle` | 反例 | burn→burn 触发环被拒绝，随后正常命令继续 |

每段素材的 `observe` 字段列出该段要观察的关系，播放器会先打印出来。

## 确定性

- 每段素材的 `seed` 只用于把推进步切成 1~2 段；命令内容与相机时点不变。
- 相同种子重复生成得到逐字节相同的 `drills/out/*.json` 与播放清单
  （见 `tests/test_drills.py::test_rebuild_is_byte_identical`）。
- 内核输入里没有任何随机量。

## 期望生命曲线

`tools/curve.py` 是独立第二实现：它不复用内核的实例对象、hp 状态或事件流，
只依据效果规格与命令表重算每拍生命。生成器把它和运行器逐拍交叉核对，
不一致就报出具体演练 id 与效果配置（name/target/source）。期望曲线不会
写回角色状态。

## 反例

`illegal_trigger_cycle` 在素材里用 `define_transform` 声明 `burn→burn`
自转换，再申请一次 burn。内核沿关系边迭代到 `MAX_REACTION_DEPTH` 后抛出
`RecursionError`，事件流留下 `reaction_chain_terminated` 与完整关系链。
运行器（`effects.replay.run_drill`）只对标注
`expect: reject_reaction_chain` 的命令吞掉该异常并继续执行后续命令，因此
播放不会中断。

## 完整性

`verify_drills.py --rebuild` 删除 `drills/out/` 与 `drills/playlist.json`
后从 `drills/source/` 重建并校验：

- 清单引用的素材存在且 sha256 匹配；
- 重放得到的帧生命与独立曲线一致；
- 反例确实被拒绝、关系链长度等于上限、且拒绝后仍有推进；
- 每个源素材都在播放清单中。

缺少引用会定位到具体演练 id 与素材路径；效果配置被改动会定位到具体
演练与 `apply` 命令。
