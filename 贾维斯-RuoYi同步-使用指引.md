# 贾维斯 → RuoYi 同步表 · RuoYi 侧使用指引

> 版本：v1.0（2026-07-19）· 任务 T7 产出物
> 上游文档：`Vibe-Trading/贾维斯-RuoYi同步-开发计划.md`（v1.0，表结构以其 §2 为准；T1 定稿如有微调以 T1 回执为准）
> 适用环境（均经实地核对）：RuoYi-Vue **3.9.2**（JDK17 / Spring Boot 4.0.6）+ 前端 **Vue3 + Element Plus + Vite**（`ruoyi-ui/package.json`：vue 3.5.26、element-plus 2.13.1）+ MySQL 8.4 库 **`jiaweisi`**（`application-druid.yml:9`）
> 前提：T1 初始化脚本已在 `jiaweisi` 库执行完毕（13 张 `jarvis_*` 表 + `jarvis_direction`/`jarvis_plan_status` 字典 SQL）。

---

## 0. 快速开始（TL;DR）

1. 建"贾维斯监控"菜单目录（§1.5 的 SQL，一次性）。
2. 登录 RuoYi（admin）→ **系统工具 → 代码生成** → 点【导入】→ 勾选 13 张 `jarvis_*` 表 → 确定。
3. 逐表点【编辑】：改**生成业务名**（§1.4 总表，规避重名）、绑字典（§2）、上级菜单选"贾维斯监控"→ 保存。
4. 逐表【预览】确认 →【生成代码】下载 zip → 按 §1.6 放入工程 → 执行裁剪后的菜单 SQL（§3.4）→ 重启后端 + 前端。
5. 按 §3 删写接口/写按钮做成只读页；按 §4 配归档任务；按 §5 配心跳监控。

---

## 1. 代码生成器导入 13 张 jarvis_* 表

### 1.1 菜单入口与前置确认

- 菜单路径：**系统工具 → 代码生成**（初始化菜单数据：一级目录"系统工具" `sql/ry_20260417.sql:164`，"代码生成"菜单指向 `tool/gen/index`，权限标识 `tool:gen:list`，同文件 `:183`）。
- 操作账号需具备 `tool:gen:import`（导入）、`tool:gen:edit`（编辑）、`tool:gen:code`（生成）权限；admin 默认全有。
- 前置确认：13 张表已存在于 `jiaweisi` 库。代码生成器只扫**当前数据源所连的库**（RuoYi 连的就是 `jiaweisi`，见 `ruoyi-admin/src/main/resources/application-druid.yml:9`）。可先用任意客户端跑 `SHOW TABLES LIKE 'jarvis_%';` 应返回 13 行。

### 1.2 导入操作步骤

1. 代码生成列表页点击【导入】按钮，弹出"导入表"对话框（`ruoyi-ui/src/views/tool/gen/importTable.vue`）。
2. 表名称输入 `jarvis` 搜索，列表显示所有未导入的 `jarvis_*` 表（后端 `GET /tool/gen/db/list` 自动排除已导入表与 `qrtz_`/`gen_` 前缀表，`jarvis_` 前缀不受影响）。
3. 逐行勾选 13 张表（点行即选中），点【确 定】。
4. 前端类型无需选择：3.9.2 导入时固定按 **Vue3 Element Plus 模版**提交（`importTable.vue:114`：`importTable({ tables, tplWebType: 'element-plus' })`），与本项目 `ruoyi-ui`（Vue3 + Element Plus）正好匹配。**不要**在后续"生成信息"里改回 Vue2 模板，前端会无法直接使用。
5. 导入动作对应后端 `POST /tool/gen/importTable`（`GenController.java:113-116`，权限 `tool:gen:import`）。导入只是把表元数据抄进 `gen_table`/`gen_table_column`，**不产生任何代码**，可反复删除重导。

### 1.3 类名前缀：`autoRemovePre=false` 现状、利弊与改法

现状配置（`ruoyi-generator/src/main/resources/generator.yml`）：

```yaml
gen:
  author: ruoyi
  packageName: com.ruoyi.system
  autoRemovePre: false      # 不自动去表前缀
  tablePrefix: sys_         # 仅 autoRemovePre=true 时生效
  allowOverwrite: false
```

类名生成逻辑（`GenUtils.convertClassName`，`ruoyi-generator/.../util/GenUtils.java:177-187`）：`autoRemovePre=false` 时表名整体转驼峰，所以 `jarvis_signal_state` → 实体类 **`JarvisSignalState`**，类名统一带 `Jarvis` 前缀。

