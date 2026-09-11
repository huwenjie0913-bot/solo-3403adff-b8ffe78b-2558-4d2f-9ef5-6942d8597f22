# 电传机穿孔纸带识读与数字修复 API

供通信史料整理人员使用的本机 REST API：接收电传机穿孔纸带的分段扫描图，
自动校正倾斜与透视、沿走纸孔估算节距、把有重叠的扫描段拼成连续孔列，
按 ITA2（博多-默里码）或自定义码表以字母/数字移位状态解码，定位损伤，
并在人工监督下枚举数字修复候选。

技术栈：Python · FastAPI · Pydantic · SQLAlchemy · SQLite · OpenCV · NumPy

## 快速开始

```bash
pip install -r requirements.txt
uvicorn punchtape.app.main:app --reload        # 本机 http://127.0.0.1:8000
```

交互式接口文档：`http://127.0.0.1:8000/docs`

运行测试：

```bash
python3 -m pytest punchtape/tests/ -q          # 37 个测试
```

数据存放：`data/punchtape.db`（SQLite）、`data/segments/`（扫描图）。
可用环境变量 `PUNCHTAPE_DB`、`PUNCHTAPE_SEGMENT_DIR` 覆盖。

## 工作流程

```
上传分段扫描图 ──► 识别（校正/节距/拼接/解码）──► 诊断 ──► 人工修订(可锁定)
                                                          │
                              导出 JSON/TXT ◄── 修复枚举(候选不自动应用)
```

### 1. 建立纸带并上传扫描段

```http
POST /tapes
{"name": "1950年代气象报文", "tape_width_mm": 17.4, "tracks": 5,
 "dpi": 300, "sprocket_after_track": 2, "notes": "..."}

POST /tapes/{id}/segments?order=0        # multipart 上传 PNG/JPEG
POST /tapes/{id}/segments?order=1        # 相邻段需有数列重叠以便拼接
```

`sprocket_after_track` 为走纸孔位置：走纸孔位于第几条数据道之后
（5 单位纸带惯例为 2，即走纸孔在第 2、3 道之间）。纸带上下颠倒时
系统会自动检测并翻转道序。

### 2. 码表

内置 ITA2 自动预置（`GET /codetables` 可见）。自定义码表：

```http
POST /codetables
{"name": "CUSTOM5", "tracks": 5,
 "ltrs": {"3": "A", "4": " "}, "figs": {"3": "1"},
 "ltrs_code": 31, "figs_code": 27}
```

码字值为整数，bit0 = 第 1 数据道；某层字符为 `null` 表示该码字在
该移位状态下非法。

### 3. 识别

```http
POST /tapes/{id}/recognize
{"code_table_id": null,          # 缺省内置 ITA2
 "deskew": true,
 "perspective_corners": null,    # 可选：纸带四角 [[x,y]×4] 触发透视校正
 "sprocket_y_hint": null,        # 可选：人工指定走纸孔 y 坐标
 "bit_order": "lsb_first",
 "initial_shift": "ltrs",
 "min_overlap": 3}
```

处理流程：透视校正（给四角时）→ 走纸孔直线拟合去倾斜 → 走纸孔间距
中位数估算节距（与 dpi 标称值互校，偏差大时出诊断）→ 数据道聚类定位
→ 逐列判定各道有孔/无孔并给出置信度 → 多段按孔列模式对齐拼接。

```http
GET /jobs/{id}/columns     # 每列：位串、码字、置信度、原图坐标、来源扫描段
GET /jobs/{id}/text        # 解码文本（revised=false 可看原始识别）
GET /jobs/{id}/diagnostics # 诊断记录
```

### 4. 诊断类型

| 类型 | 含义 |
|---|---|
| `illegal_codeword` | 码字在当前移位状态下非法（自定义码表） |
| `redundant_shift` | 冗余移位码（已处于目标状态） |
| `long_figs_run` | 数字移位连续过长，疑似缺失 LTRS（移位状态中断） |
| `shift_at_end` | 纸带结束于数字移位状态 |
| `overlap_conflict` | 重叠段读数冲突（保留双方读数） |
| `missing_hole` | 走纸孔缺失（疑似撕裂） |
| `torn_region` | 撕裂/污损区（异常墨迹块或连续缺走纸孔） |
| `low_confidence` | 低置信列 |
| `pitch_mismatch` / `pitch_fallback` / `track_extrapolated` / `tape_flipped` | 几何估算异常 |

### 5. 人工修订（可锁定）

```http
POST /jobs/{id}/revisions
{"column_index": 12, "revised_bits": "01101", "locked": true,
 "author": "张三", "reason": "原图第2道孔清晰"}
```

修订按时间叠加、最新生效；`locked=true` 的列在修复枚举中不得改动。
原始识别孔阵永远保留，修订只生成"修订孔阵"。

### 6. 修复枚举

```http
POST /jobs/{id}/repair
{"rules": {"text_regex": null, "allowed_chars": null,
           "max_shift_changes": null, "must_contain": ["WEATHER"],
           "max_flips_per_column": 2, "conf_threshold": 0.6,
           "max_candidates": 2000},
 "columns": null}                # 可指定待修复列；缺省自动选取可疑列
```

候选来源：重复扫描冲突的备选读数 + 低置信位的补孔(0→1)/去孔(1→0)组合。
约束：锁定列不动、解码全程无非法码字、满足用户校验规则（文本正则、
允许字符集、移位次数上限、必含子串）。**证据不足时保留多个候选，
绝不自动覆盖识别结果**；候选集存档于 `GET /jobs/{id}/repairs`。

### 7. 导出

```http
GET /jobs/{id}/export?format=json   # 原始孔阵、修订孔阵、解码文本、
                                    # 诊断记录、修订历史、逐字符追溯
GET /jobs/{id}/export?format=txt    # 纯文本报告
```

JSON 中 `trace` 数组把每个字符追溯到孔列、来源扫描段（segment_id +
段内列号）及该列各道在原图中的坐标。

## 项目结构

```
punchtape/
├── app/
│   ├── main.py          # FastAPI 入口
│   ├── codetable.py     # ITA2 / 自定义码表
│   ├── imaging.py       # 倾斜/透视校正、走纸孔检测、节距估算、孔列提取
│   ├── stitching.py     # 重叠段对齐拼接与冲突检测
│   ├── decoding.py      # LTRS/FIGS 移位状态解码与诊断
│   ├── repair.py        # 修复候选枚举（锁定列 + 校验规则约束）
│   ├── pipeline.py      # 识别流水线（图像→拼接→解码→入库）
│   ├── export.py        # JSON / 纯文本导出
│   ├── models.py        # SQLAlchemy 模型（SQLite）
│   ├── schemas.py       # Pydantic 请求/响应模型
│   └── routers/         # tapes / codetables / jobs 路由
└── tests/               # 37 个测试，含合成纸带图像生成器
```

## 坐标与位序约定

- 走纸方向为 x 轴；孔道按 y 排序编号 0..tracks-1，bit0 = 第 1 道
  （`bit_order=msb_first` 可反转）。
- `bit_positions` 与列坐标均为**原图**像素坐标（透视/旋转校正前）。
- 置信度 0..1：有孔位按孔心到网格距离计算，无孔位按局部亮度佐证计算。
