#!/usr/bin/env python3
"""RuoYi 贾维斯监控只读页面代码生成器（task-ruoyi-pages）。

按 6 张核心镜像表的元数据，生成 RuoYi-Vue 3.9.2 前后端只读代码：
  后端（ruoyi-system 模块，包 com.ruoyi.jarvis）：
    domain / mapper 接口 / mapper XML / service 接口 / service impl / controller
    —— controller 仅 list / getInfo / export 三个只读接口（无增删改）
  前端（ruoyi-ui，Vue3 + Element Plus）：
    api js（仅 list/get）/ views index.vue（列表+搜索+分页+导出+详情抽屉，无写按钮）

设计出处：Vibe-Trading/贾维斯-RuoYi同步-使用指引.md §1.4（业务名防冲突表）、§3（只读三层防线）。
运行：python3 tools/gen_ruoyi_jarvis_pages.py   （幂等覆盖生成）
"""

from __future__ import annotations

import os

RUOYI = "/Users/jolly/MyselfProject/JiaWeisiRuoyi/RuoYi-Vue"
PKG_DIR = os.path.join(RUOYI, "ruoyi-system/src/main/java/com/ruoyi/jarvis")
XML_DIR = os.path.join(RUOYI, "ruoyi-system/src/main/resources/mapper/jarvis")
API_DIR = os.path.join(RUOYI, "ruoyi-ui/src/api/jarvis")
VIEW_DIR = os.path.join(RUOYI, "ruoyi-ui/src/views/jarvis")

AUTHOR = "jarvis-sync"

# ── 字段元数据 ──────────────────────────────────────────────────────────
# (column, javaType, label, flags)
# flags: l=列表列  q=query(eq)  Q=query(like)  d:<dict>=字典  D=日期范围查询(时间主列)
#        x=excel导出  t=详情抽屉(大字段)  j=JSON格式化  w:<n>=列宽