| 方案 | 利 | 弊 |
|---|---|---|
| **保持 false（推荐）** | 13 个类一眼可辨"贾维斯镜像域"；`Position`、`Snapshot`、`Outcome` 这类通用词不裸奔，避免与工程既有/未来类撞名 | 类名略长（如 `JarvisIntradayPredictionController`） |
| 改为 true + 前缀加 `jarvis_` | 类名短（`SignalState`） | 通用词类名撞名风险；且 `tablePrefix` 是全局配置，影响以后所有表的导入行为 |

如确要去前缀的改法：

1. 改 `generator.yml`：`autoRemovePre: true`、`tablePrefix: sys_,jarvis_`（逗号分隔，`GenUtils.java:181-185` 按列表逐个尝试剥前缀）。
2. 重启后端（GenConfig 是启动时装配的配置类）。
3. **重新导入**这 13 张表才生效——类名是导入时算好存进 `gen_table` 的（`GenUtils.initTable:23`），已导入的记录不会自动改。已导入的表也可不重导，直接在【编辑 → 基本信息 → 实体类名称】逐表手工改（`basicInfoForm.vue:15-16`）。

注意：**`tablePrefix` 只影响类名，不影响"生成业务名"**——业务名另有生成规则且有坑，见下节。

### 1.4 逐表推荐配置总表（重要：规避业务名冲突）

业务名生成逻辑（`GenUtils.getBusinessName`，`GenUtils.java:164-169`）：取表名**最后一个下划线之后**的片段。这导致多张 jarvis 表的默认业务名**重复或语义含糊**：

- `jarvis_signal_state` 和 `jarvis_sync_state` 默认都是 `state` → 前端页面目录、后端 `@RequestMapping("/jarvis/state")` **直接冲突**；
- `jarvis_market_snapshot` 和 `jarvis_snapshot` 默认都是 `snapshot` → 同样冲突；
- `jarvis_force_order_min` 默认 `min`、`jarvis_limit_order` 默认 `order`，语义太弱。

**每张表导入后必须在【编辑 → 生成信息】里改"生成业务名"**，推荐配置如下（生成模板一律"单表（增删改查）"、生成包路径统一 `com.ruoyi.jarvis`、生成模块名统一 `jarvis`）：

| # | 表 | 实体类（默认，建议保留） | 生成业务名（必改） | 生成功能名（建议） | 优先级 |
|---|---|---|---|---|---|
| 1 | jarvis_signal_state | JarvisSignalState | signalState | 信号当前态 | P0 |
| 2 | jarvis_signal_change | JarvisSignalChange | signalChange | 信号变更流水 | P0 |
| 3 | jarvis_reco_plan | JarvisRecoPlan | recoPlan | 推荐点位 | P0 |
| 4 | jarvis_intraday_prediction | JarvisIntradayPrediction | intradayPrediction | 4小时预测 | P0 |
| 5 | jarvis_tape_bar | JarvisTapeBar | tapeBar | 盘口分钟聚合 | P0 |
| 6 | jarvis_force_order_min | JarvisForceOrderMin | forceOrderMin | 强平分钟聚合 | P0 |
| 7 | jarvis_market_snapshot | JarvisMarketSnapshot | marketSnapshot | 市场情报快照 | P0 |
| 8 | jarvis_snapshot | JarvisSnapshot | dailySnapshot | 每日决策快照 | P1 |
| 9 | jarvis_outcome | JarvisOutcome | outcome | 前向收益 | P1 |
| 10 | jarvis_position | JarvisPosition | position | 模拟仓位 | P1 |
| 11 | jarvis_limit_order | JarvisLimitOrder | limitOrder | 限价挂单 | P1 |
| 12 | jarvis_tape_flow_snap | JarvisTapeFlowSnap | tapeFlowSnap | 盘口主体画像 | P2（二期建） |
| 13 | jarvis_sync_state | JarvisSyncState | syncState | 同步心跳 | P0 |

说明：

- **生成功能名**默认取表注释全文（`GenUtils.initTable:27`），13 张表注释都较长（如"贾维斯推荐点位（十二系统共识交易计划快照）"），会被用于页面标题和按钮文案，建议按上表改短。
- **生成包路径**改成 `com.ruoyi.jarvis` 后，"生成模块名"**不会自动联动**（导入时按全局配置算好了，`GenUtils.initTable:25`），要手工改成 `jarvis`。模块名决定 Controller 路由 `/jarvis/signalState` 与前端目录 `views/jarvis/signalState/`。
- 生成信息 tab 中其它项：表单布局随意（只读页用不到）、"生成代码方式"选 **zip压缩包**（`allowOverwrite=false` 下自定义路径直接写盘会被拒，`GenController.genCode:218` 有校验）、勾选"生成详情页"（只读场景详情弹窗很有用）。
- P2 表 `jarvis_tape_flow_snap` 二期才建表，届时再导入即可，本表先列出配置备查。

