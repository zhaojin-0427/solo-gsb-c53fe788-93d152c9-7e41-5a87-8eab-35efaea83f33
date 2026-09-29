# 离线矢量瓦片编译 API

一个可完全离线部署的矢量瓦片服务：上传 **EPSG:4326 GeoJSON** 图层，服务将其保存为
**不可变 revision**，并按需把点 / 线 / 面编译成 Web Mercator（EPSG:3857）的
[Mapbox Vector Tile (MVT)](https://github.com/mapbox/vector-tile-spec) 瓦片（z = 0–14）。

技术栈：**Python 3.11 · FastAPI · SQLite · Shapely · mapbox-vector-tile**。

---

## 目录

- [瓦片编译特性](#瓦片编译特性)
- [快速启动（Docker Compose 一键启动）](#快速启动docker-compose-一键启动)
- [本地开发运行](#本地开发运行)
- [HTTP 接口](#http-接口)
- [curl 使用示例](#curl-使用示例)
- [版本（revision）与并发控制](#版本revision与并发控制)
- [数据与确定性说明](#数据与确定性说明)
- [错误码](#错误码)
- [运行测试](#运行测试)
- [项目结构](#项目结构)

---

## 瓦片编译特性

每个瓦片按如下固定管线编译，保证**相同输入永远产出字节完全一致的结果**：

1. **投影**：EPSG:4326（lon/lat）→ EPSG:3857 Web Mercator（球形墨卡托）。
2. **空间预筛**：按要素 bbox 与瓦片（带缓冲）求交，跳过无关要素。
3. **裁剪**：以 **8 像素缓冲边界**（4096 网格下即 ±128 单位）裁剪点、线、面。
4. **拓扑简化**：Douglas–Peucker 且 `preserve_topology=True`，容差 **0.5 像素**
   （4096 网格下 8 单位）；点不简化，退化的线/面被丢弃。
5. **量化**：坐标四舍五入到 **4096 × 4096** 标准网格整数。
6. **排序后编码**：要素先按**图层名**、再按**要素 ID**（整数 ID 数值序 →
   字符串 ID 字典序 → 无 ID 要素的发布顺序）稳定排序，属性键也做确定性处理，
   最后由 `mapbox-vector-tile` 编码，多边形环方向由编码器强制为 MVT 规范方向。

已编译瓦片按 `(revision, z, x, y)` 存入 SQLite 作为不可变缓存；revision 永不修改，
因此瓦片与 ETag 可永久缓存。

---

## 快速启动（Docker Compose 一键启动）

前置要求：安装 Docker Engine 与 Docker Compose v2。

```bash
docker compose up -d --build
```

服务启动在 **http://localhost:8000**：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/docs | Swagger UI（交互式 API 文档） |
| http://localhost:8000/openapi.json | OpenAPI 描述 |

查看日志 / 停止：

```bash
docker compose logs -f
docker compose down            # 停止并删除容器（保留数据卷）
docker compose down -v         # 同时删除数据卷中的数据库
```

SQLite 数据库（含已发布 revision 与瓦片缓存）持久化在 Docker 卷 `mvt-data`
（容器内 `/data/tiles.db`）。

---

## 本地开发运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# 数据库路径可配置，默认可写目录下的 data/tiles.db
export MVT_DB_PATH=./data/tiles.db
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## HTTP 接口

### 发布

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/datasets` | 上传 GeoJSON，**创建**新数据集（revision = 1） |
| `POST` | `/datasets/{id}` | 向已有数据集发布**新 revision**（带 `expected_revision` 做乐观并发控制） |
| `GET` | `/datasets` | 列出数据集 |
| `GET` | `/datasets/{id}` | 数据集详情（含 revision 列表） |
| `GET` | `/datasets/{id}/revisions` | revision 列表 |
| `GET` | `/datasets/{id}/revisions/{revision}` | 单个 revision 元数据 |

请求体支持三种形式：

- `Content-Type: application/geo+json` 或 `application/json`：原始 GeoJSON；
- `multipart/form-data`：表单字段 `file`，文件名以 `.geojson/.json` 结尾；
- 文档体必须是 GeoJSON `FeatureCollection` 或单个 `Feature`（不支持裸几何与
  `GeometryCollection`）。

查询参数 / 请求头：

| 参数 | 位置 | 说明 |
| --- | --- | --- |
| `layer` | query | 图层名，默认 `default`；须匹配 `^[A-Za-z_][A-Za-z0-9_\-.:]{0,127}$` |
| `expected_revision` | query 或 `X-Expected-Revision` 头 | 期望的当前 revision；不匹配返回 **409** |

### 取瓦片

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/datasets/{id}/tiles/{z}/{x}/{y}.mvt` | 取**最新 revision** 的瓦片（可用 `?revision=N` 固定） |
| `GET` | `/datasets/{id}/revisions/{r}/tiles/{z}/{x}/{y}.mvt` | 取**指定不可变 revision** 的瓦片 |

约束：`0 ≤ z ≤ 14`，`0 ≤ x, y < 2^z`，使用 XYZ（Google/Slippy）编号。
非法参数返回 `400`，未知数据集 / revision 返回 `404`。

响应：

- `200 OK`：`Content-Type: application/vnd.mapbox-vector-tile`，含瓦片字节；
- `204 No Content`：该瓦片范围内无要素（空瓦片，仍带 ETag、可缓存）；
- `304 Not Modified`：`If-None-Match` 命中；
- 响应头：`ETag`（瓦片字节的 SHA-256，弱/强一致的内容标识）、
  `Cache-Control`、`X-Revision`、`X-Layer-Name`。
  固定 revision 的瓦片返回 `Cache-Control: public, max-age=31536000, immutable`。

---

## curl 使用示例

### 1. 发布图层（创建数据集）

```bash
curl -sS -X POST "http://localhost:8000/datasets?layer=places" \
  -H "Content-Type: application/geo+json" \
  --data-binary @examples/places.geojson
```

也可以用 multipart 上传（省略 `layer` 查询参数时图层名为 `default`）：

```bash
curl -sS -X POST "http://localhost:8000/datasets" \
  -F "file=@examples/places.geojson;type=application/geo+json"
```

返回示例：

```json
{
  "layer_name": "places",
  "feature_count": 5,
  "dataset": {
    "id": "9f3c0c6f2d7e4c5a9b8a1c0d2e3f4a5b",
    "name": "places",
    "latest_revision": 1,
    "revision": 1,
    "created_at": "2026-09-29T08:00:00.123456Z",
    "updated_at": "2026-09-29T08:00:00.123456Z"
  }
}
```

把 ID 记下来：

```bash
DS=9f3c0c6f2d7e4c5a9b8a1c0d2e3f4a5b
```

### 2. 下载瓦片

伦敦位于 z=2 的瓦片 `(2, 1)`：

```bash
curl -sS "http://localhost:8000/datasets/$DS/tiles/2/2/1.mvt" \
  -o london.mvt -D -
```

响应头（注意 `ETag` 与不可变缓存策略——此处是最新 revision 入口）：

```http
HTTP/1.1 200 OK
content-type: application/vnd.mapbox-vector-tile
etag: "1f0e8a2b..."
cache-control: public, max-age=3600
x-revision: 1
x-layer-name: places
```

查看瓦片内容（可用任意 MVT 解码器，示例用 Python）：

```bash
python -c "import mapbox_vector_tile,sys; \
print(mapbox_vector_tile.decode(open('london.mvt','rb').read()))"
```

### 3. 条件请求（If-None-Match）

```bash
ETAG='"1f0e8a2b..."'
curl -sS -o /dev/null -w "%{http_code}\n" \
  "http://localhost:8000/datasets/$DS/tiles/2/2/1.mvt" \
  -H "If-None-Match: $ETAG"
# 304
```

### 4. 发布新版本（带乐观锁）

```bash
curl -sS -X POST \
  "http://localhost:8000/datasets/$DS?expected_revision=1&layer=places" \
  -H "Content-Type: application/geo+json" \
  --data-binary @examples/places.geojson
```

当 `expected_revision` 不是当前 revision 时返回：

```http
HTTP/1.1 409 Conflict
{"error": {"code": "conflict",
           "message": "revision conflict: expected_revision=1 but current revision is 2"}}
```

### 5. 访问不可变的历史 revision

```bash
# revision 1 的瓦片内容永远不变（immutable 缓存）
curl -sS \
  "http://localhost:8000/datasets/$DS/revisions/1/tiles/2/2/1.mvt" \
  -o rev1.mvt

# 或通过最新入口固定 revision
curl -sS \
  "http://localhost:8000/datasets/$DS/tiles/2/2/1.mvt?revision=1" -o rev1.mvt
```

### 6. 非法输入示例

```bash
# 不支持的几何集合 → 400
curl -sS -X POST http://localhost:8000/datasets \
  -H "Content-Type: application/json" \
  -d '{"type":"FeatureCollection","features":[
       {"type":"Feature","properties":{},
        "geometry":{"type":"GeometryCollection","geometries":[]}}]}'

# 非法拓扑（蝴蝶结多边形）→ 400
curl -sS -X POST http://localhost:8000/datasets \
  -H "Content-Type: application/json" \
  -d '{"type":"FeatureCollection","features":[
       {"type":"Feature","properties":{},
        "geometry":{"type":"Polygon",
          "coordinates":[[[0,0],[1,1],[1,0],[0,1],[0,0]]]}}]}'

# 越界瓦片参数 → 400
curl -sS -i "http://localhost:8000/datasets/$DS/tiles/15/0/0.mvt"
curl -sS -i "http://localhost:8000/datasets/$DS/tiles/2/4/0.mvt"
```

错误响应统一为：

```json
{"error": {"code": "bad_request", "message": "..."}}
```

---

## 版本（revision）与并发控制

- 数据集首次发布创建 revision **1**；每次 `POST /datasets/{id}` 追加一个
  revision（2、3、…），旧 revision 的要素与瓦片**永不修改**。
- `GET .../tiles/...` 默认指向 `latest_revision`；
  `/revisions/{r}/tiles/...` 永远指向发布时固定的内容。
- 发布时携带 `expected_revision`（query 参数或 `X-Expected-Revision` 头）。
  服务在单个 `BEGIN IMMEDIATE` 事务内执行"比较—插入—翻转 latest 指针"，
  因此并发发布中只有一个请求成功，其余收到 **409 Conflict**，不会发生丢失更新。

## 数据与确定性说明

- 所有几何以 **EPSG:4326 WKB** 存于 SQLite；发布时严格校验：
  坐标必须有限、经度 ∈ [-180, 180]、纬度在 Web Mercator 有效域
  （±约 85.05112878）内；几何必须拓扑合法（拒绝自交多边形等）。
- Feature `id`：非负整数写入 MVT feature id；字符串 id 通过属性 `feature_id`
  保留并参与确定性排序；布尔 / 浮点 / 负数 id 被拒绝。
- 属性仅接受字符串 / 整数 / 浮点 / 布尔；`null` 与不支持类型的处理：
  `null` 值被丢弃，嵌套对象/数组等非原始类型在发布时返回 400。
- 重复请求（含重新编译）瓦片字节一致，`ETag` 为瓦片字节的 SHA-256。
- 空瓦片返回 `204`，其 ETag 同样稳定并可参与条件请求。

## 错误码

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 400 | `bad_request` | 非法 GeoJSON、非法/无效几何、越界 z/x/y、非法图层名或参数 |
| 404 | `not_found` | 数据集或 revision 不存在 |
| 409 | `conflict` | `expected_revision` 与当前 revision 不一致 |
| 415 | `unsupported_media_type` | 不支持的 Content-Type（原始字节上传） |

## 运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试覆盖：发布与 revision 追加、多线程并发 409、非法几何/参数校验、
瓦片裁剪缓冲、0.5px 简化、4096 量化、稳定排序、跨数据集字节一致、
ETag / If-None-Match、空瓦片与不可变 revision。

## 项目结构

```
app/
  constants.py    # 缩放层级、4096 网格、8px 缓冲、0.5px 容差等常量
  errors.py       # 统一 JSON 错误
  db.py           # SQLite schema / 事务 / revision CAS / 瓦片缓存
  geo.py          # GeoJSON 解析与严格校验
  projection.py   # EPSG:4326 -> EPSG:3857 与 XYZ 瓦片范围
  tiler.py        # 裁剪 -> 拓扑简化 -> 量化 -> 确定性排序编码
  services.py     # 发布与取瓦片的业务逻辑
  main.py         # FastAPI 路由
examples/
  places.geojson  # 点/线/面示例图层
tests/            # pytest 测试套件
Dockerfile
docker-compose.yml
requirements.txt
```