TABLES = [
    dict(
        table="jarvis_signal_state", cls="JarvisSignalState", biz="signalState",
        func="信号当前态", pk="id",
        time_col="updatedAt",
        cols=[
            ("id", "Long", "主键", ""),
            ("symbol", "String", "交易对", "l Q x"),
            ("tf", "String", "时间框架", "l q x"),
            ("system_code", "String", "信号系统", "l Q x"),
            ("name_cn", "String", "系统中文名", "l x"),
            ("direction", "String", "方向", "l q d:jarvis_direction x"),
            ("strength", "BigDecimal", "强度", "l x"),
            ("reasoning", "String", "推理说明", "t"),
            ("levels_json", "String", "关键位快照", "t j"),
            ("plan_json", "String", "交易计划快照", "t j"),
            ("src_updated_ts", "BigDecimal", "源updated_ts", ""),
            ("src_changed_ts", "BigDecimal", "源changed_ts", ""),
            ("updated_at", "Date", "最近计算时间", "l D x w:160"),
            ("changed_at", "Date", "最近变更时间", "l x w:160"),
        ],
    ),
    dict(
        table="jarvis_signal_change", cls="JarvisSignalChange", biz="signalChange",
        func="信号变更流水", pk="id",
        time_col="signalTime",
        cols=[
            ("id", "Long", "流水号", "l x"),
            ("signal_time", "Date", "变更时间", "l D x w:160"),
            ("src_ts", "BigDecimal", "源ts", ""),
            ("symbol", "String", "交易对", "l Q x"),
            ("tf", "String", "时间框架", "l q x"),
            ("system_code", "String", "信号系统", "l Q x"),
            ("name_cn", "String", "系统中文名", "l x"),
            ("prev_direction", "String", "变更前方向", "l d:jarvis_direction x"),
            ("new_direction", "String", "变更后方向", "l q d:jarvis_direction x"),
            ("prev_strength", "BigDecimal", "变更前强度", "l x"),
            ("new_strength", "BigDecimal", "变更后强度", "l x"),
            ("change_kinds", "String", "变更类型", "t"),
            ("summary", "String", "摘要", "l x w:220"),
            ("prev_json", "String", "变更前快照", "t j"),
            ("new_json", "String", "变更后快照", "t j"),
            ("price", "BigDecimal", "变更时价格", "l x"),
        ],
    ),
    dict(
        table="jarvis_reco_plan", cls="JarvisRecoPlan", biz="recoPlan",
        func="推荐点位", pk="id",
        time_col="asOf",
        cols=[
            ("id", "Long", "主键", "l x"),
            ("symbol", "String", "交易对", "l Q x"),
            ("source_tf", "String", "来源框架", "l x"),
            ("side", "String", "方向", "l q d:jarvis_direction x"),
            ("entry_lo", "BigDecimal", "入场下沿", "l x"),
            ("entry_hi", "BigDecimal", "入场上沿", "l x"),
            ("stop_loss", "BigDecimal", "止损", "l x"),
            ("take_profit_1", "BigDecimal", "止盈1", "l x"),
            ("take_profit_2", "BigDecimal", "止盈2", "l x"),
            ("rr", "BigDecimal", "盈亏比", "l x"),
            ("position_pct", "BigDecimal", "建议仓位%", "l x"),
            ("plan_status", "String", "计划状态", "l q d:jarvis_plan_status x"),
            ("plan_reason", "String", "状态原因", "t"),
            ("basis_json", "String", "依据明细", "t j"),
            ("price", "BigDecimal", "拉取时价格", "l x"),
            ("direction", "String", "共识方向", "l d:jarvis_direction x"),
            ("confidence", "BigDecimal", "置信度", "l x"),
            ("plan_hash", "String", "计划哈希", ""),
            ("as_of", "Date", "计划时间", "l D x w:160"),
        ],
    ),
    dict(
        table="jarvis_intraday_prediction", cls="JarvisIntradayPrediction",
        biz="intradayPrediction", func="4小时预测", pk="id",
        time_col="barTime",
        cols=[
            ("id", "Long", "主键", "l x"),
            ("symbol", "String", "交易对", "l Q x"),
            ("bar_time", "Date", "K线时间", "l D x w:160"),
            ("src_bar_ts", "BigDecimal", "源bar_ts", ""),
            ("direction", "String", "预测方向", "l q x"),
            ("prob", "BigDecimal", "概率", "l x"),
            ("tradeable", "Integer", "可交易", "l q d:jarvis_yes_no x"),
            ("entry", "BigDecimal", "入场价", "l x"),
            ("stop", "BigDecimal", "止损价", "l x"),
            ("take", "BigDecimal", "止盈价", "l x"),
            ("atr_pct", "BigDecimal", "ATR%", "l x"),
            ("oos_hit_rate", "BigDecimal", "样本外命中率", "t"),
            ("p_value", "BigDecimal", "p值", "t"),
            ("reason", "String", "预测依据", "t"),
            ("why_text", "String", "人话解释", "t"),
            ("outcome_ret", "BigDecimal", "事后收益%", "l x"),
            ("hit", "Integer", "是否命中", "l q d:jarvis_yes_no x"),
        ],
    ),
    dict(
        table="jarvis_tape_bar", cls="JarvisTapeBar", biz="tapeBar",
        func="盘口分钟聚合", pk="id",
        time_col="minuteTime",
        cols=[
            ("id", "Long", "主键", ""),
            ("symbol", "String", "交易对", "l Q x"),
            ("minute_time", "Date", "分钟", "l D x w:160"),
            ("src_minute", "Long", "源minute", ""),
            ("buy_usd", "BigDecimal", "主动买入USD", "l x"),
            ("sell_usd", "BigDecimal", "主动卖出USD", "l x"),
            ("nr_buy_usd", "BigDecimal", "非散户买USD", "l x"),
            ("nr_sell_usd", "BigDecimal", "非散户卖USD", "l x"),
            ("open_price", "BigDecimal", "开盘价", "l x"),
            ("close_price", "BigDecimal", "收盘价", "l x"),
            ("high_price", "BigDecimal", "最高价", "l x"),
            ("low_price", "BigDecimal", "最低价", "l x"),
            ("trades_n", "Integer", "成交笔数", "l x"),
        ],
    ),
    dict(
        table="jarvis_market_snapshot", cls="JarvisMarketSnapshot",
        biz="marketSnapshot", func="市场情报快照", pk="id",
        time_col="snapTime",
        cols=[
            ("id", "Long", "主键", ""),
            ("symbol", "String", "交易对", "l Q x"),
            ("snap_time", "Date", "快照时间", "l D x w:160"),
            ("price", "BigDecimal", "现价", "l x"),
            ("price_chg_24h", "BigDecimal", "24h涨跌%", "l x"),
            ("funding_rate", "BigDecimal", "资金费率", "l x"),
            ("oi_value", "BigDecimal", "持仓量", "l x"),
            ("oi_change_pct", "BigDecimal", "持仓变化%", "l x"),
            ("long_pct", "BigDecimal", "多头占比%", "l x"),
            ("short_pct", "BigDecimal", "空头占比%", "l x"),
            ("ls_ratio", "BigDecimal", "多空比", "l x"),
            ("fng_value", "Integer", "恐贪指数", "l x"),
            ("fng_class", "String", "恐贪分级", "t"),
            ("sentiment_score", "BigDecimal", "情绪评分", "l x"),
            ("sentiment_bias", "String", "情绪倾向", "l q d:jarvis_direction x"),
            ("sentiment_headline", "String", "情绪结论", "t"),
            ("factors_json", "String", "情绪因子", "t j"),
        ],
    ),
]