### 1.5 建议先建"贾维斯监控"菜单目录

生成信息里的"上级菜单"建议统一挂到独立目录，避免散落。先执行：

```sql
-- 一级目录：贾维斯监控（menu_id 自增，无需指定）
insert into sys_menu (menu_name, parent_id, order_num, path, component, is_frame, is_cache, menu_type, visible, status, perms, icon, create_by, create_time, remark)
values ('贾维斯监控', 0, 5, 'jarvis', null, 1, 0, 'M', '0', '0', '', 'monitor', 'admin', sysdate(), '贾维斯行情数据镜像监控目录');
```

然后在每张表的【编辑 → 生成信息 → 上级菜单】树选择器里选"贾维斯监控"（`genInfoForm.vue:110-120`）。

### 1.6 生成、代码放置与重启

1. 列表页勾选已配置好的表 →【生成代码】（或逐表操作列点生成），浏览器下载 `ruoyi.zip`（后端 `GET /tool/gen/batchGenCode`，`GenController.java:241-247`）。
2. 解压 zip，按目录对应放入工程（以 zip 内实际结构为准）：

| zip 内路径 | 放入位置 |
|---|---|
| `main/java/com/ruoyi/jarvis/**`（domain/mapper/service/controller） | `ruoyi-system/src/main/java/com/ruoyi/jarvis/`（放 ruoyi-system 模块即可被 ruoyi-admin 依赖到） |
| `main/resources/mapper/jarvis/*.xml` | `ruoyi-system/src/main/resources/mapper/jarvis/` |
| `vue/api/jarvis/*.js` | `ruoyi-ui/src/api/jarvis/` |
| `vue/views/jarvis/**/index.vue` | `ruoyi-ui/src/views/jarvis/…/` |
| `*Menu.sql` | 不要直接全跑，**先按 §3.4 裁剪**再执行 |

  MyBatis 扫描配置无需动：`typeAliasesPackage: com.ruoyi.**.domain`、`mapperLocations: classpath*:mapper/**/*Mapper.xml`（`application.yml:107,109`）通配已覆盖 `com.ruoyi.jarvis.domain` 与 `mapper/jarvis/`。
3. 重启后端；前端开发模式 `npm run dev` 热更新即可，生产则重新 `npm run build:prod`。
4. 系统管理 → 角色管理：给目标角色勾选"贾维斯监控"下的新菜单（只勾**查询/导出**权限，见 §3）。

---

## 2. 字典绑定（枚举列下拉显示）

### 2.1 前提

T1 脚本已初始化两个业务字典（字典类型与键值以 T1 定稿为准）：

- `jarvis_direction`：方向类枚举（bullish 看多 / bearish 看空 / neutral 中性 / long 多 / short 空 / up 涨 / down 跌 / sideways 震荡 等值域并集）；
- `jarvis_plan_status`：计划状态（ok 可执行 / watch 观望 / neutral 中性）。

可在 **系统管理 → 字典管理**（菜单 `system/dict`，`sql/ry_20260417.sql:172`）里确认两个字典类型存在且有数据。若 T1 将 direction 域拆成了多个字典（如单独 `jarvis_side`），按其回执对应替换下表。

### 2.2 生成界面绑定步骤

1. 代码生成列表页 → 目标表【编辑】→ **字段信息** tab。
2. 找到枚举列，将"显示类型"改为**下拉框**，"字典类型"列的下拉里选择对应字典（`editTable.vue:95-104`：字典下拉直接列出 `sys_dict_type` 全部类型，含刚初始化的 `jarvis_*` 字典）。
3. 保存后**重新生成代码并覆盖对应文件**才生效——字典绑定影响的是生成产物：列表页用 `<dict-tag>` 回显、查询区生成字典下拉（生成模板逻辑见 `vm/vue/index.vue.vm:23-33`，v3 模板同理）。

### 2.3 建议绑定关系总表

| 表 | 列 | 字典 | 说明 |
|---|---|---|---|
| jarvis_signal_state | direction | jarvis_direction | 信号方向 |
| jarvis_signal_change | prev_direction、new_direction | jarvis_direction | 变更前后方向 |
| jarvis_reco_plan | direction | jarvis_direction | 共识方向 |
| jarvis_reco_plan | side | jarvis_direction（或 T1 的 jarvis_side） | long/short |
| jarvis_reco_plan | plan_status | jarvis_plan_status | ok/watch/neutral |
| jarvis_intraday_prediction | direction | jarvis_direction | up/down/sideways |
| jarvis_market_snapshot | sentiment_bias | jarvis_direction | bullish/bearish/neutral |