def camel(col: str) -> str:
    parts = col.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def parse_flags(raw: str) -> dict:
    f = dict(list=False, q=None, dict=None, drange=False, excel=False,
             detail=False, json=False, width=None)
    for tok in raw.split():
        if tok == "l":
            f["list"] = True
        elif tok == "q":
            f["q"] = "eq"
        elif tok == "Q":
            f["q"] = "like"
        elif tok == "D":
            f["drange"] = True
        elif tok == "x":
            f["excel"] = True
        elif tok == "t":
            f["detail"] = True
        elif tok == "j":
            f["json"] = True
        elif tok.startswith("d:"):
            f["dict"] = tok[2:]
        elif tok.startswith("w:"):
            f["width"] = tok[2:]
    return f


def write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print("  wrote", os.path.relpath(path, RUOYI))


# ══════════════════════════════════ 后端


def gen_domain(t: dict) -> str:
    cls, table, func = t["cls"], t["table"], t["func"]
    fields, methods = [], []
    for col, jt, label, raw in t["cols"]:
        f = parse_flags(raw)
        prop = camel(col)
        ann = []
        if jt == "Date":
            ann.append('    @JsonFormat(pattern = "yyyy-MM-dd HH:mm:ss")')
        if f["excel"]:
            if jt == "Date":
                ann.append(f'    @Excel(name = "{label}", width = 30, dateFormat = "yyyy-MM-dd HH:mm:ss")')
            else:
                ann.append(f'    @Excel(name = "{label}")')
        fields.append(
            f"    /** {label} */\n" + ("\n".join(ann) + "\n" if ann else "")
            + f"    private {jt} {prop};\n"
        )
        cap = prop[0].upper() + prop[1:]
        methods.append(
            f"    public {jt} get{cap}()\n    {{\n        return {prop};\n    }}\n\n"
            f"    public void set{cap}({jt} {prop})\n    {{\n        this.{prop} = {prop};\n    }}\n"
        )
    return (
        "package com.ruoyi.jarvis.domain;\n\n"
        "import java.math.BigDecimal;\n"
        "import java.util.Date;\n"
        "import com.fasterxml.jackson.annotation.JsonFormat;\n"
        "import com.ruoyi.common.annotation.Excel;\n"
        "import com.ruoyi.common.core.domain.BaseEntity;\n\n"
        f"/**\n * {func}对象 {table}（贾维斯镜像表，只读）\n *\n * @author {AUTHOR}\n */\n"
        f"public class {cls} extends BaseEntity\n{{\n"
        "    private static final long serialVersionUID = 1L;\n\n"
        + "\n".join(fields) + "\n"
        + "\n".join(methods)
        + "}\n"
    )


def gen_mapper_iface(t: dict) -> str:
    cls, func = t["cls"], t["func"]
    return (
        "package com.ruoyi.jarvis.mapper;\n\n"
        "import java.util.List;\n"
        f"import com.ruoyi.jarvis.domain.{cls};\n\n"
        f"/**\n * {func}Mapper接口（只读镜像表：仅查询）\n *\n * @author {AUTHOR}\n */\n"
        f"public interface {cls}Mapper\n{{\n"
        f"    public {cls} select{cls}ById(Long id);\n\n"
        f"    public List<{cls}> select{cls}List({cls} query);\n"
        "}\n"
    )