`tradeable`、`hit` 这类 `TINYINT 0/1` 列：RuoYi 自带 `sys_yes_no` 字典是 Y/N 值域，**不匹配** 0/1，别直接绑。要么让 T1 补一个 `jarvis_yes_no`（0=否 1=是）字典再绑，要么不绑字典、列表页原样显示 0/1。

---

## 3. 只读页面生成要点（镜像表只查不改）

镜像表数据修正的唯一途径是同步器重推，RuoYi 侧必须只读（上游方案 §2.6 单向性声明）。代码生成器模板固定会生成增删改，需生成后做减法。建议**三层防线全做**：

### 3.1 三层防线总览

| 层 | 动作 | 拦谁 | 说明 |
|---|---|---|---|
| 权限层 | 菜单 SQL 只插"查询/导出"按钮；角色只勾这两个 | 普通角色 | 最快，但 **admin 是超管，`@ss.hasPermi` 对其恒真，挡不住 admin** |
| 前端层 | 删新增/修改/删除按钮与弹窗 | 所有人的误操作入口 | 界面干净，防手滑 |
| 后端层 | 删 Controller 写接口 | 一切调用方（含直接 curl） | **最彻底，必做** |

### 3.2 后端：删 Controller 写接口

生成的 `JarvisXxxController.java` 固定含 6 个方法（模板 `vm/java/controller.java.vm`）。删掉三个写方法，保留三个读方法：

| 方法 | 注解 | 模板行号 | 处置 |
|---|---|---|---|
| `list` | `@GetMapping("/list")` | :43-51 | 保留 |
| `export` | `@PostMapping("/export")` | :63-71 | 保留（Excel 导出是查询语义） |
| `getInfo` | `@GetMapping("/{id}")` | :76-81 | 保留 |
| `add` | `@PostMapping` | :86-92 | **删除** |
| `edit` | `@PutMapping` | :97-103 | **删除** |
| `remove` | `@DeleteMapping("/{ids}")` | :108-114 | **删除** |

同时删除文件头部随之失效的 import（`PostMapping` 保留给 export 用，`PutMapping`/`DeleteMapping`/`RequestBody` 可删，删完以 IDE 无告警为准）。

可选的彻底清理：`IJarvisXxxService` / `JarvisXxxServiceImpl` / `JarvisXxxMapper`（接口 + XML）里的 `insertXxx`/`updateXxx`/`deleteXxx` 方法与对应 `<insert>`/`<update>`/`<delete>` 节点一并删除。不删也无碍（没有 Controller 入口就不可达），删了更清爽、审计更干净。

### 3.3 前端：删写操作按钮与函数

生成的 `views/jarvis/xxx/index.vue`（v3 模板 `vm/vue/v3/index.vue.vm` 的产物）需删：

1. **工具栏按钮**（模板 :70-105 生成物）：新增（`v-hasPermi="[':add']"`）、修改（`:edit`）、删除（`:remove`）三个 `<el-col>` 块，**保留导出**。
2. **操作列按钮**（模板 :152-155 生成物）：行内"修改""删除"按钮，保留"详情"（勾了生成详情页才有）。
3. **脚本区函数**（模板 :527 起生成物）：`handleAdd`、`handleUpdate`、`handleDelete`、`submitForm`、`cancel`、`reset` 及新增/修改共用的 `<el-dialog>` 表单块。
4. **api 文件** `src/api/jarvis/xxx.js`：删 `addXxx`、`updateXxx`、`delXxx` 三个导出函数及 index.vue 中对应 import。

改动量不大但重复 13 次，建议改完一张表后把该表的 index.vue 当模板对照其余表。前端跑 `npm run dev` 无编译告警、页面无残留按钮即完成。

### 3.4 菜单 SQL 裁剪

生成 zip 里的 `*Menu.sql`（模板 `vm/sql/sql.vm`）固定插 1 条菜单 + 5 条按钮（查询/新增/修改/删除/导出）。只执行其中三段：

- 主菜单 INSERT（含 `SELECT @parentId := LAST_INSERT_ID();` 这行必须跟着跑）；
- "查询"按钮 INSERT（`…:query`）；
- "导出"按钮 INSERT（`…:export`）。

**跳过**"新增/修改/删除"三条 INSERT（`…:add`/`…:edit`/`…:remove`）。权限标识都没入库，就永远不会被勾给任何角色。

如果生成时"上级菜单"已选了"贾维斯监控"，主菜单 INSERT 里的 `parent_id` 会带对（模板变量 `${parentMenuId}`）；若忘了选，手工把 SQL 里 parent_id 改成目录的 menu_id。

### 3.5 权限兜底

- 角色管理里给业务角色只勾"贾维斯监控"下各菜单 + 查询 + 导出。
- 建议按上游方案 §4.5 建**"监控访客"只读角色**：仅授贾维斯菜单查询权限，不给任何 `sys_*` 管理菜单，供对外/移动端账号使用。
- 做完 §3.2 后端删除后，即使某天误勾了 add 权限也没有接口可调，这是只读的最终保证。

---

## 4. quartz 归档任务（按 retention_days 清理过期数据）

三张高增速表需按保留期清理（保留天数与同步器 `sync_config.json` 的 `retention_days` 约定一致，开发计划 §4）：`jarvis_signal_change` 180 天、`jarvis_tape_bar` 90 天、`jarvis_market_snapshot` 90 天。清理跑在 RuoYi quartz 侧，用 RuoYi 应用自身的数据库连接（root），与同步账号隔离——`jarvis_sync` 账号本就无 DELETE 权限（上游方案 §4.1）。

### 4.1 白名单硬约束：Bean 必须放 `com.ruoyi.quartz.task` 包

RuoYi 定时任务对调用目标做双重校验（`SysJobController.java:101-107` 新增/修改时检查）：

- 违规串黑名单 `Constants.JOB_ERROR_STR`（`Constants.java:171-172`）：invokeTarget 不得含 `org.springframework`、`java.net.URL` 等子串；
- 包白名单 `Constants.JOB_WHITELIST_STR = { "com.ruoyi.quartz.task" }`（`Constants.java:166`）：按 Bean 名调用时会实际取出 Bean 校验其**所在包**（`ScheduleUtils.whiteList:129-140`）。

所以任务类**必须**创建为：`ruoyi-quartz/src/main/java/com/ruoyi/quartz/task/JarvisCleanTask.java`，与自带示例 `RyTask.java`（`@Component("ryTask")`）同目录同模式。放错包，保存任务时直接报"目标字符串不在白名单内"。

### 4.2 JarvisCleanTask 代码样例

```java
package com.ruoyi.quartz.task;

import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * 贾维斯镜像表过期数据归档清理
 * invoke_target 配置：jarvisCleanTask.cleanAll()
 */
@Component("jarvisCleanTask")
public class JarvisCleanTask
{
    private static final Logger log = LoggerFactory.getLogger(JarvisCleanTask.class);

    /** 表名 -> 保留天数（与同步器 sync_config.json 的 retention_days 保持一致） */
    private static final Map<String, Integer> RETENTION = Map.of(
        "jarvis_signal_change",   180,
        "jarvis_tape_bar",        90,
        "jarvis_market_snapshot", 90);

    /** 表名 -> 过期判定用的业务时间列（用业务时间而非镜像落库时间） */
    private static final Map<String, String> TIME_COLUMN = Map.of(
        "jarvis_signal_change",   "signal_time",
        "jarvis_tape_bar",        "minute_time",
        "jarvis_market_snapshot", "snap_time");

    /** 单批删除行数上限：分批小事务，避免长事务大锁 */
    private static final int BATCH_LIMIT = 5000;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    /** 清理全部镜像表（无参方法，便于 sys_job 配置） */
    public void cleanAll()
    {
        RETENTION.forEach((table, days) -> clean(table, days));
    }

    /** 单表分批清理；表名只认白名单常量，杜绝注入面 */
    public void clean(String table, Integer retentionDays)
    {
        String timeColumn = TIME_COLUMN.get(table);
        if (timeColumn == null || !RETENTION.containsKey(table))
        {
            log.warn("[jarvisClean] 表 {} 不在清理白名单，跳过", table);
            return;
        }
        String sql = String.format(
            "DELETE FROM %s WHERE %s < DATE_SUB(NOW(), INTERVAL ? DAY) LIMIT %d",
            table, timeColumn, BATCH_LIMIT);
        long total = 0;
        int affected;
        do
        {
            affected = jdbcTemplate.update(sql, retentionDays);
            total += affected;
        }
        while (affected == BATCH_LIMIT);
        log.info("[jarvisClean] {} 清理 {} 行（保留 {} 天）", table, total, retentionDays);
    }
}
```

说明：`JdbcTemplate` 由 Spring Boot 基于既有 Druid 数据源自动装配，RuoYi 工程内开箱可注入；若你的分支禁用了它，可改为注入 `DataSource` 后 `new JdbcTemplate(dataSource)`。

### 4.3 sys_job 配置 SQL

`sys_job` 表结构与自带示例见 `sql/ry_20260417.sql:582-602`。推荐直接插一条（初始**暂停**态，验证后再启用）：

```sql
insert into sys_job (job_name, job_group, invoke_target, cron_expression, misfire_policy, concurrent, status, create_by, create_time, remark)
values ('贾维斯镜像表归档清理', 'DEFAULT', 'jarvisCleanTask.cleanAll()', '0 30 4 * * ?', '3', '1', '1', 'admin', sysdate(),
        '按 retention_days 清理 jarvis_signal_change(180d)/jarvis_tape_bar(90d)/jarvis_market_snapshot(90d)');
```