def gen_mapper_xml(t: dict) -> str:
    cls, table = t["cls"], t["table"]
    result, sel_cols, wheres = [], [], []
    time_db_col = None
    for col, jt, _label, raw in t["cols"]:
        f = parse_flags(raw)
        prop = camel(col)
        result.append(f'        <result property="{prop}"    column="{col}"    />')
        sel_cols.append(col)
        if f["drange"]:
            time_db_col = col
        if f["q"] == "like":
            wheres.append(
                f'            <if test="{prop} != null and {prop} != \'\'"> and {col} like concat(\'%\', #{{{prop}}}, \'%\')</if>'
            )
        elif f["q"] == "eq":
            cond = f"{prop} != null" + ("" if jt in ("Integer", "Long", "BigDecimal") else f" and {prop} != ''")
            wheres.append(f'            <if test="{cond}"> and {col} = #{{{prop}}}</if>')
    result.append(f'        <result property="createTime"    column="create_time"    />')
    sel_cols.append("create_time")
    if time_db_col:
        wheres.append(
            f'            <if test="params.beginTime != null and params.beginTime != \'\'"> and {time_db_col} &gt;= #{{params.beginTime}}</if>'
        )
        wheres.append(
            f'            <if test="params.endTime != null and params.endTime != \'\'"> and {time_db_col} &lt;= #{{params.endTime}}</if>'
        )
    order = f"order by {time_db_col} desc" if time_db_col else "order by id desc"
    nl = "\n"
    return f'''<?xml version="1.0" encoding="UTF-8" ?>
<!DOCTYPE mapper
PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN"
"http://mybatis.org/dtd/mybatis-3-mapper.dtd">
<mapper namespace="com.ruoyi.jarvis.mapper.{cls}Mapper">

    <resultMap type="{cls}" id="{cls}Result">
{nl.join(result)}
    </resultMap>

    <sql id="select{cls}Vo">
        select {", ".join(sel_cols)} from {table}
    </sql>

    <select id="select{cls}List" parameterType="{cls}" resultMap="{cls}Result">
        <include refid="select{cls}Vo"/>
        <where>
{nl.join(wheres)}
        </where>
        {order}
    </select>

    <select id="select{cls}ById" parameterType="Long" resultMap="{cls}Result">
        <include refid="select{cls}Vo"/>
        where id = #{{id}}
    </select>
</mapper>
'''


def gen_service_iface(t: dict) -> str:
    cls, func = t["cls"], t["func"]
    return (
        "package com.ruoyi.jarvis.service;\n\n"
        "import java.util.List;\n"
        f"import com.ruoyi.jarvis.domain.{cls};\n\n"
        f"/**\n * {func}Service接口（只读）\n *\n * @author {AUTHOR}\n */\n"
        f"public interface I{cls}Service\n{{\n"
        f"    public {cls} select{cls}ById(Long id);\n\n"
        f"    public List<{cls}> select{cls}List({cls} query);\n"
        "}\n"
    )


def gen_service_impl(t: dict) -> str:
    cls, func = t["cls"], t["func"]
    var = cls[0].lower() + cls[1:]
    return (
        "package com.ruoyi.jarvis.service.impl;\n\n"
        "import java.util.List;\n"
        "import org.springframework.beans.factory.annotation.Autowired;\n"
        "import org.springframework.stereotype.Service;\n"
        f"import com.ruoyi.jarvis.mapper.{cls}Mapper;\n"
        f"import com.ruoyi.jarvis.domain.{cls};\n"
        f"import com.ruoyi.jarvis.service.I{cls}Service;\n\n"
        f"/**\n * {func}Service实现（只读镜像表）\n *\n * @author {AUTHOR}\n */\n"
        "@Service\n"
        f"public class {cls}ServiceImpl implements I{cls}Service\n{{\n"
        "    @Autowired\n"
        f"    private {cls}Mapper {var}Mapper;\n\n"
        "    @Override\n"
        f"    public {cls} select{cls}ById(Long id)\n    {{\n"
        f"        return {var}Mapper.select{cls}ById(id);\n    }}\n\n"
        "    @Override\n"
        f"    public List<{cls}> select{cls}List({cls} query)\n    {{\n"
        f"        return {var}Mapper.select{cls}List(query);\n    }}\n"
        "}\n"
    )