字段口径（列注释见 `sys_job` DDL）：`misfire_policy='3'` 错过放弃执行、`concurrent='1'` 禁止并发、`status='1'` 暂停（0 正常）。cron `0 30 4 * * ?` = 每天 04:30 低峰执行。

不想跑 SQL 也可界面配：**系统监控 → 定时任务**（菜单 `monitor/job`，`sql/ry_20260417.sql:177`）→ 新增，调用目标字符串填 `jarvisCleanTask.cleanAll()`，其余同上。

### 4.4 验证步骤

1. 部署 `JarvisCleanTask` 并重启后端。
2. 定时任务页面找到该任务 → 操作列【执行一次】。
3. 看两处结果：后端日志 `[jarvisClean] jarvis_signal_change 清理 N 行…`；**系统监控 → 定时任务 → 调度日志**里状态应为"正常"。
4. 库里抽查：`SELECT MIN(signal_time) FROM jarvis_signal_change;` 应不早于 180 天前（表里本就没有过期数据时删 0 行，也算通过）。
5. 验证通过后把任务状态改为"正常"，启用调度。

---

## 5. 同步心跳监控

### 5.1 数据源：jarvis_sync_state 表

同步器每轮 upsert 一行/表（表结构见上游方案 §2.1）：`table_name`（PK）、`cursor_value`、`last_run_at`、`last_ok_at`、`last_error`、`rows_total`、`lag_seconds`（同步器自算的"现在 − 已同步的最大业务时间"）、`update_time`（每次 upsert 自动刷新）。

监控看两个互补信号：

- **`lag_seconds` 大** = 链路通但数据旧（源侧没新数据，或同步组积压）；
- **`update_time` 长时间不动** = 同步器进程本身停了（此时 `lag_seconds` 是旧值，不可信，**要先看这个**）。

### 5.2 延迟告警查询 SQL

告警阈值按频率组区分（开发计划 §3.3 分组、§6 验证标准）：fast 组 <15s、mid 组 <90s、api 组 <400s（P95）。心跳停跳判定取 api 组最长周期 300s 的两倍 = 600s。

```sql
SELECT
  table_name,
  lag_seconds,
  TIMESTAMPDIFF(SECOND, update_time, NOW()) AS heartbeat_age_s,
  CASE
    WHEN TIMESTAMPDIFF(SECOND, update_time, NOW()) > 600 THEN 'DOWN-同步器停跳'
    WHEN table_name IN ('jarvis_signal_state','jarvis_signal_change')
         AND lag_seconds > 15  THEN 'ALERT-fast组超阈'
    WHEN table_name IN ('jarvis_tape_bar','jarvis_intraday_prediction','jarvis_force_order_min',
                        'jarvis_snapshot','jarvis_outcome','jarvis_position','jarvis_limit_order')
         AND lag_seconds > 90  THEN 'ALERT-mid组超阈'
    WHEN table_name IN ('jarvis_reco_plan','jarvis_market_snapshot')
         AND lag_seconds > 400 THEN 'ALERT-api组超阈'
    ELSE 'OK'
  END AS health,
  last_ok_at,
  rows_total,
  last_error
FROM jarvis_sync_state
ORDER BY table_name;
```

补充说明：

- `jarvis_reco_plan` 是 hash 判重表——计划不变就不插新行，其 `lag_seconds` 语义以同步器写入口径为准（T4 实现），若按"最近成功拉取时间"计算则上述阈值适用；
- 阈值是 P95 口径，偶发单次超限属正常抖动，持续超限才动作（排查步骤见 §6.1）。

### 5.3 首页徽标实现思路

两档做法，按需选：

**方案 A（零开发）**：`jarvis_sync_state` 本身就在 §1 里生成了只读页面（业务名 syncState），把上面 §5.2 的 CASE 逻辑留给肉眼——运维直接看"同步心跳"菜单页即可。

**方案 B（首页徽标）**：在首页放一个红/黄/绿聚合徽标，自动轮询。

后端：往生成的 `JarvisSyncStateController` 加一个只读聚合接口（依旧是查询语义，不破坏只读原则）：

```java
/** 同步链路健康聚合：返回 OK / ALERT / DOWN 与明细行 */
@PreAuthorize("@ss.hasPermi('jarvis:syncState:list')")
@GetMapping("/health")
public AjaxResult health()
{
    List<JarvisSyncState> rows = jarvisSyncStateService.selectJarvisSyncStateList(new JarvisSyncState());
    // 按 §5.2 的分组阈值逐行判级，取最差级别作为整体状态
    String worst = judge(rows);   // 实现略：DOWN > ALERT > OK
    AjaxResult ajax = AjaxResult.success();
    ajax.put("status", worst);
    ajax.put("rows", rows);
    return ajax;
}
```