def gen_controller(t: dict) -> str:
    cls, biz, func = t["cls"], t["biz"], t["func"]
    var = cls[0].lower() + cls[1:]
    return (
        "package com.ruoyi.jarvis.controller;\n\n"
        "import java.util.List;\n"
        "import jakarta.servlet.http.HttpServletResponse;\n"
        "import org.springframework.beans.factory.annotation.Autowired;\n"
        "import org.springframework.security.access.prepost.PreAuthorize;\n"
        "import org.springframework.web.bind.annotation.GetMapping;\n"
        "import org.springframework.web.bind.annotation.PathVariable;\n"
        "import org.springframework.web.bind.annotation.PostMapping;\n"
        "import org.springframework.web.bind.annotation.RequestMapping;\n"
        "import org.springframework.web.bind.annotation.RestController;\n"
        "import com.ruoyi.common.annotation.Log;\n"
        "import com.ruoyi.common.core.controller.BaseController;\n"
        "import com.ruoyi.common.core.domain.AjaxResult;\n"
        "import com.ruoyi.common.core.page.TableDataInfo;\n"
        "import com.ruoyi.common.enums.BusinessType;\n"
        "import com.ruoyi.common.utils.poi.ExcelUtil;\n"
        f"import com.ruoyi.jarvis.domain.{cls};\n"
        f"import com.ruoyi.jarvis.service.I{cls}Service;\n\n"
        f"/**\n * {func}Controller（贾维斯镜像表·只读：仅 list/getInfo/export，无增删改）\n *\n * @author {AUTHOR}\n */\n"
        "@RestController\n"
        f'@RequestMapping("/jarvis/{biz}")\n'
        f"public class {cls}Controller extends BaseController\n{{\n"
        "    @Autowired\n"
        f"    private I{cls}Service {var}Service;\n\n"
        f"    /**\n     * 查询{func}列表\n     */\n"
        f'    @PreAuthorize("@ss.hasPermi(\'jarvis:{biz}:list\')")\n'
        '    @GetMapping("/list")\n'
        f"    public TableDataInfo list({cls} query)\n    {{\n"
        "        startPage();\n"
        f"        List<{cls}> list = {var}Service.select{cls}List(query);\n"
        "        return getDataTable(list);\n    }\n\n"
        f"    /**\n     * 导出{func}列表\n     */\n"
        f'    @PreAuthorize("@ss.hasPermi(\'jarvis:{biz}:export\')")\n'
        f'    @Log(title = "{func}", businessType = BusinessType.EXPORT)\n'
        '    @PostMapping("/export")\n'
        f"    public void export(HttpServletResponse response, {cls} query)\n    {{\n"
        f"        List<{cls}> list = {var}Service.select{cls}List(query);\n"
        f"        ExcelUtil<{cls}> util = new ExcelUtil<{cls}>({cls}.class);\n"
        f'        util.exportExcel(response, list, "{func}数据");\n    }}\n\n'
        f"    /**\n     * 获取{func}详细信息\n     */\n"
        f'    @PreAuthorize("@ss.hasPermi(\'jarvis:{biz}:query\')")\n'
        '    @GetMapping(value = "/{id}")\n'
        "    public AjaxResult getInfo(@PathVariable(\"id\") Long id)\n    {\n"
        f"        return success({var}Service.select{cls}ById(id));\n    }}\n"
        "}\n"
    )


# ══════════════════════════════════ 前端


def gen_api_js(t: dict) -> str:
    cls, biz, func = t["cls"], t["biz"], t["func"]
    cap = biz[0].upper() + biz[1:]
    return f'''import request from '@/utils/request'

// 查询{func}列表
export function list{cap}(query) {{
  return request({{
    url: '/jarvis/{biz}/list',
    method: 'get',
    params: query
  }})
}}

// 查询{func}详细
export function get{cap}(id) {{
  return request({{
    url: '/jarvis/{biz}/' + id,
    method: 'get'
  }})
}}
'''