前端：`ruoyi-ui/src/views/index.vue`（首页）加一个小卡片组件，`onMounted` 拉取 + `setInterval` 60s 轮询 `/jarvis/syncState/health`，按 `status` 渲染三色徽标（绿=OK、黄=ALERT、红=DOWN），点击跳转"同步心跳"列表页看明细。徽标文案建议直接显示最大 `lag_seconds` 与停跳表数量，例如"同步正常 · 最大延迟 8s"/"同步延迟 · signal_change 落后 132s"/"同步器停跳 9 分钟"。

---

## 6. 常见问题（FAQ)

### 6.1 页面/表里没数据，怎么排查？

按"同步器心跳 → 源侧 → 网络"三步走：

**第一步：看心跳表（jarvis_sync_state）**

```sql
SELECT table_name, last_ok_at, last_error, rows_total FROM jarvis_sync_state;
```

- 整表为空/没有目标表的行 → 同步器从未跑起来或该表分组被禁用（`groups.*.enabled`），去 Mac mini 查：`launchctl list | grep com.jarvis.sync`（进程在不在）、`tail -50 ~/.vibe-trading/sync/sync.log`；
- `last_error` 有内容 → 按错误文本处置（MySQL 拒连/权限/超时等）；
- 心跳新鲜、`rows_total` 不涨 → 进入第二步，多半是源侧没有新数据。

**第二步：看源侧（贾维斯）**

- `jarvis_signal_state`/`jarvis_signal_change`：源表是**懒建**的且只由 dashboard 重算路径写入——同步器的 API 通道每 180s 拉 consensus 会持续触发（开发计划 §3.2）；若 api 组被关，手工触发一次验证：`curl 'http://127.0.0.1:<dashboard端口>/api/twelve/signals?symbol=BTCUSDT'`；
- `jarvis_tape_bar`：源数据靠 WS 流实时聚合，确认贾维斯 WS 进程在跑；
- `jarvis_intraday_prediction`：需要 daemon 以 `--intraday` 模式运行，且每 4h 才产出一批；
- `jarvis_reco_plan`：hash 判重，**计划没变化就不插新行，这是正常现象**，别误判为断流（看 jarvis_sync_state 心跳即可确认链路活着）；
- `jarvis_market_snapshot`：默认 5 分钟一条/币，耐心等一个周期。

**第三步：查网络与权限**

```bash
mysql -h 127.0.0.1 -u jarvis_sync -p jiaweisi -e 'SELECT 1;'
mysql -h 127.0.0.1 -u jarvis_sync -p jiaweisi -e 'SHOW GRANTS;'
```

连接失败查 MySQL 服务与防火墙；GRANTS 里应能看到 13 张 `jarvis_*` 表的 SELECT/INSERT/UPDATE（无 DELETE 属正常设计）。迁云后还要查隧道/TLS（上游方案 §4.2）。

### 6.2 导入表弹窗里看不到 jarvis_* 表

- 表必须建在 RuoYi 数据源所连的 `jiaweisi` 库（`application-druid.yml:9`）——建错库了在别的库能查到但生成器看不到；
- **已导入过的表不再显示**（db/list 排除已导入项）：想重新导入，先在代码生成列表页删除该表记录再导；
- `qrtz_`/`gen_` 前缀表被硬排除，`jarvis_` 前缀不受影响。

### 6.3 生成代码后端启动报错 / 前端页面互相覆盖

典型原因是**业务名冲突**（§1.4）：两张表业务名相同会导致 `@RequestMapping("/jarvis/state")` 重复注册（Spring 启动即报 Ambiguous mapping）、`views/jarvis/state/index.vue` 相互覆盖。按 §1.4 总表逐表改"生成业务名"后重新生成。注意改业务名后菜单 SQL 里的 component 路径与权限标识也随之变化，旧菜单记录要清理。

### 6.4 字典下拉是空的 / 列表页枚举列显示原始值

- 先确认 T1 的字典 SQL 已执行（字典管理页面能看到 `jarvis_direction`/`jarvis_plan_status` 且有数据）；
- 字段信息里"显示类型"必须选**下拉框**（或单选框）且"字典类型"已绑定，二者缺一模板不会生成字典渲染（`vm/vue/index.vue.vm:23-39` 的分支条件）；
- 绑定后要**重新生成代码**并覆盖旧文件，浏览器强刷。

### 6.5 页面时间差 8 小时？

RuoYi 连接串 `serverTimezone=GMT%2B8`（`application-druid.yml:9-11`），而部分镜像表时间列按开发计划注释是 **UTC 换算**写入（如 `jarvis_tape_bar.minute_time`"源 minute epoch 秒换算 UTC"）。若页面显示与北京时间差 8 小时，属两侧口径未对齐：

- 先抽一行对照：`SELECT minute_time, create_time FROM jarvis_tape_bar ORDER BY id DESC LIMIT 1;` 与当前北京时间比对；
- 短期兜底：查询侧转换 `CONVERT_TZ(minute_time, '+00:00', '+08:00')`（或前端展示层 +8h）；
- 根本解法：让同步器统一按东八区写入（T3/T4 实现约定，以其定稿为准）。发现口径不一致时，把现象反馈给同步器侧修正，**不要**在 RuoYi 侧改库里的数据。

### 6.6 迁云库时 RuoYi 侧要改什么？

| 项 | 动作 |
|---|---|
| 数据库连接 | `application-druid.yml` 的 url/username/password 指向云 MySQL（连接串保留 `useUnicode/characterEncoding/serverTimezone` 参数），重启 |
| jarvis_* 表 | 云库重跑 T1 初始化脚本建表；历史数据要么 `mysqldump jiaweisi 'jarvis_*'` 导过去，要么让同步器清游标冷启动全量重推（幂等，数据量大时耗时更长） |
| 字典/菜单/sys_job | 属 RuoYi 业务库数据，整库迁移自然带过去；只迁结构不迁数据的话需重跑字典 SQL、菜单 SQL 与 §4.3 的 sys_job INSERT |
| quartz 归档任务 | 代码零改动（跟随应用数据源）；迁移后手工【执行一次】验证 |
| Redis | 云上另配 `spring.redis`，与本文无关但别漏 |
| 同步器侧（贾维斯） | 非 RuoYi 职责，但需同步改 `~/.vibe-trading/sync/sync_config.json` 的 mysql 段 + `ssl_mode: REQUIRED` 并重启 launchd 服务（开发计划 §4）；对外暴露安全基线见上游方案 §4.5（nginx/HTTPS/封 druid 监控页等） |

---

## 附录：本文引用的 RuoYi 代码证据索引

| 证据点 | 位置 |
|---|---|
| 生成器全局配置（autoRemovePre=false 等） | `ruoyi-generator/src/main/resources/generator.yml` |
| 类名/业务名/模块名生成逻辑 | `ruoyi-generator/.../util/GenUtils.java:19-30,151-187` |
| 导入表接口（tool:gen:import） | `ruoyi-generator/.../controller/GenController.java:113-116` |
| 批量生成 zip 接口 | `GenController.java:241-247`；自定义路径写盘校验 `:218` |
| 导入弹窗固定 element-plus 模板 | `ruoyi-ui/src/views/tool/gen/importTable.vue:114` |
| 生成信息表单（模板/前端类型/包/模块/业务名/上级菜单） | `ruoyi-ui/src/views/tool/gen/genInfoForm.vue:5-120` |
| 实体类名称手工修改入口 | `ruoyi-ui/src/views/tool/gen/basicInfoForm.vue:15-16` |
| 字段信息 tab 字典绑定列 | `ruoyi-ui/src/views/tool/gen/editTable.vue:95-104` |
| Controller 生成模板（6 方法与注解） | `ruoyi-generator/src/main/resources/vm/java/controller.java.vm` |
| Vue3 列表页模板（按钮 v-hasPermi 位置） | `vm/vue/v3/index.vue.vm:70-105,152-155,527+` |
| 菜单 SQL 模板（1 菜单 + 5 按钮） | `vm/sql/sql.vm` |
| "系统工具/代码生成/字典管理/定时任务"菜单初始数据 | `sql/ry_20260417.sql:164,183,172,177` |
| sys_job 表结构与示例数据 | `sql/ry_20260417.sql:582-602` |
| 定时任务包白名单/违规串 | `ruoyi-common/.../Constants.java:166,171-172` |
| 白名单校验实现 | `ruoyi-quartz/.../util/ScheduleUtils.java:129-140` |
| 任务 Bean 写法示例 | `ruoyi-quartz/.../task/RyTask.java` |
| 新增/修改任务时的校验入口 | `ruoyi-quartz/.../controller/SysJobController.java:101-107,137-143` |
| 数据源与库名（jiaweisi） | `ruoyi-admin/src/main/resources/application-druid.yml:9-11` |
| 后端端口/MyBatis 扫描配置 | `ruoyi-admin/src/main/resources/application.yml:19,107,109` |
| 前端技术栈（Vue3 + Element Plus） | `ruoyi-ui/package.json` |