def gen_index_vue(t: dict) -> str:
    cls, biz, func = t["cls"], t["biz"], t["func"]
    cap = biz[0].upper() + biz[1:]
    cols = [(c, jt, lb, parse_flags(raw)) for c, jt, lb, raw in t["cols"]]
    dicts = sorted({f["dict"] for _c, _j, _l, f in cols if f["dict"]})
    time_prop = None

    # 搜索表单
    search_items = []
    for col, jt, label, f in cols:
        prop = camel(col)
        if f["drange"]:
            time_prop = prop
            search_items.append(f'''      <el-form-item label="{label}" style="width: 308px">
        <el-date-picker
          v-model="daterange{cap}"
          value-format="YYYY-MM-DD HH:mm:ss"
          type="daterange"
          range-separator="-"
          start-placeholder="开始"
          end-placeholder="结束"
          :default-time="[new Date(2000, 1, 1, 0, 0, 0), new Date(2000, 1, 1, 23, 59, 59)]"
        ></el-date-picker>
      </el-form-item>''')
        elif f["q"] and f["dict"]:
            search_items.append(f'''      <el-form-item label="{label}" prop="{prop}">
        <el-select v-model="queryParams.{prop}" placeholder="全部" clearable style="width: 160px">
          <el-option v-for="dict in {f['dict']}" :key="dict.value" :label="dict.label" :value="dict.value" />
        </el-select>
      </el-form-item>''')
        elif f["q"]:
            search_items.append(f'''      <el-form-item label="{label}" prop="{prop}">
        <el-input v-model="queryParams.{prop}" placeholder="请输入{label}" clearable style="width: 160px" @keyup.enter="handleQuery" />
      </el-form-item>''')

    # 列表列
    table_cols = []
    for col, jt, label, f in cols:
        if not f["list"]:
            continue
        prop = camel(col)
        width = f' width="{f["width"]}"' if f["width"] else ""
        if f["dict"]:
            table_cols.append(f'''      <el-table-column label="{label}" align="center" prop="{prop}"{width}>
        <template #default="scope">
          <dict-tag :options="{f['dict']}" :value="scope.row.{prop}" />
        </template>
      </el-table-column>''')
        elif jt == "Date":
            table_cols.append(f'''      <el-table-column label="{label}" align="center" prop="{prop}"{width}>
        <template #default="scope">
          <span>{{{{ parseTime(scope.row.{prop}) }}}}</span>
        </template>
      </el-table-column>''')
        else:
            table_cols.append(
                f'      <el-table-column label="{label}" align="center" prop="{prop}"{width} :show-overflow-tooltip="true" />'
            )

    # 详情抽屉描述项（全字段：列表列 + detail 字段）
    detail_items = []
    for col, jt, label, f in cols:
        prop = camel(col)
        if f["json"]:
            detail_items.append(f'''        <el-descriptions-item label="{label}" :span="2">
          <pre class="jarvis-json">{{{{ formatJson(detailRow.{prop}) }}}}</pre>
        </el-descriptions-item>''')
        elif f["detail"]:
            detail_items.append(
                f'        <el-descriptions-item label="{label}" :span="2">{{{{ detailRow.{prop} }}}}</el-descriptions-item>'
            )
        elif f["dict"]:
            detail_items.append(f'''        <el-descriptions-item label="{label}">
          <dict-tag :options="{f['dict']}" :value="detailRow.{prop}" />
        </el-descriptions-item>''')
        elif jt == "Date":
            detail_items.append(
                f'        <el-descriptions-item label="{label}">{{{{ parseTime(detailRow.{prop}) }}}}</el-descriptions-item>'
            )
        else:
            detail_items.append(
                f'        <el-descriptions-item label="{label}">{{{{ detailRow.{prop} }}}}</el-descriptions-item>'
            )

    query_props = "\n".join(
        f"    {camel(c)}: undefined," for c, _j, _l, f in cols if f["q"]
    )
    dict_use = (
        f'const {{ {", ".join(dicts)} }} = useDict({", ".join(chr(34) + d + chr(34) for d in dicts)})'
        if dicts else "// 本表无字典列"
    )
    daterange_logic = (
        f'''  queryParams.value.params = {{}}
  if (daterange{cap}.value && daterange{cap}.value.length === 2) {{
    queryParams.value.params["beginTime"] = daterange{cap}.value[0]
    queryParams.value.params["endTime"] = daterange{cap}.value[1]
  }}''' if time_prop else "  queryParams.value.params = {}"
    )
    daterange_decl = f"const daterange{cap} = ref([])" if time_prop else ""
    daterange_reset = f"  daterange{cap}.value = []" if time_prop else ""

    nl = "\n"
    return f'''<template>
  <div class="app-container">
    <el-form :model="queryParams" ref="queryRef" :inline="true" v-show="showSearch">
{nl.join(search_items)}
      <el-form-item>
        <el-button type="primary" icon="Search" @click="handleQuery">搜索</el-button>
        <el-button icon="Refresh" @click="resetQuery">重置</el-button>
      </el-form-item>
    </el-form>

    <el-row :gutter="10" class="mb8">
      <el-col :span="1.5">
        <el-button type="warning" plain icon="Download" @click="handleExport" v-hasPermi="['jarvis:{biz}:export']">导出</el-button>
      </el-col>
      <right-toolbar v-model:showSearch="showSearch" @queryTable="getList"></right-toolbar>
    </el-row>

    <el-table v-loading="loading" :data="dataList">
{nl.join(table_cols)}
      <el-table-column label="操作" width="80" align="center" fixed="right" class-name="small-padding fixed-width">
        <template #default="scope">
          <el-button link type="primary" icon="View" @click="handleDetail(scope.row)" v-hasPermi="['jarvis:{biz}:query']">详情</el-button>
        </template>
      </el-table-column>
    </el-table>

    <pagination
      v-show="total > 0"
      :total="total"
      v-model:page="queryParams.pageNum"
      v-model:limit="queryParams.pageSize"
      @pagination="getList"
    />

    <!-- 详情抽屉（只读） -->
    <el-drawer v-model="detailOpen" title="{func}详情" size="46%" append-to-body>
      <el-descriptions :column="2" border>
{nl.join(detail_items)}
      </el-descriptions>
    </el-drawer>
  </div>
</template>

<script setup name="{cap}">
import {{ list{cap}, get{cap} }} from "@/api/jarvis/{biz}"

const {{ proxy }} = getCurrentInstance()
{dict_use}

const dataList = ref([])
const loading = ref(true)
const showSearch = ref(true)
const total = ref(0)
const detailOpen = ref(false)
const detailRow = ref({{}})
{daterange_decl}

const data = reactive({{
  queryParams: {{
    pageNum: 1,
    pageSize: 20,
{query_props}
  }}
}})

const {{ queryParams }} = toRefs(data)

/** 查询{func}列表 */
function getList() {{
  loading.value = true
{daterange_logic}
  list{cap}(queryParams.value).then(response => {{
    dataList.value = response.rows
    total.value = response.total
    loading.value = false
  }})
}}

/** 搜索按钮操作 */
function handleQuery() {{
  queryParams.value.pageNum = 1
  getList()
}}

/** 重置按钮操作 */
function resetQuery() {{
{daterange_reset}
  proxy.resetForm("queryRef")
  handleQuery()
}}

/** 详情按钮操作 */
function handleDetail(row) {{
  get{cap}(row.id).then(response => {{
    detailRow.value = response.data
    detailOpen.value = true
  }})
}}

/** JSON 字段格式化展示 */
function formatJson(raw) {{
  if (!raw) return ""
  try {{
    return JSON.stringify(JSON.parse(raw), null, 2)
  }} catch (e) {{
    return raw
  }}
}}

/** 导出按钮操作 */
function handleExport() {{
  proxy.download("jarvis/{biz}/export", {{
    ...queryParams.value
  }}, `{biz}_${{new Date().getTime()}}.xlsx`)
}}

getList()
</script>

<style scoped>
.jarvis-json {{
  margin: 0;
  max-height: 320px;
  overflow: auto;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}}
</style>
'''


def main() -> None:
    for t in TABLES:
        cls, biz = t["cls"], t["biz"]
        print(f"== {t['table']} -> {cls} ({biz})")
        write(os.path.join(PKG_DIR, "domain", f"{cls}.java"), gen_domain(t))
        write(os.path.join(PKG_DIR, "mapper", f"{cls}Mapper.java"), gen_mapper_iface(t))
        write(os.path.join(XML_DIR, f"{cls}Mapper.xml"), gen_mapper_xml(t))
        write(os.path.join(PKG_DIR, "service", f"I{cls}Service.java"), gen_service_iface(t))
        write(os.path.join(PKG_DIR, "service", "impl", f"{cls}ServiceImpl.java"), gen_service_impl(t))
        write(os.path.join(PKG_DIR, "controller", f"{cls}Controller.java"), gen_controller(t))
        write(os.path.join(API_DIR, f"{biz}.js"), gen_api_js(t))
        write(os.path.join(VIEW_DIR, biz, "index.vue"), gen_index_vue(t))
    print("\nDONE: 6 tables x 8 files = 48 files")


if __name__ == "__main__":
    main()
